"""CtxWeftRuntime：顶层 API。

Phase 4 版本：完整 SessionManager + TaskManager + LifecycleManager 支持；
同时保留 run_single_task() 兼容 Phase 1 测试。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ctx_weft.core.auth.authorizer import AllowAllAuthorizer, Authorizer
from ctx_weft.core.control.tokens import CancelToken, PauseToken, RunTokens
from ctx_weft.core.orchestrator.hitl_manager import HitlManager, HitlRequest  # noqa: F401 — re-exported for shell use
from ctx_weft.core.loop.capability_gateway import CapabilityGateway
from ctx_weft.core.assembler import (
    ContextAssembler,
    PriorityBudgetStrategy,
)
from ctx_weft.core.assembler.assembler import AssemblerDeps
from ctx_weft.core.assembler.composer import DefaultComposer
from ctx_weft.core.assembler.sources import (
    AgentRecallSource,
    BlackboardSource,
    CapabilitySource,
    GuidanceSource,
    IdentitySource,
    KnowledgeRetrievalSource,
    SemanticRecallSource,
    TaskSpecSource,
)
from ctx_weft.core.events import Event, EventType, InProcessEventBus
from ctx_weft.core.events.bus import EventBus
from ctx_weft.protocols import LLMClient, LLMClientResolver
from ctx_weft.core.loop.driver import LoopContext, LoopState, StepDriver, make_event
from ctx_weft.core.loop.park import HitlPark
from ctx_weft.core.loop.steps import ActStep, FinalizeStep, RecognizeIntentStep, ObserveStep, PrepareStep
from ctx_weft.core.loop.steps.background_observe import launch_background_observe, register_close_synth
from ctx_weft.core.loop.steps.compact import CompactStep
from ctx_weft.core.loop.steps.reconcile import ReconcileStep
from ctx_weft.core.loop.steps.suspend import SuspendStep
from ctx_weft.core.orchestrator.capability_cache import CapabilityCache
from ctx_weft.core.orchestrator.control_capability import ControlCapabilityProvider
from ctx_weft.protocols.capability import SessionScopedCapabilityProvider
from ctx_weft.core.state.models import NormalTaskSettings
from ctx_weft.core.orchestrator.lifecycle_manager import LifecycleManager
from ctx_weft.core.orchestrator.session_manager import SessionManager
from ctx_weft.core.orchestrator.task_manager import TaskManager, _task_payload
from ctx_weft.core.orchestrator.task_runner import AgentBinding, TaskRunner, effective_agent_id
from ctx_weft.core.orchestrator.task_queue import QueueEntry
from ctx_weft.core.state.models import Agent, LoopGuard, Session, Task
from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols import (
    AgentTemplate,
    Capability,
    KnowledgeProvider,
    LLMOutageError,
    MemoryProvider,
    MemoryScope,
    ProviderContext,
    TemplateResolver,
)
from ctx_weft.protocols.capability import (
    AgentCapability,
    AgentCapabilityProvider,
    CapabilityProvider,
    SkillCapabilityProvider,
    qualify,
)

logger = logging.getLogger(__name__)


def _latest_prior_root_task(task_manager: "TaskManager", t: "Task") -> "Task | None":
    """The most recent prior root task (no parent) in the session — the inherit source
    for a root user-turn dispatched straight to a sub-agent.

    Such a task has ``use_subagent=True`` + ``inherit_memory=True`` but no
    ``parent_task_id`` (root turns have none), so the parent-based copy in ``_resolve``
    has nothing to copy from. We fall back to the previous root task: because
    ``_copy_memory_for_inherit`` recalls by ``agent_id``, sourcing from it pulls the
    prior root agent's whole conversation so far, restoring cross-turn continuity.
    """
    prior = [
        x for x in task_manager.all_tasks()
        if not x.parent_task_id and x.id != t.id
        and x.created_at is not None and t.created_at is not None
        and x.created_at < t.created_at
    ]
    return max(prior, key=lambda x: x.created_at, default=None)


async def _copy_memory_for_inherit(
    parent_task: "Task",
    child_task: "Task",
    sub_agent: "Agent",
    memory: "MemoryProvider",
    session_id: str,
    tenant_id: str,
) -> None:
    """spawn 时把 parent agent 的当前召回视图复制进 child agent scope（spec Phase 2 2026-06-30）。

    镜像父此刻 AgentRecallSource 的两路召回：task 层 body（父自身 + 同 agent 兄弟，按 agent_id 跨 task）
    + agent 层对话回合（Phase 1 写的 start_task 框 / 跨 agent bubble / 同 agent finish 对）。二者按
    (timestamp, seq_no) 归并后写入 child scope，作 child 的起始记忆；之后两边各自演进。
    同 agent 兄弟 body 因此带框（不再裸泄漏），跨 agent 兄弟以 bubble 呈现。
    """
    # AGENT_COMPACT_SUMMARY（父的黑盒折叠派发日志）仍排除——对子无用（沿用 2026-06-23 的窄化意图，
    # 只是现在改为 mirror 而非「仅 OPEN-task body」）。
    from ctx_weft.protocols import MemoryEvent, MemoryEventType, MemoryScope

    parent_agent_id = parent_task.assigned_agent_id or parent_task.creator_agent_id
    parent_scope = MemoryScope(session_id=session_id, task_id=parent_task.id, agent_id=parent_agent_id)
    ctx = ProviderContext(session_id=session_id, tenant_id=tenant_id)

    # Mirror the parent agent's current recall view (spec Phase 2, 2026-06-30):
    #   (a) task-layer body — parent's own turns + same-agent siblings' bodies (by agent_id), and
    #   (b) agent-layer dispatch turns — the start_task frames, cross-agent bubbles, and same-agent
    #       finish pairs that Phase 1 writes into the parent agent scope.
    # Merging both by (timestamp, seq_no) means inherited same-agent sibling bodies arrive FRAMED
    # (their start_task frame precedes them, so no naked leak) and cross-agent siblings arrive as
    # bubbles. AGENT_COMPACT_SUMMARY is still excluded — the parent's folded black-box dispatch log
    # is of little use to a child.
    body_records = await memory.recall_recent_by_agent(
        agent_scope=parent_scope,
        types=[
            MemoryEventType.USER_PROMPT,
            MemoryEventType.LLM_RESPONSE,
            MemoryEventType.TOOL_RESULT,
            MemoryEventType.TASK_COMPACT_SUMMARY,
        ],
        limit=2000,
        ctx=ctx,
    )
    frame_records = await memory.recall_recent(
        scope=parent_scope,
        types=[MemoryEventType.AGENT_CONVERSATION_TURN],
        limit=2000,
        ctx=ctx,
    )
    combined = sorted(
        [*body_records, *frame_records],
        key=lambda r: (r.timestamp, r.metadata.get("seq_no", 0)),
    )
    child_scope = MemoryScope(session_id=session_id, task_id=child_task.id, agent_id=sub_agent.id)
    for r in combined:  # chronological → re-ingest preserves order via fresh per-scope seq_no
        md = {"inherited_from_task_id": parent_task.id}
        if r.role == "assistant" and r.metadata.get("tool_calls"):
            md["tool_calls"] = r.metadata["tool_calls"]
        if r.role == "tool" and r.metadata.get("tool_call_id"):
            md["tool_call_id"] = r.metadata["tool_call_id"]
        await memory.ingest(
            MemoryEvent(
                type=MemoryEventType.AGENT_CONVERSATION_TURN,
                scope=child_scope,
                content=r.content,
                timestamp=r.timestamp,
                role=r.role,
                metadata=md,
            ),
            ctx,
        )


async def _flush_tracking_memory(
    agent: "Agent",
    task: "Task",
    task_manager: "TaskManager",
    memory: "MemoryProvider",
    session_id: str,
    tenant_id: str,
) -> None:
    """把 agent.tracking_task_ids 中的前序任务结果写入 memory，写完后标记已拉取。"""
    pending = [tid for tid in agent.tracking_task_ids if tid not in agent.fetched_tracking_ids]
    if not pending:
        return

    from ctx_weft.protocols import MemoryEvent, MemoryEventType, MemoryScope
    from ctx_weft.protocols.context import ProviderContext

    scope = MemoryScope(session_id=session_id, task_id=task.id, agent_id=agent.id)
    provider_ctx = ProviderContext(session_id=session_id, tenant_id=tenant_id)

    for tid in pending:
        tracked = task_manager.get_task(tid)
        if tracked is None:
            agent.fetched_tracking_ids.add(tid)
            continue
        result = ""
        if isinstance(tracked.outputs, list):
            result = next(
                (p.get("text", "") for p in tracked.outputs if isinstance(p, dict) and p.get("type") == "text"),
                "",
            )
        elif isinstance(tracked.outputs, str):
            result = tracked.outputs
        report = tracked.task_summary or tracked.process_report or ""
        if result or report:
            content = f"sub-task '{tracked.title}' completed. \nresult:{result} \nprocess report:{report}"
            try:
                await memory.ingest(
                    MemoryEvent(
                        type=MemoryEventType.OBSERVER_SUMMARY,
                        scope=scope,
                        content=content,
                        timestamp=now_utc(),
                        role="assistant",
                        metadata={"task_id": tid, "outcome": tracked.status.lower()},
                    ),
                    provider_ctx,
                )
            except Exception:
                logger.exception("Failed to flush tracking task %s into agent %s memory", tid, agent.id)
        agent.fetched_tracking_ids.add(tid)


# ── ProviderRegistry ──────────────────────────────────────────────────────────


class ProviderRegistry:
    """Provider 注册表。

    四种 provider 类型：
      memory     — 唯一；重复注册覆盖
      knowledge  — 有序列表，按 priority 升序（数字小 = 优先级高）
      capability — 有序列表；SkillCapabilityProvider 注册/注销时通知 SkillExecutorCapabilityProvider
      llm        — 唯一；重复注册覆盖
    """

    def __init__(self) -> None:
        self._memory: MemoryProvider | None = None
        self._knowledge: list[tuple[int, KnowledgeProvider]] = []  # (priority, provider)
        self._capabilities: list[CapabilityProvider] = []
        self._capability_authorizers: dict[str, Authorizer] = {}  # provider_name or capability_id → Authorizer
        self._llm_provider: LLMClientResolver | None = None

    # ── Memory ────────────────────────────────────────────────────────────────

    def register_memory(self, provider: MemoryProvider) -> None:
        self._memory = provider

    def get_memory(self) -> MemoryProvider:
        if self._memory is None:
            raise RuntimeError("MemoryProvider not registered")
        return self._memory

    # ── Knowledge ─────────────────────────────────────────────────────────────

    def register_knowledge(self, provider: KnowledgeProvider, *, priority: int = 0) -> None:
        """注册知识源。priority 升序决定查询顺序（0 最高，数字越小越先查询）。"""
        self._knowledge.append((priority, provider))
        self._knowledge.sort(key=lambda t: t[0])

    def get_knowledge_providers(self) -> list[KnowledgeProvider]:
        return [p for _, p in self._knowledge]

    # ── Capability ────────────────────────────────────────────────────────────

    def register_capability(
        self,
        provider: CapabilityProvider,
        *,
        authorizer: Authorizer | None = None,
        tool_authorizers: dict[str, Authorizer] | None = None,
    ) -> None:
        self._capabilities.append(provider)
        if authorizer is not None:
            self._capability_authorizers[provider.name] = authorizer
        if tool_authorizers:
            self._capability_authorizers.update(tool_authorizers)
        if isinstance(provider, SkillCapabilityProvider):
            self._notify_skill_executor_dirty()

    def deregister_capability(self, provider_name: str) -> bool:
        before = len(self._capabilities)
        removed = [p for p in self._capabilities if p.name == provider_name]
        self._capabilities = [p for p in self._capabilities if p.name != provider_name]
        prefix = provider_name + ":"
        for key in [k for k in self._capability_authorizers if k == provider_name or k.startswith(prefix)]:
            del self._capability_authorizers[key]
        if any(isinstance(p, SkillCapabilityProvider) for p in removed):
            self._notify_skill_executor_dirty()
        return len(self._capabilities) < before

    def get_capability_providers(self) -> list[CapabilityProvider]:
        return list(self._capabilities)

    def set_capability_authorizer(self, key: str, authorizer: Authorizer) -> None:
        """注册或覆盖单个 authorizer，key 可以是 provider_name 或完整 capability_id。"""
        self._capability_authorizers[key] = authorizer

    def get_capability_authorizers(self) -> dict[str, Authorizer]:
        """provider_name → Authorizer 映射，供 CapabilityGateway 使用。"""
        return dict(self._capability_authorizers)

    def _notify_skill_executor_dirty(self) -> None:
        """SkillCapabilityProvider 增减时通知 SkillExecutorCapabilityProvider 重建索引。"""
        from ctx_weft.core.orchestrator.skill_executor_capability import SkillExecutorCapabilityProvider
        for p in self._capabilities:
            if isinstance(p, SkillExecutorCapabilityProvider):
                p.mark_dirty()
                break

    # ── LLM ──────────────────────────────────────────────────────────────────

    def register_llm_provider(self, provider: LLMClientResolver) -> None:
        self._llm_provider = provider

    def get_llm_provider(self) -> LLMClientResolver:
        if self._llm_provider is None:
            raise RuntimeError("LLMProvider not registered")
        return self._llm_provider

    def has_llm_provider(self) -> bool:
        return self._llm_provider is not None


# ── SessionStartParams ────────────────────────────────────────────────────────


@dataclass
class SessionStartParams:
    """Parameters for start_session(). Construct via SessionStartParams.create().

    resume=False → create a new session. session_id=None lets runtime generate the ID;
                   session_id=<id> creates a NEW session with a host-provided ID
                   (so the host can pre-register session-scoped resources, e.g. the
                   filesystem provider's workspace, before execution starts).
    resume=True  → resume an existing session (root_agent_id recovered from event store);
                   session_id must be set.
    """

    template_id: str
    user_prompt: str
    initial_task_settings: NormalTaskSettings
    context_limit: int
    session_id: str | None = None
    tenant_id: str = "default"
    llm_account: str | None = None
    llm_model: str | None = None
    token_budget: int = 200_000
    reserved_output_tokens: int = 8192
    resume: bool = False

    @classmethod
    def create(
        cls,
        template_id: str,
        user_prompt: str,
        *,
        context_limit: int,
        session_id: str | None = None,
        initial_task: dict | None = None,
        tenant_id: str = "default",
        llm_account: str | None = None,
        llm_model: str | None = None,
        token_budget: int = 200_000,
        reserved_output_tokens: int = 8192,
        resume: bool = False,
    ) -> "SessionStartParams":
        from ctx_weft.core.state.models import deserialize_settings
        return cls(
            template_id=template_id,
            user_prompt=user_prompt,
            session_id=session_id,
            initial_task_settings=deserialize_settings(initial_task),
            context_limit=context_limit,
            tenant_id=tenant_id,
            llm_account=llm_account,
            llm_model=llm_model,
            token_budget=token_budget,
            reserved_output_tokens=reserved_output_tokens,
            resume=resume,
        )


# ── RunHandle ─────────────────────────────────────────────────────────────────


@dataclass
class RunHandle:
    """Handle to a running or completed session/task."""

    run_id: str
    session_id: str
    task_id: str
    agent_id: str
    template_id: str
    event_bus: EventBus
    _state: LoopState | None = None

    async def events(self) -> AsyncIterator[Event]:
        from ctx_weft.core.events.types import EventFilter
        async for ev in self.event_bus.stream(EventFilter(run_id=self.run_id)):
            yield ev

    async def wait_for_finish(self, timeout: float = 300.0) -> LoopState | None:
        """Block until RunFinished event or timeout."""
        try:
            async with asyncio.timeout(timeout):
                from ctx_weft.core.events.types import EventFilter
                async for ev in self.event_bus.stream(EventFilter(run_id=self.run_id)):
                    if ev.type == "RunFinished":
                        return self._state
        except TimeoutError:
            pass
        return self._state


async def _task_has_dangling_tool_call(memory, scope, provider_ctx) -> bool:
    """该 scope 最近一个 assistant turn 是否存在「有 tool_call、无 TOOL_RESULT」（spec/07 §6）。"""
    from ctx_weft.core.loop.steps.reconcile import _dangling_tool_calls
    return bool(await _dangling_tool_calls(memory, scope, provider_ctx))



# ── CtxWeftRuntime ─────────────────────────────────────────────────────────────


class CtxWeftRuntime:
    """Top-level runtime.

    Supports two modes:
    - run_single_task(): Phase 1 compat — simple single-task execution
    - start_session(): Phase 4 orchestration; session_id=None creates, session_id=<id> resumes
    """

    def __init__(
        self,
        template_resolver: TemplateResolver,
        providers: ProviderRegistry | None = None,
        llm: LLMClient | None = None,
        hitl_manager: HitlManager | None = None,
        event_store: "Any | None" = None,
        config: "RuntimeConfig | None" = None,
    ) -> None:
        from ctx_weft.core.config import RuntimeConfig
        self._config = config or RuntimeConfig()
        self._llm = llm  # fallback for backward compat / tests
        self._template_resolver = template_resolver
        self.providers = providers or ProviderRegistry()
        self._event_bus = InProcessEventBus()
        # shell 侧持有此实例，用于 approve() / reject() 响应 HITL 请求
        self.hitl_manager: HitlManager = hitl_manager or HitlManager(
            timeout_sec=self._config.hitl_timeout_sec,
            event_bus=self._event_bus,
            max_resolved=self._config.hitl_max_resolved,
        )
        # 冷应答自触发 session resume —— 热/冷分流在 core 内闭环,host 只转发回复（spec/07 §6/§9）。
        self.hitl_manager.set_cold_resolve_handler(self._resume_after_cold_hitl)
        # 冷决定查询：reconcile 短路的跨重启回落（内存缓存重启后不含已解决,不查日志会重问）。
        self.hitl_manager.set_cold_decision_lookup(self._cold_hitl_decision)
        # 默认使用内存版 EventStore，自动订阅 EventBus；传入自定义实现时由调用方自行 wire
        from ctx_weft.core.state.event_store import InMemoryEventStore
        self.event_store = event_store or InMemoryEventStore(event_bus=self._event_bus)

        # Auto-register 内置 providers（与用户注册的 providers 无关）
        control_provider = ControlCapabilityProvider(hitl_manager=self.hitl_manager)
        self.providers.register_capability(control_provider)

        from ctx_weft.core.orchestrator.skill_executor_capability import SkillExecutorCapabilityProvider
        skill_executor = SkillExecutorCapabilityProvider(self.providers)
        self.providers.register_capability(skill_executor)

        from ctx_weft.core.orchestrator.agent_capability import TemplateAgentCapabilityProvider
        self.providers.register_capability(TemplateAgentCapabilityProvider(self._template_resolver))

        # Capability cache (per-session, shared across all agents in runtime)
        self._capability_cache = CapabilityCache()

        # Per-run 控制信号 registry：session_id → {task_id → RunTokens}。随派发登记、随 run
        # 注销（_SessionTaskRunner.execute），被顶替旧 TM 的 inflight 一样在册——pause/cancel
        # 经 registry 必达全部在途 run（spec 2026-07-05）。
        self._run_tokens: dict[str, dict[str, RunTokens]] = {}
        # pause 弃子进行中的 session：新派发 run 的 PauseToken 出生即 paused
        # （root agent 任务被重排后，新 run 在 act 首个 checkpoint 立即 park）。
        self._pausing: set[str] = set()
        # 本轮暂停的续跑点名额（一次性）：pause_session pause 到在途 root run、或闩锁窗口内
        # 第一个 root run born-pause 时认领；此后窗口内再派发的 root run 一律 born-cancel。
        # 与 _pausing 同生命周期（_on_idle / _release_session / pause_session 兜底一起清）。
        self._pause_claimed: set[str] = set()
        # compact 等一次性操作的忙位（原先借 _cancel_tokens dict 占位）。
        self._busy_sessions: set[str] = set()
        self._task_managers: dict[str, TaskManager] = {}
        # Per-session resume 锁：串行化同一 session 的 recover_session，避免重叠的冷 HITL 应答 /
        # /resume 并发建出两个 TaskManager、两套 drain 竞争派发（spec/07 §9）。惰性建、不回收
        # （体量微小、按 session 数有界）。
        self._resume_locks: dict[str, asyncio.Lock] = {}

    @property
    def event_bus(self) -> EventBus:
        return self._event_bus

    @property
    def template_resolver(self) -> TemplateResolver:
        """构造时注入的 TemplateResolver（公开只读，host 列模板等场景用，勿绕私有属性）。"""
        return self._template_resolver

    def _resolve_llm(
        self,
        llm_account: str | None = None,
        llm_model: str | None = None,
    ) -> LLMClient:
        """Resolve LLMClient: registry first, then fallback to self._llm."""
        if self.providers.has_llm_provider():
            return self.providers.get_llm_provider().get_client(llm_account, llm_model)
        if self._llm is not None:
            return self._llm
        raise RuntimeError(
            "No LLM available. Register an LLMProvider via providers.register_llm_provider() "
            "or pass llm= to CtxWeftRuntime."
        )

    def _sync_session_llm_window(self, session: Session) -> None:
        """换模型/账号续跑后，把会话窗口参数对齐新模型（context_limit / reserved_output_tokens）。

        只在恢复方显式传入 llm 覆盖时调用：CONTEXT_OVERFLOW 挂起的会话换更大窗口的模型
        恢复，若窗口仍沿用投影里旧模型的值，重装配会原样再溢出，切换等于无效。
        duck-type 读取（镜像 run_single_task）：桩 client 缺属性时保持会话原值；解析失败
        （如未注册 provider）不阻断恢复，只记日志、沿用原值。
        """
        try:
            llm = self._resolve_llm(session.llm_provider or None, session.llm_model or None)
        except Exception:
            logger.warning(
                "model-switch resume: cannot resolve LLM client for session %s; "
                "keeping projected window params", session.id,
            )
            return
        limit = getattr(llm, "context_limit", None)
        if limit:
            session.context_limit = limit
        reserve = getattr(llm, "output_reserve", None)
        if reserve is not None:
            session.reserved_output_tokens = reserve

    async def _resolve_subagent_template(self, qualified: str, ctx: ProviderContext) -> str:
        """Map a qualified sub-agent name (agent__planner) back to its template_name.

        Iterates every registered AgentCapabilityProvider (multi-provider, arbitrary
        prefix). An unmatched/raw value passes through as a literal template_id.
        """
        for p in self.providers.get_capability_providers():
            if isinstance(p, AgentCapabilityProvider):
                try:
                    caps = await p.list(ctx)
                except Exception:
                    continue
                for cap in caps:
                    if isinstance(cap, AgentCapability) and qualify(cap.id) == qualified:
                        return cap.template_name
        return qualified

    def _register_run_tokens(
        self, session_id: str, task_id: str, *, root_run: bool = True,
    ) -> RunTokens:
        """为一次派发发放控制信号对；pause 弃子窗口内按执行 agent 分流出生信号：

        - root agent 的 run 出生即 paused → act 首检查点 park 成唯一续跑点。名额一次性
          （_pause_claimed）：root agent 同时有多个任务（后继任务/多条消息）时，第一个
          root run 认领后，窗口内再派发的 root run（如子任务死光被重排的 SUSPENDED root
          任务）一律 born-cancel——"一次暂停恰一个续跑点"。park 中的 run 非终态，其祖先
          不会被 _try_resume_parent 重排，合法等待中的父任务不受此规则误伤。
        - 非 root run 出生即 cancelled（M-1）——检查点 pause 先于 cancel，若两信号齐置，
          多级委派中被 _try_resume_parent 重排的中间父任务会 park 出气泡抢走续跑点；
          born-cancel 使其协作取消，再经 _try_resume_parent 逐级级联到 root agent 的任务。
        """
        tokens = RunTokens(cancel=CancelToken(), pause=PauseToken())
        if session_id in self._pausing:
            if root_run and session_id not in self._pause_claimed:
                self._pause_claimed.add(session_id)
                tokens.pause.pause()
            else:
                tokens.cancel.cancel()
        self._run_tokens.setdefault(session_id, {})[task_id] = tokens
        return tokens

    def _deregister_run_tokens(self, session_id: str, task_id: str) -> None:
        per = self._run_tokens.get(session_id)
        if per is None:
            return
        per.pop(task_id, None)
        if not per:
            self._run_tokens.pop(session_id, None)

    async def pause_session(self, session_id: str) -> bool:
        """软打断（spec 2026-07-05）：放弃其余在途/排队任务，只留 root agent 当前那一轮。

        - 置 _pausing 闩锁：其间新派发的 root agent run 出生即 paused（被 _try_resume_parent
          重排后在 act 首检查点 park，不烧 LLM）——但续跑点名额一次性（_pause_claimed）：
          在途 root run 被 pause 或首个 root run born-pause 即认领，窗口内其后的 root run
          一律 born-cancel；非 root run 出生即 cancelled（多级委派的中间父任务协作取消后
          逐级级联，气泡最终必落 root agent，M-1）。
        - 排队任务全部放弃（abandon_pending：标 CANCELED，不动 session 状态、不封 drain）。
        - 在途 run 按真实执行 agent 划分：== root agent 的那一轮（同 agent 串行 ≤1）pause →
          park 一个 wait 气泡；其余（含被顶替旧 TM 的 inflight）cancel → 协作取消终态。
        - 闩锁由 _on_idle（root park 后会话空闲）或 _release_session 清除。
        """
        per = self._run_tokens.get(session_id, {})
        tm = self._task_managers.get(session_id)
        if not per and (tm is None or tm.is_done()):
            return False
        self._pausing.add(session_id)
        root_agent = ""
        if tm is not None:
            tm.set_pause_abandon(True)
            root_agent = (tm.session.root_agent_id or "") if tm.session is not None else ""
            # 保留 root agent 已入队未派发的那一条（keep_agent），其余排队任务弃子。
            await tm.abandon_pending(reason="pause_abandon", keep_agent=root_agent or None)
        for task_id, tokens in list(per.items()):
            if root_agent and tm is not None and tm.running_agent_of(task_id) == root_agent:
                tokens.pause.pause()
                # 在途 root run 即唯一续跑点：认领名额，闩锁窗口内此后派发的 root scope
                # 任务（如被 _try_resume_parent 重排的 SUSPENDED root 任务）born-cancel。
                self._pause_claimed.add(session_id)
            else:
                tokens.cancel.cancel()
        # 补 drain：把保留的 root 排队条目派发出去——它出生即 paused → act 首检查点 park 出唯一
        # 气泡，成为本次暂停的续跑点。对空队列 / 满并发是安全 no-op。放在信号循环后、兜底前。
        if tm is not None:
            await tm.drain()
        # 竞态兜底：信号发完会话已静止（root 恰好收尾、无可 park 对象）→ 立即清闩锁防残留。
        if tm is not None and tm.is_done():
            self._pausing.discard(session_id)
            self._pause_claimed.discard(session_id)
            tm.set_pause_abandon(False)
        return True

    def pause_task(self, session_id: str, task_id: str) -> bool:
        """定向暂停（spec 2026-07-05 §2.3）：pause 指定在途 task 的 run → 它在检查点 park
        自己的 wait 气泡，经多 pending 面板回复续跑。不在跑（无本 run 令牌）→ False。
        只停该 task 本身的 run，不涉及其子任务。"""
        tokens = self._run_tokens.get(session_id, {}).get(task_id)
        if tokens is None:
            return False
        tokens.pause.pause()
        return True

    async def cancel_session(self, session_id: str) -> bool:
        """硬取消：取消全部在途 run（per-run CancelToken）+ 全部后续 task（drain 队列）→ 会话 CANCELED。

        memory 保留。开新对话由调用方另起（新 /messages → 同 session_id 的 new run）。
        """
        per = self._run_tokens.get(session_id, {})
        task_manager = self._task_managers.get(session_id)
        if not per and task_manager is None:
            return False
        # 取消前判定会话是否已空闲挂起（无在跑任务）。RUNNING：在途 task 经 CancelToken→checkpoint
        # 协作取消→on_task_finished→is_done→_fire_session_done→_on_done 自行回收，故此处不抢着回收。
        idle = task_manager is not None and task_manager.is_done()
        if task_manager is not None:
            await task_manager.cancel_all(reason="user_cancel")
        for tokens in per.values():
            tokens.cancel.cancel()
        if idle:
            # 已暂停/中断（无在跑 task）的会话被取消：cancel_all 不经 _fire_session_done，_on_done
            # 不会触发，故显式回收 runtime 侧 per-session 状态（含较重的 TaskManager），避免滞留。
            self._release_session(session_id)
        return True

    # ── Phase 1 compat ───────────────────────────────────────────────────────

    async def run_single_task(
        self,
        *,
        session_id: str | None = None,
        template_id: str,
        user_prompt: str,
        tenant_id: str = "default",
        llm_account: str | None = None,
        llm_model: str | None = None,
    ) -> tuple[RunHandle, LoopState]:
        """Phase 1 compat: run a single task end-to-end and await completion."""
        import dataclasses as _dc

        sid = session_id or generate_id("ses")
        ctx = ProviderContext(session_id=sid, tenant_id=tenant_id)
        llm = self._resolve_llm(llm_account, llm_model)
        lm = LifecycleManager(template_resolver=self._template_resolver)

        agent, template = await lm.instantiate_agent(
            template_id=template_id, session_id=sid, tenant_id=tenant_id, ctx=ctx,
        )

        session = Session(
            id=sid,
            user_prompt=user_prompt,
            status="RUNNING",
            tenant_id=tenant_id,
            root_agent_id=agent.id,
            llm_provider=llm_account or "",
            created_at=now_utc(),
        )
        session.context_limit = llm.context_limit
        # 真实 client 必有 output_reserve；duck-type 桩缺失则保留 session 既有默认（8192）。
        _reserve = getattr(llm, "output_reserve", None)
        if _reserve is not None:
            session.reserved_output_tokens = _reserve
        agent = _dc.replace(agent, loop_guard=LoopGuard(
            context_limit=session.context_limit,
            reserved_output_tokens=session.reserved_output_tokens,
        ))
        task = Task(
            id=generate_id("tsk"),
            session_id=sid,
            status="ACTIVE",
            tenant_id=tenant_id,
            assigned_agent_id=agent.id,
            creator_agent_id=agent.id,
            title="User Request",
            description=user_prompt[:200],
            user_prompt=user_prompt,
            created_at=now_utc(),
        )

        task_manager = TaskManager(
            session_id=sid,
            event_bus=self._event_bus,
            max_concurrent=self._config.task_max_concurrent,
            task_max_retries=self._config.task_max_retries,
        )
        task_manager.set_session(session)
        task_manager.register_task(task)

        for p in self.providers.get_capability_providers():
            if isinstance(p, ControlCapabilityProvider):
                p.register_session(sid, task_manager, session)
                break
        try:
            state, handle = await self._execute_task(
                session=session,
                task=task,
                agent=agent,
                template=template,
                run_id=generate_id("run"),
                memory=self.providers.get_memory(),
                llm_account=llm_account,
                llm_model=llm_model,
                task_manager=task_manager,
            )
        finally:
            for p in self.providers.get_capability_providers():
                if isinstance(p, SessionScopedCapabilityProvider):
                    p.deregister_session(sid)
        return handle, state

    # ── Phase 4 full session ─────────────────────────────────────────────────

    async def start_session(self, params: SessionStartParams) -> RunHandle:
        """Create or resume a session and start execution.

        params.resume is False → new session (session_id=None → runtime generates it;
                                  session_id=<id> → new session with that host-provided ID).
        params.resume is True  → resume existing session (root_agent_id recovered from events).
        """
        memory = self.providers.get_memory()
        lm = LifecycleManager(template_resolver=self._template_resolver)
        sm = SessionManager(
            lifecycle_manager=lm,
            event_bus=self._event_bus,
            task_max_concurrent=self._config.task_max_concurrent,
            task_max_retries=self._config.task_max_retries,
            default_task_timeout_ms=self._config.default_task_timeout_ms,
        )

        if not params.resume:
            session, root_task, task_manager = await sm.create_session(
                template_id=params.template_id,
                user_prompt=params.user_prompt,
                tenant_id=params.tenant_id,
                llm_model=params.llm_model,
                llm_account=params.llm_account,
                initial_task_settings=params.initial_task_settings,
                session_id=params.session_id,
                context_limit=params.context_limit,
                token_budget=params.token_budget,
                reserved_output_tokens=params.reserved_output_tokens,
            )
        else:
            session, root_task, task_manager = await sm.resume_session(
                session_id=params.session_id,
                event_store=self.event_store,
                user_prompt=params.user_prompt,
                tenant_id=params.tenant_id,
                llm_model=params.llm_model,
                llm_account=params.llm_account,
                initial_task_settings=params.initial_task_settings,
            )

        run_id = generate_id("run")
        handle = RunHandle(
            run_id=run_id,
            session_id=session.id,
            task_id=root_task.id,
            agent_id=session.root_agent_id or "",
            template_id=params.template_id,
            event_bus=self._event_bus,
        )

        template = await self._template_resolver.get(
            params.template_id,
            version=None,
            ctx=ProviderContext(session_id=session.id, tenant_id=params.tenant_id),
        )
        task_manager.set_runner(self._make_task_runner(
            session=session,
            template=template,
            template_id=params.template_id,
            lm=lm,
            memory=memory,
            llm_account=params.llm_account,
            llm_model=params.llm_model,
            task_manager=task_manager,
            default_run_id=run_id,
            handle=handle,
        ))
        task_manager.set_session(session)

        self._register_and_drain(session, task_manager)
        return handle

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _register_and_drain(
        self,
        session: "Session",
        task_manager: "TaskManager",
    ) -> None:
        """Wire up ControlCapabilityProvider, set done callback, launch drain."""
        for p in self.providers.get_capability_providers():
            if isinstance(p, ControlCapabilityProvider):
                p.register_session(session.id, task_manager, session)
                break

        self._task_managers[session.id] = task_manager

        # 归属权谓词：多轮对话里每次 resume 都新建 TM 并覆盖此映射。旧 TM 的收尾若迟到
        # （被其慢的 background observe 拖住），必须认出自己已被顶替、变 no-op，否则会
        # 冲掉新一轮的会话状态（详见 TaskManager._is_current）。
        task_manager.set_is_current(
            lambda tm=task_manager: self._task_managers.get(session.id) is tm
        )
        # 完成判定的"未决 HITL"真相：查内存 HitlManager。有 parked（未决 HITL）任务时，
        # 会话算"空闲等应答"而非"完成"，避免另一任务收尾时把 parked 任务孤立（spec/07 §9.1）。
        task_manager.set_has_pending_hitl(
            lambda sid=session.id: bool(self.hitl_manager.list_pending(session_id=sid))
        )

        async def _on_done() -> None:
            # compare-and-clear：仅当本 TM 仍是当前 owner 才回收，避免顶替它的新 TM 被误释放。
            if self._task_managers.get(session.id) is task_manager:
                self._release_session(session.id)

        async def _on_idle() -> None:
            # per-run token 生命周期已随 run 对齐（execute finally 注销），无需在此回收。
            # 只清 pause 弃子闩锁；compare-and-check 防被顶替旧 TM 的迟到 idle 误清新一轮闩锁。
            if self._task_managers.get(session.id) is task_manager:
                self._pausing.discard(session.id)
                self._pause_claimed.discard(session.id)
                task_manager.set_pause_abandon(False)

        task_manager.set_session_done_callback(_on_done)
        task_manager.set_session_idle_callback(_on_idle)

        asyncio.create_task(task_manager.drain())

    def _release_session(self, session_id: str) -> None:
        """回收 runtime 侧全部 per-session 内存状态：per-run 令牌 registry 残余 + pause 闩锁 +
        TaskManager 映射 + scoped providers（fs workspace、control 的 TaskManager 注册等）。幂等。

        会话终结(_on_done) 或取消一个**已空闲挂起**的会话(cancel_session) 时调用——后者 cancel_all
        不经 on_task_finished/_fire_session_done，故不会自动触发 _on_done，须显式回收避免 TaskManager 滞留。
        """
        self._run_tokens.pop(session_id, None)
        self._pausing.discard(session_id)
        self._pause_claimed.discard(session_id)
        self._task_managers.pop(session_id, None)
        for _p in self.providers.get_capability_providers():
            if isinstance(_p, SessionScopedCapabilityProvider):
                _p.deregister_session(session_id)

    def _make_task_runner(
        self,
        *,
        session: Session,
        template: "AgentTemplate",
        template_id: str,
        lm: LifecycleManager,
        memory: MemoryProvider,
        llm_account: str | None,
        llm_model: str | None,
        task_manager: TaskManager,
        default_run_id: str,
        handle: "RunHandle | None" = None,
        pre_resolved_agents: dict[str, "Agent"] | None = None,
    ) -> "_SessionTaskRunner":
        """构造本 session/run 的两阶段 runner（原闭包工厂的显式化）。"""
        return _SessionTaskRunner(
            runtime=self, session=session, template=template, template_id=template_id,
            lm=lm, memory=memory, llm_account=llm_account, llm_model=llm_model,
            task_manager=task_manager,
            default_run_id=default_run_id, handle=handle,
            pre_resolved_agents=pre_resolved_agents,
        )

    # ── Crash recovery ───────────────────────────────────────────────────────

    async def recover_session(
        self,
        session_id: str,
        *,
        user_reply: "HitlRequest | None" = None,
        llm_account: str | None = None,
        llm_model: str | None = None,
        resumed_task_id: str | None = None,
    ) -> None:
        """Serialize resume per session, then reuse the live owner or rebuild + drain.

        单 owner 架构：若该 session 已有**存活的 owner TM** 且拥有被应答的 ``resumed_task_id``，
        就把应答作为消息投递给它、就地重驱（``_resume_in_existing_tm``），**不重建 TM**——从根上
        消除"多 TM 顶替/跨 TM 双跑"。仅当无存活 owner（真崩溃冷启动 / ``/resume`` / 活 TM 不含该
        task）才从事件日志重建。per-session 锁把整段过程串行化。
        """
        lock = self._resume_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            await self._recover_session_locked(
                session_id, user_reply=user_reply,
                llm_account=llm_account, llm_model=llm_model,
                resumed_task_id=resumed_task_id,
            )

    async def _recover_session_locked(
        self,
        session_id: str,
        *,
        user_reply: "HitlRequest | None" = None,
        llm_account: str | None = None,
        llm_model: str | None = None,
        resumed_task_id: str | None = None,
    ) -> None:
        """Reuse the live owner TM, or rebuild it from the event store, then resume.

        Called by the host on /resume (INTERRUPTED session) and internally on a cold
        HITL reply. Internally replays events (or loads snapshot + delta) to reconstruct
        Session/Task state. Raises RuntimeError with a descriptive message on failure.

        ``user_reply``: when a cold reply resolves an act plain-text pause (``wait_for_user``)
        HITL, reconcile cannot cover it (no dangling tool_call in the task layer), so the
        user's reply is injected here as a ``USER_PROMPT`` before drain — then the task
        re-enters act with the reply in the conversation.
        """
        # ── 复用活 owner（单 owner 架构主路径）─────────────────────────────────────
        # 冷 HITL 应答且已有存活 TM 拥有该 task → 就地重驱，不重建。避免每次冷应答造新 TM →
        # 顶替 → 跨 TM 双跑（根因 II）。/resume（无 resumed_task_id）与崩溃冷启动仍走重建。
        existing = self._task_managers.get(session_id)
        if (resumed_task_id is not None and existing is not None
                and existing.is_alive() and existing.get_task(resumed_task_id) is not None):
            await self._resume_in_existing_tm(
                existing, user_reply=user_reply,
                llm_account=llm_account, llm_model=llm_model,
                resumed_task_id=resumed_task_id,
            )
            return

        from ctx_weft.core.control.reducers import rebuild_view
        view = await rebuild_view(self.event_store, session_id)
        sess_proj = view.sessions.get(session_id)
        if sess_proj is None:
            raise RuntimeError(f"Session {session_id!r} not found in event store")

        template_id = sess_proj.template_id
        if not template_id:
            raise RuntimeError(f"Session {session_id!r} has no template_id — cannot recover")

        from ctx_weft.core.control.converters import session_from_projection, task_from_projection
        session = session_from_projection(sess_proj)
        # 调用方（host /resume）传入当前所选 LLM 时覆盖投影里的原始 model：用户改了 model 后
        # 续跑须用新 model，而非 SessionCreated 记录的旧 model（投影不随重配更新）。
        if llm_account is not None:
            session.llm_provider = llm_account
        if llm_model is not None:
            session.llm_model = llm_model
        if llm_account is not None or llm_model is not None:
            # 换模型恢复：窗口参数须随新模型，否则 CONTEXT_OVERFLOW 挂起换大模型也照旧溢出
            self._sync_session_llm_window(session)
        all_tasks = [task_from_projection(tp) for tp in view.tasks.values()]

        # 重建内存 HitlManager（_futures 空 → 后续应答自动走冷 resume；spec/07 §9）
        if view.pending_hitl:
            self.hitl_manager.rebuild_pending(view.pending_hitl)
        # 有未解决 pending HITL 的 task：restore 时保持 parked、不重排（spec/07 §9.1）
        parked_task_ids = {h.task_id for h in view.pending_hitl.values() if h.task_id}

        _TERMINAL = {"FINISHED", "FAILED", "CANCELED"}
        terminal_ids = {t.id for t in all_tasks if t.status in _TERMINAL}
        resumable = [t for t in all_tasks if t.status not in _TERMINAL]

        # 折出被崩溃打断的段 recap（started 无 done）——覆盖全部 observe 段边界。
        from ctx_weft.core.control.reducers import fold_pending_task_recap
        events_all = await self.event_store.read_by_session(session_id)
        pending_recap = fold_pending_task_recap(events_all)

        # 既无可恢复 task 又无 task（空/损坏投影）→ 确无事可做，保留原抛错。
        if not resumable and not all_tasks:
            raise RuntimeError(f"Session {session_id!r} has no resumable tasks")

        lm = LifecycleManager(template_resolver=self._template_resolver)
        template = await self._template_resolver.get(
            template_id, version=None,
            ctx=ProviderContext(session_id=session.id, tenant_id=session.tenant_id),
        )
        task_manager = TaskManager(
            session_id=session.id,
            event_bus=self._event_bus,
            max_concurrent=self._config.task_max_concurrent,
            task_max_retries=self._config.task_max_retries,
        )
        task_manager.set_session(session)
        # 方案 II：若已有活 TM 正在跑（如崩溃后多次冷应答、或进程内 idle-park 后应答），它被本次
        # 顶替后 drain 会停（_is_current），但其已派发、在跑的协程仍会跑完。新 TM 不得重排这些
        # 在跑任务，否则同一 task 跨 TM 双跑。把它们并入 restore 的"不派发"集合，交由旧 TM 收尾。
        existing_tm = self._task_managers.get(session.id)
        inflight = existing_tm.running_task_ids() if existing_tm is not None and existing_tm.is_alive() else set()
        if inflight:
            logger.info("Recovery: session %s has a live TM running %s; new TM will not re-dispatch them",
                        session.id, sorted(inflight))
        task_manager.restore(all_tasks, terminal_ids, parked_task_ids=parked_task_ids | inflight)

        pre_resolved = {
            av.id: Agent(
                id=av.id,
                session_id=session.id,
                template_id=template_id,
                template_version=template.version,
                status="IDLE",
                tenant_id=session.tenant_id,
                spawn_depth=av.spawn_depth,
                parent_agent_id=av.parent_agent_id,
            )
            for av in view.agents.values()
        }

        task_manager.set_runner(self._make_task_runner(
            session=session,
            template=template,
            template_id=template_id,
            lm=lm,
            memory=self.providers.get_memory(),
            llm_account=session.llm_provider,
            llm_model=session.llm_model,
            task_manager=task_manager,
            default_run_id=generate_id("run"),
            pre_resolved_agents=pre_resolved,
        ))
        # act 纯文本暂停（wait_for_user）冷应答：把用户回复注入 task 层并重排（reconcile 覆盖不到,见上）。
        if user_reply is not None:
            await self._inject_user_reply(user_reply, session, task_manager)

        # 重跑被崩溃打断的段 recap（登记到新 TM；close 边界补 register_close_synth 替换占位 finish 对）。
        tasks_by_id = {t.id: t for t in all_tasks}
        for tid, info in pending_recap.items():
            t = tasks_by_id.get(tid)
            if t is None:
                continue
            await self._relaunch_task_recap(
                session=session, template=template, template_id=template_id,
                task_manager=task_manager, task=t,
                agent_id=info.get("agent_id") or t.assigned_agent_id or "",
                boundary=info.get("boundary") or "finish",
            )

        self._register_and_drain(session, task_manager)

        # 无可恢复 task（所有 task 已终态）但 session 因崩溃未落终态 → 显式收尾：
        # gather 重跑的后台 recap 后发 SESSION_FINISHED（终态镜像 on_task_finished）。
        if not resumable:
            final_status = "FAILED" if session.failure_counter > 0 else "SUCCEEDED"
            await task_manager.finalize_idle_session(final_status)

    async def _find_finish_pair_tool_call_id(
        self, memory: MemoryProvider, scope: MemoryScope, task_id: str, pctx: ProviderContext,
    ) -> str | None:
        """从 memory 找该 task close 时写的占位 finish 对 assistant turn，返回其 finish_task tool_call id。"""
        from ctx_weft.protocols import MemoryEventType
        fin = qualify("control:finish_task")
        turns = await memory.recall_recent(scope, [MemoryEventType.AGENT_CONVERSATION_TURN], 500, pctx)
        for r in turns:
            if r.role == "assistant" and r.metadata.get("origin_task_id") == task_id:
                for tc in (r.metadata.get("tool_calls") or []):
                    if tc.get("name") == fin and tc.get("id"):
                        return tc["id"]
        return None

    async def _relaunch_task_recap(
        self, *, session: Session, template: "AgentTemplate", template_id: str,
        task_manager: TaskManager, task: Task, agent_id: str, boundary: str,
    ) -> None:
        """恢复：重建 LoopState 重跑一个被崩溃打断的段 recap，登记到传入 TM（track_background）。

        close 边界（finish/normal）：先从 memory 读占位 finish 对 tool_call_id + 据 task 状态定 outcome，
        register_close_synth，使重跑经 _replace_finish_report 替换占位对。best-effort：任何一步失败记日志、跳过。
        """
        from ctx_weft.core.loop.steps.background_observe import _CLOSE_BOUNDARIES
        try:
            lm = LifecycleManager(template_resolver=self._template_resolver)
            pctx0 = ProviderContext(session_id=session.id, tenant_id=session.tenant_id)
            agent, _tmpl = await lm.instantiate_agent(
                template_id=template_id,
                session_id=session.id, tenant_id=session.tenant_id,
                existing_agent_id=agent_id, ctx=pctx0,
            )
            memory = self.providers.get_memory()
            scope = MemoryScope(session_id=session.id, task_id=task.id, agent_id=agent.id)
            provider_ctx = self._build_provider_ctx(session, task, agent)
            skill_index = self._skill_provider_index()
            assembler = self._build_assembler(memory, provider_ctx, skill_index)
            gateway = self._build_gateway(memory)
            llm = self._resolve_llm(session.llm_provider, session.llm_model)
            loop_ctx = self._build_loop_ctx(
                assembler, llm, memory, provider_ctx, gateway, skill_index, None, task_manager,
            )
            state = LoopState(
                run_id=generate_id("run"), session=session, task=task, agent=agent,
                scope=scope, extra={"template": template},
            )
            if boundary in _CLOSE_BOUNDARIES:
                tcid = await self._find_finish_pair_tool_call_id(memory, scope, task.id, provider_ctx)
                if tcid is not None:
                    outcome = "fail" if task.status == "FAILED" else "success"
                    register_close_synth(task.id, tcid, scope, outcome)
            launch_background_observe(state, loop_ctx, boundary=boundary)
        except Exception:
            logger.exception("recover: failed to relaunch task recap for task=%s", task.id)

    async def _resume_in_existing_tm(
        self,
        tm: "TaskManager",
        *,
        user_reply: "HitlRequest | None",
        llm_account: str | None,
        llm_model: str | None,
        resumed_task_id: str,
    ) -> None:
        """把冷 HITL 应答作为消息投递给**存活的 owner TM**，就地重驱——不重建 TM（单 owner 架构）。

        - model = 会话状态：把本轮所选 model 写回 owner 的 session，下次 dispatch 经 run_task seam 生效。
        - 控制令牌随 run 在派发时发放（per-run registry），无需在此重建。
        - wait_for_user 冷应答注入用户回复到 task 层；approval 走 reconcile。
        - 重排被应答的 task 并重新 drain（``_register_and_drain`` 对同一 TM 幂等：重挂回调 + 派发）。
        """
        session = tm.session
        if session is None:  # 防御：存活 owner 一定注入过 session
            raise RuntimeError("live TaskManager has no session — cannot resume in place")
        if llm_account is not None:
            session.llm_provider = llm_account
        if llm_model is not None:
            session.llm_model = llm_model
        if llm_account is not None or llm_model is not None:
            # 同 recover_session：换模型就地续跑也要对齐窗口参数
            self._sync_session_llm_window(session)
        if user_reply is not None:
            await self._inject_user_reply(user_reply, session, tm)
        tm.resume_task(resumed_task_id)
        self._register_and_drain(session, tm)

    async def compact_session(
        self,
        session_id: str,
        *,
        agent_id: str | None = None,
        task_id: str = "",
    ) -> dict[str, str]:
        """Run a one-shot, compact-only operation over an IDLE session's memory.

        Folds the agent layer (dispatch log) of ``agent_id`` (default: the session
        root agent). Pass a real ``task_id`` to also make that task's task layer
        eligible. Raises ``SessionBusyError`` if the session is currently running.

        Calls ``CompactStep.execute`` directly (no step driver / no Run lifecycle
        events), so the session's projection status is untouched — only
        MemoryCompactStarted / MemoryCompacted are emitted (both reducer no-ops).

        Returns ``{"session_id", "agent_id", "task_id"}``. When ``task_id`` is not
        supplied, the returned ``task_id`` is a transient in-memory carrier id with no
        event-store record (it only scopes the fold); callers should not try to look it up.

        Note: a concurrent ``pause_session`` while a compact is in flight is not
        honoured mid-compact — ``CompactStep`` does not poll the pause token — but the
        idle-guard still prevents a new compact/drain from starting on this session.
        """
        import dataclasses as _dc

        from ctx_weft.core.control.converters import session_from_projection
        from ctx_weft.core.control.reducers import rebuild_view
        from ctx_weft.core.errors import SessionBusyError
        from ctx_weft.core.loop.steps.compact import CompactStep
        from ctx_weft.core.orchestrator.lifecycle_manager import LifecycleManager
        from ctx_weft.core.state.models import LoopGuard, NormalTaskSettings, Task
        from ctx_weft.protocols import MemoryScope, ProviderContext

        # ── idle-guard: claim the slot synchronously (no await before the claim) ──
        if session_id in self._busy_sessions or self._run_tokens.get(session_id):
            raise SessionBusyError(session_id)
        self._busy_sessions.add(session_id)
        token = CancelToken()
        try:
            view = await rebuild_view(self.event_store, session_id)
            proj = view.sessions.get(session_id)
            if proj is None:
                raise RuntimeError(f"Session {session_id!r} not found in event store")
            if not proj.template_id:
                raise RuntimeError(f"Session {session_id!r} has no template_id — cannot compact")

            session = session_from_projection(proj)
            target_agent_id = agent_id or session.root_agent_id
            if not target_agent_id:
                raise RuntimeError(f"Session {session_id!r} has no agent to compact")

            lm = LifecycleManager(template_resolver=self._template_resolver)
            pctx = ProviderContext(session_id=session.id, tenant_id=session.tenant_id)
            agent, template = await lm.instantiate_agent(
                template_id=proj.template_id,
                session_id=session.id,
                tenant_id=session.tenant_id,
                existing_agent_id=target_agent_id,
                ctx=pctx,
            )
            # 手动 compact_session 是「强制立即压」的一次性操作，不受预算门控（escalating_compact
            # 按 token_estimate vs target_tokens 判断是否需要压）——context_tokens=context_limit
            # 使门总是打开，交给各级内部的可折性判断决定实际动多少。
            agent = _dc.replace(
                agent,
                loop_guard=LoopGuard(
                    context_limit=session.context_limit,
                    context_tokens=session.context_limit,
                    reserved_output_tokens=session.reserved_output_tokens,
                ),
                runtime={"llm_model": session.llm_model or ""},
            )

            task = Task(
                id=task_id or generate_id("tsk"),
                session_id=session.id,
                status="ACTIVE",
                tenant_id=session.tenant_id,
                assigned_agent_id=agent.id,
                creator_agent_id=agent.id,
                settings=NormalTaskSettings(),
                user_prompt_in_memory=True,  # nothing to ingest
                created_at=now_utc(),
            )

            memory = self.providers.get_memory()
            provider_ctx = self._build_provider_ctx(session, task, agent)
            skill_index = self._skill_provider_index()
            assembler = self._build_assembler(memory, provider_ctx, skill_index)
            gateway = self._build_gateway(memory)
            llm = self._resolve_llm(session.llm_provider, session.llm_model)
            loop_ctx = self._build_loop_ctx(
                assembler, llm, memory, provider_ctx, gateway, skill_index, token, None,
            )

            scope = MemoryScope(session_id=session.id, task_id=task.id, agent_id=agent.id)
            state = LoopState(
                run_id=generate_id("run"),
                session=session,
                task=task,
                agent=agent,
                scope=scope,
                extra={"template": template},
            )
            outcome = await CompactStep().execute(state, loop_ctx)
            for ev in outcome.events:
                await self._event_bus.emit(ev)

            return {"session_id": session.id, "agent_id": agent.id, "task_id": task.id}
        finally:
            self._busy_sessions.discard(session_id)

    async def _resume_after_cold_hitl(self, req: "HitlRequest") -> None:
        """冷 HITL 应答后恢复 session（HitlManager.on_cold_resolve 回调）。

        act 的纯文本暂停（form=wait）须把回复注入 task 层（reconcile 覆盖不到——它不在
        task 层留 dangling tool_call）；act 的 ``ask_user`` / approval 走 reconcile,不在此注入。
        """
        is_inject = req.form == "wait"
        # 应答携带的当前所选模型（host 据 entry 传入）覆盖投影里的旧 model：用户改 model 后
        # 冷续跑须用新 model。未携带（None）时 recover_session 回退投影。
        # resumed_task_id：被应答的 task——若存活 owner 拥有它，recover_session 就地重驱不重建。
        await self.recover_session(
            req.session_id,
            user_reply=req if is_inject else None,
            llm_account=req.resume_llm_account,
            llm_model=req.resume_llm_model,
            resumed_task_id=req.task_id,
        )

    async def _inject_user_reply(
        self, req: "HitlRequest", session: Session, task_manager: TaskManager,
    ) -> None:
        """把 act 纯文本暂停（wait_for_user）的用户回复作为 USER_PROMPT 注入 task 层 + 重排。"""
        from ctx_weft.protocols import MemoryEvent, MemoryEventType
        from ctx_weft.core.loop.steps.background_observe import await_pending_background_observe

        target = task_manager.get_task(req.task_id)
        if target is None:
            logger.warning("wait_for_user cold resume: task %s not found for HITL %s", req.task_id, req.id)
            return

        # 强一致屏障：上一轮 plain_text/interrupt park 甩出的后台 observe（fire-and-forget 段折叠）
        # 可能仍在跑。先等它落库，再注入本轮 USER_PROMPT——保证折叠摘要的时间戳早于新消息，
        # 否则迟到的摘要会越到新消息之后、令下一轮装配误判「续跑」并埋掉新输入（见
        # background_observe.apply_compact 的段尾锚点 + composer 续跑 cue）。同进程有在跑 fold 才等；
        # 真崩溃冷启动 _task_pending 为空 → no-op。
        await await_pending_background_observe(req.task_id)

        # agent_id 必须是本 task 对话真正所在的 agent scope——AgentRecallSource 用
        # recall_recent_by_agent 按 scope.agent_id 过滤召回 task body（≠ recall_recent 的 task_id 键）。
        # 冷重启从事件日志重建的 HITL 丢了 agent_id（HITL_REQUIRED 投影未持久化它，见 reducers），
        # req.agent_id="" 会把回复写进空 agent scope → 对 actor 装配不可见 → 续跑 cue → 空白回复
        # （重启后「第一句」丢失）。回退到 task 的真实 agent（assigned/creator），与首条 USER_PROMPT
        # 落库时同 scope。
        agent_id = req.agent_id or target.assigned_agent_id or target.creator_agent_id or ""
        scope = MemoryScope(session_id=session.id, task_id=target.id, agent_id=agent_id)
        pctx = ProviderContext(
            session_id=session.id, tenant_id=session.tenant_id,
            task_id=target.id, agent_id=agent_id,
        )
        if req.status == "rejected":
            content = f"Human declined: {req.message}" if req.message else "Human rejected the request."
        else:
            content = req.message or "(no response)"
            # ① 中途打断（未吐 token）续接：补「上一条请求已取消」说明（context=interrupt:edit）。
            if req.context == "interrupt:edit":
                from ctx_weft.core.loop.steps.act import interrupt_edit_note
                prev = await self._last_user_prompt(scope, pctx)
                content = interrupt_edit_note(prev, content)
        await self.providers.get_memory().ingest(
            MemoryEvent(
                type=MemoryEventType.USER_PROMPT,
                scope=scope,
                content=content,
                timestamp=now_utc(),
                role="user",
                metadata={"task_id": target.id, "source": "hitl_reply"},
            ),
            pctx,
        )
        # 清旧进展、置 PENDING（restore 已重排,这里保证状态正确）。
        target.outputs = None
        target.process_report = None
        target.process_report_at = None
        if target.status not in ("FINISHED", "FAILED", "CANCELED"):
            target.status = "PENDING"

    async def _last_user_prompt(self, scope: MemoryScope, pctx: ProviderContext) -> str:
        """取 scope 内最近一条 USER_PROMPT 内容（供 ① 打断续接的「上一条取消」说明）。"""
        from ctx_weft.protocols import MemoryEventType
        try:
            recs = await self.providers.get_memory().recall_recent(
                scope, [MemoryEventType.USER_PROMPT], 1, pctx,
            )
        except Exception:
            return ""
        return (recs[-1].content or "") if recs else ""

    async def recover(self) -> int:
        """Recover every still-active session (SessionCreated, no SessionFinished) after a restart.

        Decision is made **in core, from events** (no host projection, no full replay):
        a session with an unresolved pending HITL was waiting for a human answer → **only the
        in-memory HitlManager is rebuilt** (so ``/hitl/pending`` and the reply endpoints work);
        status stays PAUSED_HITL or PAUSED and **nothing runs** — the task rebuild + drain defers to the
        reply's ``recover_session``. Otherwise it was actively running at crash → **emit
        ``SessionStatusChanged(INTERRUPTED)``**.

        So at startup **nothing drains/runs**: PAUSED waits for a reply, INTERRUPTED waits for
        ``/resume``. No host callback — the interrupt is just an event handled by the host's
        existing subscribers (projection + SSE). Call in the app lifespan after providers are
        registered, before serving. Returns the count handled.
        """
        try:
            session_ids = await self.event_store.list_active_session_ids()
        except NotImplementedError:
            logger.warning("Recovery: EventStore does not support list_active_session_ids — skipped")
            return 0

        for session_id in session_ids:
            try:
                n = await self.rebuild_hitl(session_id)
                if n:
                    # 有未决 HITL → 如实反映"等待人工"（否则投影停在崩溃前的 RUNNING，看着在跑却卡住）。
                    # wait-only（纯文本软待命）= PAUSED、其余 = PAUSED_HITL——与 SESSION_PAUSED_HITL
                    # 的 reducer/投影语义一致（form=wait 无 HITL 面板，误标会让前端等一个不存在的面板）。
                    pend = self.hitl_manager.list_pending(session_id=session_id)
                    status = "PAUSED" if pend and all(r.form == "wait" for r in pend) else "PAUSED_HITL"
                    await self._emit_session_status(session_id, status)
                    logger.info("Recovery: session %s → %s (%d pending, drain deferred to reply)", session_id, status, n)
                else:
                    await self._emit_session_interrupted(session_id)
                    logger.info("Recovery: session %s → INTERRUPTED (event emitted)", session_id)
            except Exception:
                logger.exception("Recovery: failed to recover session %s", session_id)

        return len(session_ids)

    async def rebuild_hitl(self, session_id: str) -> int:
        """从事件重建该 session 的内存 pending HITL（仅折叠 HITL 类事件,不 drain）,返回 pending 条数。

        幂等,可重复调用。启动 `recover` 用它重建 PAUSED 会话;应答入口也可在内存为空时按需自愈
        （重启后内存 HitlManager 还没被 recover 填上时,据事件即时重建,避免应答 404；spec/07 §9）。
        """
        pending = await self._pending_hitl(session_id)
        if pending:
            self.hitl_manager.rebuild_pending(pending)
        return len(pending)

    async def rebuild_all_pending_hitl(self) -> int:
        """据事件重建**所有 active session** 的内存 pending HITL（不发中断、不 drain）,返回总条数。

        供只带 hitl_id 的应答入口（`/hitl/{id}/*`）自愈:重启后内存 HitlManager 为空、又无 session_id
        可定位时,重建全部 active pending 后即可按 id 命中。仅在 miss 时调用,成本有界（spec/07 §9）。
        """
        try:
            session_ids = await self.event_store.list_active_session_ids()
        except NotImplementedError:
            return 0
        total = 0
        for sid in session_ids:
            try:
                total += await self.rebuild_hitl(sid)
            except Exception:
                logger.exception("rebuild_all_pending_hitl: failed for session %s", sid)
        return total

    async def _emit_session_interrupted(self, session_id: str, reason: str | None = None) -> None:
        """发 SessionStatusChanged(INTERRUPTED) —— host 读模型(投影/SSE)按事件自行反映,不走回调。

        reason 标记中断成因（如 "llm_outage"）供前端区分 LLM 故障中断 vs 通用中断(重启等)。
        """
        payload: dict = {"new_status": "INTERRUPTED"}
        if reason:
            payload["reason"] = reason
        await self._event_bus.emit(Event(
            id=generate_id("evt"),
            run_id=None,
            sequence=0,
            session_id=session_id,
            type=EventType.SESSION_STATUS_CHANGED,
            timestamp=now_utc(),
            payload=payload,
        ))

    async def _emit_session_status(self, session_id: str, new_status: str) -> None:
        """发 SessionStatusChanged(new_status) —— host 读模型据事件反映，不走回调。

        供恢复时把有未决 HITL 的会话如实标为 PAUSED_HITL（否则投影停在崩溃前的 RUNNING）。
        """
        await self._event_bus.emit(Event(
            id=generate_id("evt"),
            run_id=None,
            sequence=0,
            session_id=session_id,
            type=EventType.SESSION_STATUS_CHANGED,
            timestamp=now_utc(),
            payload={"new_status": new_status},
        ))

    async def _pending_hitl(self, session_id: str) -> dict:
        """该 session 仍未解决的 pending HITL（{id: HitlRequest}）—— 仅折叠 HITL 类事件,不全量回放。"""
        from ctx_weft.core.control.reducers import HITL_STATUS_EVENT_TYPES, fold_pending_hitl
        try:
            events = await self.event_store.read_session_events_of_types(session_id, HITL_STATUS_EVENT_TYPES)
        except NotImplementedError:
            # 退化（极简 EventStore 未实现轻查询）：全量读后内存过滤,仍正确、只是不省。
            events = [e for e in await self.event_store.read_by_session(session_id)
                      if e.type in HITL_STATUS_EVENT_TYPES]
        return fold_pending_hitl(events)

    async def _cold_hitl_decision(self, session_id: str, tool_call_id: str):
        """冷决定查询（HitlManager 绑定）：从事件日志折出该 tool_call 的可用人工决定。

        reconcile 短路门控的跨重启回落——内存决定缓存重启后只重建 pending、不含已解决,
        不查日志就会把已答过的问题重新问一遍、丢掉答案（spec/07 §6）。仅折 HITL 类事件。
        """
        from ctx_weft.core.control.reducers import HITL_STATUS_EVENT_TYPES, fold_cold_hitl_decision
        try:
            events = await self.event_store.read_session_events_of_types(session_id, HITL_STATUS_EVENT_TYPES)
        except NotImplementedError:
            events = [e for e in await self.event_store.read_by_session(session_id)
                      if e.type in HITL_STATUS_EVENT_TYPES]
        return fold_cold_hitl_decision(events, tool_call_id)

    # ── Internal execution ───────────────────────────────────────────────────

    def _build_provider_ctx(self, session: Session, task: Task, agent: Agent) -> ProviderContext:
        return ProviderContext(
            session_id=session.id,
            tenant_id=session.tenant_id,
            task_id=task.id,
            agent_id=agent.id,
            timestamp=now_utc(),
            skill_name=task.settings.skill_name if isinstance(task.settings, NormalTaskSettings) else "",
        )

    def _skill_provider_index(self) -> dict:
        # provider_name → SkillCapabilityProvider，供 PrepareStep 加载 Level2 capabilities
        return {
            p.name: p
            for p in self.providers.get_capability_providers()
            if isinstance(p, SkillCapabilityProvider)
        }

    def _build_assembler(
        self,
        memory: MemoryProvider,
        provider_ctx: ProviderContext,
        skill_index: dict,
    ) -> ContextAssembler:
        deps = AssemblerDeps(
            memory=memory,
            knowledge_providers=self.providers.get_knowledge_providers(),
            provider_ctx=provider_ctx,
            skill_provider_index=skill_index,
            capability_provider_index={
                p.name: p for p in self.providers.get_capability_providers()
            },
        )
        return ContextAssembler(
            sources=[
                IdentitySource(),
                CapabilitySource(),
                TaskSpecSource(),
                AgentRecallSource(),
                BlackboardSource(),
                SemanticRecallSource(),
                KnowledgeRetrievalSource(),
                GuidanceSource(),
            ],
            budget=PriorityBudgetStrategy(),
            composer=DefaultComposer(),
            deps=deps,
        )

    def _build_gateway(self, memory: MemoryProvider) -> CapabilityGateway:
        return CapabilityGateway(
            capability_cache=self._capability_cache,
            capability_providers=self.providers.get_capability_providers(),
            memory=memory,
            event_bus=self._event_bus,
            provider_authorizers=self.providers.get_capability_authorizers(),
            spill_threshold=self._config.spill_threshold,
            spill_preview_chars=self._config.spill_preview_chars,
        )

    def _build_loop_ctx(
        self,
        assembler: ContextAssembler,
        llm: LLMClient,
        memory: MemoryProvider,
        provider_ctx: ProviderContext,
        gateway: CapabilityGateway,
        skill_index: dict,
        cancel_token: CancelToken | None,
        task_manager: "TaskManager | None",
        pause_token: "PauseToken | None" = None,
    ) -> LoopContext:
        return LoopContext(
            assembler=assembler,
            llm=llm,
            memory=memory,
            event_bus=self._event_bus,
            provider_ctx=provider_ctx,
            capability_cache=self._capability_cache,
            capability_providers=self.providers.get_capability_providers(),
            capability_gateway=gateway,
            template_resolver=self._template_resolver,
            skill_provider_index=skill_index,
            cancel_token=cancel_token,
            task_manager=task_manager,
            hitl_manager=self.hitl_manager,
            pause_token=pause_token,
            config=self._config,
        )

    @staticmethod
    def _build_step_driver(initial_step: str) -> StepDriver:
        return StepDriver(
            steps={
                "prepare": PrepareStep(),
                "act": ActStep(),
                "observe": ObserveStep(),
                "finalize": FinalizeStep(),
                "suspend": SuspendStep(),
                "compact": CompactStep(),
                "recognize_intent": RecognizeIntentStep(),
                "reconcile": ReconcileStep(),
            },
            initial_step=initial_step,
        )

    async def _run_loop(
        self,
        state: LoopState,
        loop_ctx: LoopContext,
        driver: StepDriver,
        run_id: str,
        initial_step: str,
        task: Task,
        agent: Agent,
    ) -> LoopState:
        """Execute the step driver loop; emit lifecycle events; return final state.

        Raises on non-retriable errors (after emitting RunFinished).
        """
        await self._event_bus.emit(make_event(state, EventType.RUN_STARTED, payload={
            "run_id": run_id,
            "initial_step": initial_step,
        }))

        run_error: BaseException | None = None
        was_cancelled = False
        try:
            async for outcome in driver.run(state, loop_ctx):
                if outcome.state_patch:
                    state = state.apply_patch(outcome.state_patch)
        except HitlPark:
            # 热→冷降级 / 显式挂起：干净挂起，不算失败。run_error 保持 None →
            # finally 发 RUN_FINISHED(SUSPENDED, will_retry=False)，与委派挂起同形；
            # _run_task 据 task.status==SUSPENDED 走挂起分支（不 requeue）。
            if task.status not in ("FINISHED", "FAILED", "CANCELED"):
                task.status = "SUSPENDED"
                # Phase 3：补发 TASK_SUSPENDED，使 task 投影状态 = 内存状态(SUSPENDED)。冷 park 此前
                # 只改内存、不发此事件 → 投影停在 ACTIVE，与"在等人"脱节（restore/host UI 都被误导）。
                # parked-set 保证 restore 不会据此错误重排（spec/07 §9.1）；与委派挂起(SuspendStep)同形。
                await self._event_bus.emit(make_event(
                    state, EventType.TASK_SUSPENDED, payload={"reason": "hitl_park"},
                ))
            logger.info("_run_loop: task %s parked on HITL", task.id)
        except asyncio.CancelledError:
            was_cancelled = True
            if task.status not in ("FINISHED", "FAILED", "CANCELED"):
                task.status = "CANCELED"
        except LLMOutageError as exc:
            # 瞬时 LLM 故障自愈耗尽 / 中途断流 → 可恢复中断，**不是** task 失败。
            # task 置 SUSPENDED（非终态，与 HitlPark 同形）→ _run_task 走挂起分支不判 FINISHED，
            # restore() 在 /resume 时据非终态重排；不发 TASK_FAILED；不增 failure_counter；
            # run_error 保持 None → finally 不再抛出（不经 _handle_task_failure）。
            if task.status not in ("FINISHED", "FAILED", "CANCELED"):
                task.status = "SUSPENDED"
            logger.warning("_run_loop: task %s interrupted by LLM outage: %s", task.id, exc)
            await self._emit_session_interrupted(state.session.id, reason="llm_outage")
        except Exception as exc:
            run_error = exc
            # 运行层崩溃 = 可恢复中断的临时标记（非终态）：re-raise 交 _handle_task_failure
            # 定夺——原地重试（翻回 PENDING）或挂起等 /resume（保持 SUSPENDED + 发事件）。
            # 真失败只有 observer 判 fail 一条路（FinalizeStep 闭合胶囊、回传父亲）。
            # ContextOverflowError 不再特判终态：retriable=False 使其跳过重试直接挂起，
            # 溢出文案随 task.error / TASK_SUSPENDED.error_message 抵达 host（提示换大窗口模型）。
            if task.status not in ("FINISHED", "FAILED", "CANCELED"):
                task.status = "SUSPENDED"
                task.error = str(exc)
            if getattr(exc, "retriable", False):
                logger.warning("_run_loop: task %s failed (retriable): %s", task.id, exc)
            else:
                logger.exception("_run_loop: run failed for task %s", task.id)
        finally:
            self._capability_cache.evict(agent.id)
            if was_cancelled:
                await self._event_bus.emit(make_event(state, EventType.RUN_CANCELED, payload={"run_id": run_id}))
                await self._event_bus.emit(make_event(state, EventType.TASK_CANCELED, payload={}))
            # will_retry=True suppresses SSE close on the host side.
            # cancelled → False; retriable=False → TaskManager won't retry anyway.
            will_retry = (
                run_error is not None
                and task.retry_count < task.max_retries
                and getattr(run_error, "retriable", True)
            )
            await self._event_bus.emit(make_event(state, EventType.RUN_FINISHED, payload={
                "final_status": task.status,
                "will_retry": will_retry,
                "total_events": state.sequence_counter,
                "total_turns": len(state.transcript),
                "error": str(run_error) if run_error else None,
                "error_type": type(run_error).__name__ if run_error else None,
            }))

        if run_error is not None:
            raise run_error

        return state

    async def _execute_task(
        self,
        session: Session,
        task: Task,
        agent: Agent,
        template: AgentTemplate,
        run_id: str,
        memory: MemoryProvider,
        llm_account: str | None = None,
        llm_model: str | None = None,
        cancel_token: CancelToken | None = None,
        pause_token: "PauseToken | None" = None,
        initial_step: str = "prepare",
        task_manager: "TaskManager | None" = None,
        scope_agent_id: str | None = None,
    ) -> tuple[LoopState, RunHandle]:
        provider_ctx = self._build_provider_ctx(session, task, agent)
        skill_index = self._skill_provider_index()
        assembler = self._build_assembler(memory, provider_ctx, skill_index)
        gateway = self._build_gateway(memory)
        llm = self._resolve_llm(llm_account, llm_model)
        loop_ctx = self._build_loop_ctx(assembler, llm, memory, provider_ctx, gateway, skill_index, cancel_token, task_manager, pause_token=pause_token)

        scope = MemoryScope(session_id=session.id, task_id=task.id, agent_id=scope_agent_id or agent.id)
        state = LoopState(
            run_id=run_id,
            session=session,
            task=task,
            agent=agent,
            scope=scope,
            extra={"template": template},
        )
        driver = self._build_step_driver(initial_step)
        state = await self._run_loop(state, loop_ctx, driver, run_id, initial_step, task, agent)
        handle = RunHandle(
            run_id=run_id,
            session_id=session.id,
            task_id=task.id,
            agent_id=agent.id,
            template_id=template.id,
            event_bus=self._event_bus,
            _state=state,
        )
        return state, handle


class _SessionTaskRunner:
    """两阶段 TaskRunner（每个 owner-TM 一个实例）：assemble 装配执行 agent，execute 驱动 step loop。

    原 _make_task_runner 闭包的显式化：闭包捕获 → 实例字段；_resolved_agents
    闭包缓存 → 实例属性（恢复播种 = 构造参数 pre_resolved_agents）。
    assigned_agent_id 回填 / started_at / TASK_STARTED 均归 TaskManager（两阶段契约）。
    """

    def __init__(
        self,
        *,
        runtime: "CtxWeftRuntime",
        session: Session,
        template: "AgentTemplate",
        template_id: str,
        lm: LifecycleManager,
        memory: MemoryProvider,
        llm_account: str | None,
        llm_model: str | None,
        task_manager: TaskManager,
        default_run_id: str,
        handle: "RunHandle | None" = None,
        pre_resolved_agents: dict[str, "Agent"] | None = None,
    ) -> None:
        self._runtime = runtime
        self._session = session
        self._template = template
        self._template_id = template_id
        self._lm = lm
        self._memory = memory
        self._llm_account = llm_account
        self._llm_model = llm_model
        self._task_manager = task_manager
        self._default_run_id = default_run_id
        self._handle = handle
        self._resolved_agents: dict[str, Agent] = dict(pre_resolved_agents or {})

    # ── 阶段 1：装配 ─────────────────────────────────────────────────────────

    async def assemble(self, task_id: str) -> "AgentBinding | None":
        """决定并实例化执行 agent + memory 预备 + reconcile 探测（原 _resolve）。"""
        import dataclasses as _dc

        t = self._task_manager.get_task(task_id)
        if t is None:
            return None
        sess_id = self._session.id
        tenant_id = self._session.tenant_id

        match t.settings:
            case NormalTaskSettings(use_subagent=True) as s:
                ctx = ProviderContext(session_id=sess_id, tenant_id=tenant_id)
                sub_tmpl_id = (
                    await self._runtime._resolve_subagent_template(s.subagent_template, ctx)
                    if s.subagent_template else ""
                ) or self._template_id
                parent_agent = self._resolved_agents.get(t.creator_agent_id) if t.creator_agent_id else None
                agent, tmpl = await self._lm.instantiate_agent(
                    template_id=sub_tmpl_id, session_id=sess_id, tenant_id=tenant_id,
                    parent_agent=parent_agent, ctx=ctx,
                    existing_agent_id=t.assigned_agent_id or None,
                )
                agent = _dc.replace(agent, loop_guard=LoopGuard(
                    context_limit=self._session.context_limit,
                    reserved_output_tokens=self._session.reserved_output_tokens,
                ))
                t.assigned_agent_id = agent.id
                await _flush_tracking_memory(agent, t, self._task_manager, self._memory, sess_id, tenant_id)
                if s.inherit_memory and not t.user_prompt_in_memory:
                    # Parented sub-tasks copy from their parent; a root turn dispatched
                    # straight to a sub-agent has no parent_task_id, so fall back to the
                    # previous root task (else its sub-agent starts blank — no session memory).
                    src_t = (
                        self._task_manager.get_task(t.parent_task_id) if t.parent_task_id
                        else _latest_prior_root_task(self._task_manager, t)
                    )
                    if src_t:
                        await _copy_memory_for_inherit(
                            parent_task=src_t, child_task=t, sub_agent=agent,
                            memory=self._memory, session_id=sess_id, tenant_id=tenant_id,
                        )
                initial = await self._reconcile_or(t, agent, "prepare")
                self._resolved_agents[agent.id] = agent
                return AgentBinding(agent_id=agent.id, agent=agent, template=tmpl,
                                    initial_step=initial, run_id=generate_id("run"))

            case _:
                # 非 subagent 任务在**创建者**的 agent scope 上跑（延续创建者对话），
                # 而非一律 root——否则 subagent 派生的非 subagent 子会跑进 root scope、丢失
                # 创建者上下文并污染 root。scope 键与调度串行判定共用 effective_agent_id 单一真相。
                agent = self._default_agent(
                    effective_agent_id(t, self._session.root_agent_id or ""),
                )
                await _flush_tracking_memory(agent, t, self._task_manager, self._memory, sess_id, tenant_id)
                initial = await self._reconcile_or(t, agent, "prepare")
                self._resolved_agents[agent.id] = agent
                return AgentBinding(agent_id=agent.id, agent=agent, template=self._template,
                                    initial_step=initial, run_id=self._default_run_id)

    # ── 阶段 2：执行 ─────────────────────────────────────────────────────────

    async def execute(self, binding: "AgentBinding", task_id: str) -> None:
        t = self._task_manager.get_task(task_id)
        if t is None:
            return
        # 单 owner 架构 seam：model 在派发时从可变 session 读取；控制令牌 per-run 发放——
        # 随本次派发登记进 runtime registry、run 结束注销，pause/cancel 经 registry 必达在途 run。
        # 出生信号在登记点按执行 agent 分流：pause 弃子窗口内 root run born-pause、
        # 非 root run born-cancel（一次暂停恰一个续跑点，且气泡必落 root agent）。
        tokens = self._runtime._register_run_tokens(
            self._session.id, task_id,
            root_run=binding.agent_id == (self._session.root_agent_id or ""),
        )
        # assemble 窗口补偿：TM 已整体取消（cancel_all 在 assemble 期间到达）→ 本 run 出生即取消。
        if self._task_manager.is_cancelled():
            tokens.cancel.cancel()
        try:
            s, _ = await self._runtime._execute_task(
                session=self._session,
                task=t,
                agent=binding.agent,
                template=binding.template,
                run_id=binding.run_id,
                memory=self._memory,
                llm_account=self._session.llm_provider or self._llm_account,
                llm_model=self._session.llm_model or self._llm_model,
                initial_step=binding.initial_step,
                task_manager=self._task_manager,
                cancel_token=tokens.cancel,
                pause_token=tokens.pause,
            )
        finally:
            self._runtime._deregister_run_tokens(self._session.id, task_id)
        if self._handle is not None and s is not None:
            self._handle._state = s

    # ── helpers（原闭包内嵌函数）───────────────────────────────────────────────

    def _default_agent(self, agent_id: str | None = None) -> Agent:
        return Agent(
            id=agent_id or generate_id("agt"),
            session_id=self._session.id,
            template_id=self._template.id,
            template_version=self._template.version,
            status="RUNNING",
            tenant_id=self._session.tenant_id,
            loop_guard=LoopGuard(
                context_limit=self._session.context_limit,
                reserved_output_tokens=self._session.reserved_output_tokens,
            ),
            memory_config=self._template.memory_config,
            loop_config=self._template.loop_config,
            created_at=now_utc(),
        )

    async def _reconcile_or(self, t: "Task", agent: "Agent", base: str) -> str:
        """base initial_step；若该 task 最近 assistant turn 有 dangling tool_call → reconcile。"""
        from ctx_weft.protocols.context import ProviderContext as _PCtx
        from ctx_weft.protocols.memory import MemoryScope as _Scope
        sess_id = self._session.id
        scope = _Scope(session_id=sess_id, task_id=t.id, agent_id=agent.id)
        pctx = _PCtx(session_id=sess_id, tenant_id=self._session.tenant_id,
                     task_id=t.id, agent_id=agent.id)
        if await _task_has_dangling_tool_call(self._memory, scope, pctx):
            return "reconcile"
        return base
