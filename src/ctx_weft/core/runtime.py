"""CtxWeftRuntime：顶层 API。

Phase 4 版本：完整 SessionManager + TaskManager + AgentRegistry 支持；
同时保留 run_single_task() 兼容 Phase 1 测试。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ctx_weft.protocols import ContentPart

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
from ctx_weft.protocols.capability import Authorizer
from ctx_weft.core.control.tokens import CancelToken, PauseToken, RunTokens
from ctx_weft.core.discriminators import CancelReason, InterruptReason
from ctx_weft.protocols.events import Event, EventOrigin, EventType
from ctx_weft.core.hitl.registry import HitlRegistry, PendingHitl
from ctx_weft.core.hitl.reply_intake import ReplyIntake
from ctx_weft.core.hitl.service import HitlService
from ctx_weft.core.hitl.status import paused_status_for
from ctx_weft.core.loop.capability_gateway import CapabilityGateway
from ctx_weft.core.loop.driver import LoopContext, LoopState, StepDriver, make_event
from ctx_weft.core.loop.hitl_waiter import HitlWaiter
from ctx_weft.core.loop.park import HitlPark
from ctx_weft.core.loop.steps import (
    ActStep,
    FinalizeStep,
    ObserveStep,
    PrepareStep,
    RecognizeIntentStep,
)
from ctx_weft.core.loop.steps.background_observe import (
    await_pending_background_observe,
    launch_background_observe,
    register_close_synth,
)
from ctx_weft.core.loop.steps.compact import CompactStep
from ctx_weft.core.loop.steps.reconcile import ReconcileStep
from ctx_weft.core.loop.steps.suspend import SuspendStep
from ctx_weft.core.orchestrator.capability_cache import CapabilityCache
from ctx_weft.core.orchestrator.control_capability import ControlCapabilityProvider
from ctx_weft.core.orchestrator.agent_registry import AgentRegistry, ModelChoice, ResolvedModel
from ctx_weft.core.orchestrator.agent_state import AgentInput
from ctx_weft.core.orchestrator.session_manager import SessionManager
from ctx_weft.core.orchestrator.task_manager import TaskManager, _task_payload
from ctx_weft.core.orchestrator.task_disposition import RunOutcome, RunOutcomeKind
from ctx_weft.core.orchestrator.task_queue import QueueEntry
from ctx_weft.core.orchestrator.task_runner import AgentBinding, TaskRunner, effective_agent_id
from ctx_weft.core.state.models import Agent, LoopGuard, NormalTaskSettings, Session, Task
from ctx_weft.core.errors import (
    AgentNotFound,
    AgentNotRunningError,
    crash_error_code,
    crash_run_outcome,
)
from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols import (
    AgentTemplate,
    Capability,
    KnowledgeProvider,
    LLMClient,
    LLMClientResolver,
    LLMOutageError,
    MemoryAddress,
    MemoryKind,
    MemoryProvider,
    MemoryScope,
    ProviderContext,
)
from ctx_weft.protocols.agent import AgentDetail, AgentSummary
from ctx_weft.protocols.capability import (
    AgentCapabilityProvider,
    CapabilityProvider,
    SessionScopedCapabilityProvider,
    SkillCapabilityProvider,
    qualify,
)
from ctx_weft.protocols.events import EventBus
from ctx_weft.protocols.hitl import (
    HITL_OUTCOME_ACCEPTED,
    PREFACE_AFTER_INTERRUPT,
    PREFACE_AFTER_INTERRUPT_EDIT,
    HitlReply,
    HitlRequestView,
    ToolResultDelivery,
    UserTurnDelivery,
)

logger = logging.getLogger(__name__)

#: `resume_agent` 冷续跑一个暂停气泡时喂给 `_write_hitl_reply_turn` 的 message——
#: 操作者的"继续跑"没有新指示可言，空串会落到那边的 `"(no response)"` 兜底文案
#: （读起来像"问了没人答"，语义不对：这不是一次没人回答的提问）。与
#: `act.INTERRUPTED_MARK` / `act.CANCELLED_MARK` 同一方括号风格的合成标记。
_RESUME_MARK = "[resumed by operator]"


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
    from ctx_weft.protocols import MemoryAddress, MemoryEvent, MemoryEventType

    parent_agent_id = parent_task.assigned_agent_id or parent_task.creator_agent_id
    parent_scope = MemoryAddress(session_id=session_id, task_id=parent_task.id, agent_id=parent_agent_id)
    ctx = ProviderContext(session_id=session_id, tenant_id=tenant_id)

    # Mirror the parent agent's current recall view (spec Phase 2, 2026-06-30):
    #   (a) task-layer body — parent's own turns + same-agent siblings' bodies (by agent_id), and
    #   (b) agent-layer dispatch turns — the start_task frames, cross-agent bubbles, and same-agent
    #       finish pairs that Phase 1 writes into the parent agent scope.
    # Merging both by (timestamp, seq_no) means inherited same-agent sibling bodies arrive FRAMED
    # (their start_task frame precedes them, so no naked leak) and cross-agent siblings arrive as
    # bubbles. AGENT_COMPACT_SUMMARY is still excluded — the parent's folded black-box dispatch log
    # is of little use to a child.
    from ctx_weft.protocols import MemoryAddress, MemoryKind, MemoryScope
    # v2 P3a：body = TASK 视图默认 kinds（对话+段摘要，跨 task 半址）；frames = AGENT 视图
    # conversation turn（AGENT_COMPACT_SUMMARY = SUMMARY kind，天然排除）。
    body_records = await memory.load_view(
        MemoryAddress(session_id=session_id, agent_id=parent_agent_id),
        MemoryScope.TASK, ctx,
    )
    frame_records = await memory.load_view(
        MemoryAddress(session_id=session_id, agent_id=parent_agent_id),
        MemoryScope.AGENT, ctx, kinds=[MemoryKind.CONVERSATION_TURN],
    )
    combined = sorted(
        [*body_records, *frame_records],
        key=lambda r: (r.timestamp, r.metadata.get("seq_no", 0)),
    )
    child_scope = MemoryAddress(session_id=session_id, task_id=child_task.id, agent_id=sub_agent.id)
    for r in combined:  # chronological → re-ingest preserves order via fresh per-scope seq_no
        md = {"inherited_from_task_id": parent_task.id}
        if r.role == "assistant" and r.metadata.get("tool_calls"):
            md["tool_calls"] = r.metadata["tool_calls"]
        if r.role == "tool" and r.metadata.get("tool_call_id"):
            md["tool_call_id"] = r.metadata["tool_call_id"]
        await memory.ingest(
            MemoryEvent(
                kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.AGENT,
                address=child_scope,
                content=r.content,
                timestamp=r.timestamp,
                role=r.role,
                metadata=md,
            ),
            ctx,
        )


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
        self._blob_store: "MemoryBlobStore | None" = None
        self._null_blob_store: "MemoryBlobStore | None" = None
        self._event_blob_store: "EventBlobStore | None" = None
        self._null_event_blob_store: "EventBlobStore | None" = None

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
        from ctx_weft.core.orchestrator.skill_executor_capability import (
            SkillExecutorCapabilityProvider,
        )
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

    # ── MemoryBlobStore ──────────────────────────────────────────────────────

    def register_memory_blob_store(self, store: "MemoryBlobStore") -> None:
        """注册二进制内容存储。未注册时 get_memory_blob_store() 返回 NullMemoryBlobStore。"""
        self._blob_store = store

    def get_memory_blob_store(self) -> "MemoryBlobStore":
        """取 memory 侧 blob store。**只有两级：显式注册 > NullMemoryBlobStore。**

        与 `get_event_blob_store()` 完全对称。曾经这里有第三级——「memory provider 自己
        实现了 MemoryBlobStore 且 can_externalize 就用它」——那一级的唯一服务对象是
        `SqlMemoryProvider` 的字节存储；字节已移出 RDBMS（spec 2026-08-29 §5），
        该级无对象可服务，一并删除。

        自动解析删掉之后，「字节放哪」这件事只由接线代码表达，不再藏在解析规则里：
        宿主要 blob 能力就显式 `register_memory_blob_store(FsBlobStore(...))`。
        不注册就是 `NullMemoryBlobStore`，行为与不接 blob 的宿主逐字节一致。

        `NullMemoryBlobStore` 实例只建一次，重复调用返回同一对象。
        """
        if self._blob_store is not None:
            return self._blob_store
        if self._null_blob_store is None:
            from ctx_weft.protocols import NullMemoryBlobStore
            self._null_blob_store = NullMemoryBlobStore()
        return self._null_blob_store

    # ── EventBlobStore ───────────────────────────────────────────────────────

    def register_event_blob_store(self, store: "EventBlobStore") -> None:
        """注册事件流侧的二进制存储。未注册时 get_event_blob_store() 返回 NullEventBlobStore。"""
        self._event_blob_store = store

    def get_event_blob_store(self) -> "EventBlobStore":
        """取 event blob store。**只有两级：显式注册 > NullEventBlobStore。**

        与 `get_memory_blob_store()` 完全对称——两侧都**刻意不**自动解析到 memory
        provider（spec §4；memory 侧那条已删除的中间级见 spec 2026-08-29 §5.3）：
        自动解析会让「共用」成为隐式默认，而双 store 的出发点正是让两者**可分**。
        host 要共用就把同一个实例注册两次——意图写在接线代码里，而不是藏在解析规则里。

        「可分」的实质不在解析规则，而在 ref 命名空间：两个 store 的 ref 是两个
        独立的命名空间，即便 host 把同一个实例注册两次也不改变这一点——core 从不
        比较两侧的 ref，也从不拿一侧的 ref 去另一侧解析。

        `NullEventBlobStore` 实例只建一次，重复调用返回同一对象。
        """
        if self._event_blob_store is not None:
            return self._event_blob_store
        if self._null_event_blob_store is None:
            from ctx_weft.protocols.events import NullEventBlobStore
            self._null_event_blob_store = NullEventBlobStore()
        return self._null_event_blob_store


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
    user_prompt: "str | list[ContentPart]"
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
        user_prompt: "str | list[ContentPart]",
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
        from ctx_weft.protocols.events import EventFilter
        async for ev in self.event_bus.stream(EventFilter(run_id=self.run_id)):
            yield ev

    async def wait_for_finish(self, timeout: float = 300.0) -> LoopState | None:
        """Block until RunFinished event or timeout."""
        try:
            async with asyncio.timeout(timeout):
                from ctx_weft.protocols.events import EventFilter
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


def _reply_turn_agent_id(req: "PendingHitl", target: "Task") -> str:
    """人的答复该写进**哪个 agent scope**。写入（`_write_hitl_reply_turn`）与「查它写没写过」
    （`_injected_reply_ids`）共用这一份，两边不得各推一次。

    必须是本 task 对话真正所在的 agent scope——`AgentRecallSource` 用
    `recall_recent_by_agent` 按 `scope.agent_id` 过滤召回 task body。冷重启从旧事件日志
    重建的 HITL 丢了 agent_id（legacy `HITL_REQUIRED` 投影未持久化它），`req.agent_id=""`
    会把回复写进空 agent scope → 对 actor 装配不可见 → 重启后「第一句」丢失。故回退到
    task 的真实 agent（assigned/creator），与首条 USER_PROMPT 落库时同 scope。
    """
    return (req.agent_id
            or getattr(target, "assigned_agent_id", "")
            or getattr(target, "creator_agent_id", "")
            or "")


def _suspended_on_live_children(task_manager: "TaskManager", task: "Task") -> bool:
    """该 task 是否「SUSPENDED 且尚有未终态的子任务」——即等着 `_try_resume_parent` 唤醒。

    这是 `restore` 唯一**刻意不重排**的形状（见 `TaskManager.restore` 的注释）：它必须
    保持 SUSPENDED，否则 `_try_resume_parent` 的 `status == "SUSPENDED"` 门失效，子任务
    收尾时那次唤醒静默消失，父任务永久停摆。
    """
    if task.status != "SUSPENDED":
        return False
    _TERMINAL = ("FINISHED", "FAILED", "CANCELED")
    for cid in task_manager.children_of(task.id):
        child = task_manager.get_task(cid)
        if child is None or child.status not in _TERMINAL:
            return True
    return False


class CtxWeftRuntime:
    """Top-level runtime.

    Supports two modes:
    - run_single_task(): Phase 1 compat — simple single-task execution
    - start_session(): Phase 4 orchestration; session_id=None creates, session_id=<id> resumes
    """

    def __init__(
        self,
        *,
        providers: ProviderRegistry | None = None,
        llm: LLMClient | None = None,
        event_bus: EventBus | None = None,
        event_store: "Any | None" = None,
        config: "RuntimeConfig | None" = None,
        snapshot_every_n: int = 0,
    ) -> None:
        from ctx_weft.core.config import RuntimeConfig
        self._config = config or RuntimeConfig()
        self._llm = llm  # fallback for backward compat / tests
        self.providers = providers or ProviderRegistry()
        # 默认实现只在 host 没给时才解析——避免「默认」从运行期选择退化成 import 期耦合。
        if event_bus is None:
            from ctx_weft.providers.events import InProcessEventBus
            event_bus = InProcessEventBus()
        self._event_bus: EventBus = event_bus
        # HITL：构造期一次性接线，**没有 setter、没有半成品窗口**。裸构造即生产形态
        # （spec 2026-09-01 重设计 §3）。`_normalize_hitl_content` 是与 start_session /
        # run_single_task 共用的内容校验 + 外部化方法（Phase 3c Task A），此处原样接进
        # `ReplyIntake` 作为其 `ContentNormalizer`。
        self.hitl_registry = HitlRegistry(max_resolved=self._config.hitl_max_resolved)
        self.hitl = HitlService(
            registry=self.hitl_registry,
            event_bus=self._event_bus,
            reply_intake=ReplyIntake(self._normalize_hitl_content),
        )
        self._hitl_timeout_sec = self._config.hitl_timeout_sec
        from ctx_weft.providers.events import attach_persistence
        if event_store is None:
            from ctx_weft.providers.events import InMemoryEventStore
            event_store = InMemoryEventStore()
        self.event_store = event_store
        # 单一入口交给 attach_persistence（spec 2026-08-29 §6.4 + final review R15）：
        # ① EventPersister 与 SnapshotWriter 的订阅顺序契约（persister 必须先于 snapshot
        #   writer 订阅，否则 rebuild_view 看不到当前事件）统一由它保证，宿主不必懂顺序；
        # ② snapshot_every_n=0（默认）时不接 SnapshotWriter —— 与改造前行为零变化；
        # ③ 返回的 handle 存成**公开**属性 `self.persistence`（而不是私有的
        #   `_event_persister`），因为 `EventPersister.detach` 的 docstring 明确要求宿主
        #   换持久 store 时调用 detach，私有且无调用点会让那个用例本就是坏的。
        self.persistence = attach_persistence(
            self._event_bus, self.event_store, snapshot_every_n=snapshot_every_n)

        # Auto-register 内置 providers（与用户注册的 providers 无关）
        control_provider = ControlCapabilityProvider()
        self.providers.register_capability(control_provider)

        from ctx_weft.core.orchestrator.skill_executor_capability import (
            SkillExecutorCapabilityProvider,
        )
        skill_executor = SkillExecutorCapabilityProvider(self.providers)
        self.providers.register_capability(skill_executor)

        # media:get_image —— 取回被 L0.5 降级掉的图（子设计 §9）。core 侧 provider：
        # 工具体要读 task 视图算「原本挂在第几条 user 回合」，普通 capability provider
        # 拿不到 MemoryProvider。memory / blob store 由它在调用时经 registry 解析，
        # 故此处不要求它们已注册。
        from ctx_weft.core.media.capability import MediaCapabilityProvider
        self.providers.register_capability(MediaCapabilityProvider(self.providers))

        # 模板通道硬校验（spec 2026-07-22）：模板进入 core 的唯一通道是
        # AgentCapabilityProvider；缺失则 root agent 都无法实例化，构造即失败。
        agent_provider_names = [
            p.name for p in self.providers.get_capability_providers()
            if isinstance(p, AgentCapabilityProvider)
        ]
        if not agent_provider_names:
            raise ValueError(
                "CtxWeftRuntime requires at least one AgentCapabilityProvider in the "
                "ProviderRegistry — register one before constructing, e.g. "
                "providers.register_capability(LocalAgentTemplateProvider(templates_dir))"
            )
        from ctx_weft.core.orchestrator.template_lookup import TemplateLookup
        self._template_lookup = TemplateLookup(self.providers)
        # Agent 注册表：runtime 级长生命周期组件，_agents 是 agent 身份与配置的唯一住所。
        # 从前 AgentRegistry 是每次调用 new 一个的临时对象，见
        # docs/events-v2.md §2.1.1（与 SessionManager 同形的那次晋升）。
        # model_resolver=self._resolve_llm：registry 现解不缓存（那是 LLMClientResolver
        # 的职责），构造期注入、无默认值——单测跑的和生产跑的必须是同一个东西
        # （core/hitl/reply_intake.py docstring 的既有立场，Task 4 因默认 bus 判过一次
        # Critical，这里不重蹈）。self._resolve_llm 已带好「无 provider 时回落 self._llm」
        # 那条分支，无需在此重复。
        self._agent_registry = AgentRegistry(
            template_lookup=self._template_lookup, event_bus=self._event_bus,
            model_resolver=self._resolve_llm)
        # ALM 的输入端：只认 TASK_*（_INPUT_BY_EVENT），发 AGENT_*。与
        # SessionManager.attach_to_bus 之间无顺序依赖——两者各订各的事件类型，互不
        # 消费对方发出的事件（docs/events-v2.md §2.1.1，Task 12）。
        self._agent_registry.attach_to_bus()

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
        # 会话状态的唯一住所。从前 SessionManager 是每次调用 new 一个的临时对象
        # （无状态、用完即弃），状态因此无处可放，被 TaskManager / runtime / reducer
        # 各写一份。见 docs/events-v2.md §2.1.1。
        self._session_manager = SessionManager(
            agent_registry=self._agent_registry,
            event_bus=self._event_bus,
            task_max_concurrent=self._config.task_max_concurrent,
            task_max_retries=self._config.task_max_retries,
            default_task_timeout_ms=self._config.default_task_timeout_ms,
        )
        # SM 的输入端：只认 TaskManager 的四类事件（_INPUT_BY_EVENT），本 task
        # 之后 TM 还没开始发这三条信号，运行时行为不变（docs/events-v2.md §2.1.1）。
        self._session_manager.attach_to_bus()
        # Per-session resume 锁：串行化同一 session 的 recover_session，避免重叠的冷 HITL 应答 /
        # /resume 并发建出两个 TaskManager、两套 drain 竞争派发（spec/07 §9）。惰性建、不回收
        # （体量微小、按 session 数有界）。
        self._resume_locks: dict[str, asyncio.Lock] = {}

    @property
    def event_bus(self) -> EventBus:
        return self._event_bus

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

    async def _validate_and_normalize_content(
        self,
        content: "str | list[ContentPart]",
        session_id: str,
        *,
        tenant_id: str = "default",
    ) -> "tuple[str | list[ContentPart], str | list[dict] | None]":
        """入口内容校验 + **双侧外部化**的单一真源（三个入口共用）。

        返回 `(memory 侧归一化内容, event 侧 jsonable)`。两个产物都从**同一份原始
        content** 派生，各自只碰一个 store：memory 侧走 `normalize_content`，event
        侧走 `content_to_event_jsonable`。两个 ref 不必相同，core 也不比较它们——
        两个契约独立、ref 命名空间互不相通（2026-08-28 解耦方案）。

        **event 侧必须先做**：它要的是归一化**之前**的原始字节。等 `normalize_content`
        把图片 part 改写成 memory ref 之后再喂给 event 侧，拿到的只会是一个 event
        store 永远打不开的引用（`content_to_event_jsonable` 会把它降级成文本占位并
        告警——图片就在事件流里丢了）。

        顺序恒为 `validate_content` → 两侧外部化，理由两条（spec §6.1）：
        (a) 被拒的内容不该在任何 blob store 里留下垃圾——校验失败必须发生在任何 `put`
        之前；(b) `normalize_content` 里的 `b64decode(..., validate=True)` 刻意不加
        try/except，靠 validate 先行把畸形 base64 拦成 `InvalidContentError`。抽成
        这一个方法之后，三处调用点的顺序不会再各自漂移。

        **(a) 有一个诚实的例外**：event 侧先 put、memory 侧后 put，故 memory 侧
        `normalize_content` 失败（blob store 报错等）时，本方法带着异常返回，而字节
        **已经**落进了 event blob store——留下一份无人引用的孤儿。这个顺序是被「event
        侧要的是归一化之前的原始字节」硬性决定的（见上），不为此改序、也不加补偿删除
        （删除本身会失败、且 `EventBlobStore` 协议里没有 delete）。孤儿的回收归宿主的
        event blob 保留策略，与「被拒的内容不在 blob store 里留垃圾」相比，这里的口径
        准确说法是：**校验（validate）失败恒不留垃圾；外部化中途失败可能留下 event 侧
        孤儿字节**。

        **不解析 LLM、不判模型能力**（spec 2026-08-28）：模态处置归 `LLMClient`
        实现方，core 全程透传。这也让「纯文本不提前解析 LLM」这条不变量自动成立
        ——本方法根本不碰 LLM。

        memory 侧不能外部化（`NullMemoryBlobStore`）时第一个产物是原样的同一对象；
        纯文本 content 两侧都是零 IO 直通（两个函数对 `str` 都原样返回）。
        """
        from ctx_weft.core.content import (
            content_to_event_jsonable,
            normalize_content,
            validate_content,
        )

        event_blob_store = self.providers.get_event_blob_store()
        validate_content(content, event_blob_store=event_blob_store)
        ctx = ProviderContext(session_id=session_id, tenant_id=tenant_id)
        event_jsonable = await content_to_event_jsonable(
            content, event_blob_store=event_blob_store, ctx=ctx,
        )
        blob_store = self.providers.get_memory_blob_store()
        if not blob_store.can_externalize:
            return content, event_jsonable
        normalized = await normalize_content(content, blob_store=blob_store, ctx=ctx)
        return normalized, event_jsonable

    async def _tenant_for_session(self, session_id: str) -> str:
        """由 session_id 解出 tenant_id；解不出一律回落 ``"default"``，**绝不抛**。

        HITL 请求不带 tenant_id，而 blob 的 ProviderContext 需要它——多租户宿主下
        写死 `"default"` 会让 HITL 递进来的图落到错误的 tenant 锚点。

        两条途径，先热后冷：
        1. 活 owner TaskManager 的 `session`（`_task_managers`）——热应答的主路径，纯内存查表；
        2. 事件日志：**每条 `Event` 都带 `tenant_id`**（`protocols/events.py`），取该 session
           第一条即可，不必 `rebuild_view` 折叠整个投影（冷应答/重启后走这条）。

        本方法在 HITL 应答路径上——**抛错会卡住人类应答**，故整段 best-effort：
        存储不可用 / session 无事件 / 事件不带 tenant，一律回落 `"default"`（= 现状）。
        """
        tm = self._task_managers.get(session_id)
        sess = tm.session if tm is not None else None
        if sess is not None and sess.tenant_id:
            return sess.tenant_id
        try:
            events = await self.event_store.read_by_session(session_id)
        except Exception:
            logger.warning(
                "HITL tenant resolve: cannot read events for session %s; using 'default'",
                session_id, exc_info=True,
            )
            return "default"
        for ev in events:
            tenant = getattr(ev, "tenant_id", "")
            if tenant:
                return tenant
        return "default"

    async def _normalize_hitl_content(
        self, content: "str | list[ContentPart]", session_id: str,
    ) -> "tuple[str | list[ContentPart], str | list[dict] | None]":
        """HITL 应答内容的校验 + 双侧外部化（`ReplyIntake` 的 `ContentNormalizer` 回调）。

        只做「从 session_id 解出本次应答真正要用的 tenant」这一件事，校验/外部化本身
        仍由三入口共用的 `_validate_and_normalize_content` 完成（顺序恒为
        validate → 两侧外部化，不在此重写一遍），返回值也原样透传它的二元组
        `(memory 侧内容, event 侧载荷)`——`HitlService` 拿后者直接发 HITL_* 事件。

        - **tenant**：由 `session_id` 解出（见 `_tenant_for_session`）。解 tenant 冷路径
          要读事件日志，故只在**真会写 blob** 时才付这个代价：纯文本、或两个 blob store
          都不能外部化时 tenant 根本用不上。判据是 memory **或** event 任一可外部化就要
          解——event 侧的外部化独立于 memory 侧（两个 store 各写各的），只看 memory 侧
          会漏掉「memory 不可外部化、event 可外部化」这一组合的 tenant。

        解出的 tenant 只喂给本次外部化：event 侧与 memory 侧同用这一个
        `ProviderContext`，两边的 blob 落在同一个 tenant 锚点上。
        """
        tenant_id = "default"
        if not isinstance(content, str) and (
            self.providers.get_memory_blob_store().can_externalize
            or self.providers.get_event_blob_store().can_externalize
        ):
            tenant_id = await self._tenant_for_session(session_id)
        return await self._validate_and_normalize_content(
            content, session_id, tenant_id=tenant_id,
        )

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
            await tm.abandon_pending(
                reason=CancelReason.PAUSE_ABANDON, keep_agent=root_agent or None
            )
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
        """硬取消：取消全部在途 run（per-run CancelToken）+ 全部后续 task（drain 队列）→ 会话 CANCELED
        + 该 session 下**每个 agent** 显式转 `terminated`（R23）。

        memory 保留。开新对话由调用方另起（新 /messages → 同 session_id 的 new run）。

        R23（task-20 核实结论）：`cancel_all` 让 task 发 `TASK_CANCELED`，经 ALM
        （`_INPUT_BY_EVENT[TASK_CANCELED] = AgentInput.SETTLED`）只会把 agent 打回
        `idle`——不是 `terminated`。会话被取消后 agent 却还"活着"（`idle`，能再收
        `send_message`），与"会话取消"这个动作的意图不符。故在既有的会话级取消机制
        （`cancel_all` 清队列 + 全部 run token cancel + `_release_session` 回收
        per-session 状态）之外，**额外**用 Task 19 的 `cancel_agent`——唯一的 agent
        终态入口——把该 session 下每个 agent 都真正推到 `terminated`。
        """
        per = self._run_tokens.get(session_id, {})
        task_manager = self._task_managers.get(session_id)
        if not per and task_manager is None:
            return False
        # 取消前判定会话是否已空闲挂起（无在跑任务）。RUNNING：在途 task 经 CancelToken→checkpoint
        # 协作取消→on_task_finished→is_done→_fire_session_done→_on_done 自行回收，故此处不抢着回收。
        idle = task_manager is not None and task_manager.is_done()
        # 未决的 ask_user 一并终局，且**先于**下面 cancel_all 触发的 SessionFinished——
        # 与熔断 trip 序列同一条纪律（HITL 终局须先于会话终态）。不终局的代价在重启后：
        # rebuild_hitl 按「有 HitlOpened 无终局事件」折 pending，会把已取消会话的
        # 提问当未决恢复出来（总账 A10）。
        await self._cancel_session_hitl(session_id, message=CancelReason.USER_CANCEL)
        if task_manager is not None:
            await task_manager.cancel_all(reason=CancelReason.USER_CANCEL)
        for tokens in per.values():
            tokens.cancel.cancel()
        # R23：显式终态化每个 agent——必须在 `_release_session` 之前做（那一步会把
        # agent record 从 registry 摘掉，届时 `cancel_agent` 查无此 agent，只能静默
        # 跳过、发不出 `AgentTerminated`）。上面的 `_cancel_session_hitl` 已经把该
        # session 全部未决 HITL 收口过一轮，`cancel_agent` 内部对 `waiting_human` 的
        # HITL 终局分支这里必是 no-op——HITL 终局先于 agent 终态的纪律因此自动成立，
        # 不需要在这里再插一次序。走 `cancel_agent`（Task 19 的唯一 agent 终态入口），
        # 不直接拍 `apply_input`/改 `rec.status`。
        for aid in list(self._agent_registry.agent_ids_of_session(session_id)):
            await self.cancel_agent(aid, reason="session_canceled")
        if idle:
            # 已暂停/中断（无在跑 task）的会话被取消：cancel_all 不经 _fire_session_done，_on_done
            # 不会触发，故显式回收 runtime 侧 per-session 状态（含较重的 TaskManager），避免滞留。
            self._release_session(session_id)
        return True

    # ── Agent 级取消（Task 19）───────────────────────────────────────────────

    async def cancel_agent(self, agent_id: str, *, reason: str | None = None) -> list[str]:
        """终止 agent 及其全部子孙（spec 6）。返回被终结的 agent id 列表。

        级联向下展开（`AgentRegistry.descendants_of`，自带成环防御），避免孤儿子
        agent 永远挂着无人管。每个目标按各自**当前**状态分别处理，再统一转
        `terminated`：
        - `waiting_human`：先终局它名下的未决 HITL——按 `agent_id` 过滤
          （`_cancel_pending_hitl_of`），不殃及同 session 其他 agent 的未决提问。
          `_cancel_session_hitl` 是 session 粒度，这里要的是 agent 粒度，不能复用。
        - `running`：`_cancel_run_token` 对其在途 run 发协作取消信号——按 task_id
          索引，不是 TaskManager 的会话级 `cancel_all`。只发信号，不代表立即终结：
          `TASK_CANCELED` 是否发出由 run 收尾时的既有守卫按 task 状态判定，这里不等。
        - `idle` / `interrupted`：无需额外动作，直接转 `terminated`。

        全部转移经 `AgentRegistry.apply_input` 一处发生——那是状态转移与事件发射的
        唯一入口，不允许绕过它自己拼 AgentTerminated 事件。对已经是 `terminated`
        的目标，`apply_input` 按五态机定义返回 False，天然跳过、不重复终结。

        HITL 终局必须**先于**该 agent 转 `terminated`——与 `cancel_session` 里
        `_cancel_session_hitl` 先于 `cancel_all` 同一条纪律：重启后 `rebuild_hitl`
        按「有 HitlOpened 无终局事件」把已取消的提问当未决恢复出来，顺序错了会把这条
        恢复不变量搞坏（见 `_cancel_session_hitl` 调用处注释）。

        `agent_id` 直接指定的那个 `cascaded_from=None`；因级联被带上的子孙传发起者
        的 `agent_id`，供 host 侧区分「用户直接点了取消」还是「祖先被取消带下来的」。

        agent 不存在 -> 返回空列表，不抛错（幂等友好，与 `AgentRegistry.has()` 之类
        既有「查无則静默」的读路径同一口径）。
        """
        reg = self._agent_registry
        if agent_id not in reg._agents:
            return []

        targets = [agent_id, *reg.descendants_of(agent_id)]
        killed: list[str] = []
        for aid in targets:
            rec = reg._agents.get(aid)
            if rec is None or rec.status == "terminated":
                continue

            if rec.status == "waiting_human":
                await self._cancel_pending_hitl_of(aid, session_id=rec.session_id)
            if rec.status == "running" and rec.current_task_id:
                self._cancel_run_token(rec.session_id, rec.current_task_id)

            ok = await reg.apply_input(
                aid,
                AgentInput.CANCEL,
                task_id=rec.current_task_id,
                reason=reason or "canceled",
                cascaded_from=None if aid == agent_id else agent_id,
            )
            if ok:
                killed.append(aid)
        return killed

    async def _cancel_pending_hitl_of(self, agent_id: str, *, session_id: str) -> None:
        """终局**该 agent 名下**全部未决 HITL（`cancel_agent` 专用）。

        与 `_cancel_session_hitl` 同一模式（best-effort，单条失败不阻断其余），区别
        在粒度：`_cancel_session_hitl` 按 session 收口全部未决请求，这里额外按
        `agent_id` 过滤（`HitlRequestView.agent_id`）——`cancel_agent` 只该终局这一个
        agent 名下的未决提问，不能误伤同一 session 里其他 agent 仍然合法在等的提问。
        """
        for v in self.list_pending_hitl(session_id=session_id):
            if v.agent_id != agent_id or v.resolved:
                continue
            try:
                await self.hitl.cancel(v.id, message=CancelReason.USER_CANCEL)
            except Exception:
                logger.exception(
                    "_cancel_pending_hitl_of: cancel failed for agent=%s hitl=%s", agent_id, v.id,
                )

    # ── Agent 级暂停 / 恢复（Task 20）────────────────────────────────────────

    async def pause_agent(self, agent_id: str, *, reason: str | None = None) -> list[str]:
        """暂停 agent 及其全部**当前 running** 的子孙（spec 7）。

        只对 `running` 生效——`agent_id` 本身非 running 直接报错（`AgentNotRunningError`）；
        级联展开到的子孙里非 running 的静默跳过（暂停不该殃及本就 idle/等待中的子孙）。

        建在既有的**定向暂停原语** `pause_task`（spec 2026-07-05 §2.3）之上，不是把
        `cancel_agent` 的「立即拍状态」搬过来抄一份——`pause_task` 只对准这一个 task
        的 run 发一次软打断信号，被暂停的 run 在自己的下一个检查点自行 park 出一个
        wait 气泡（`ActStep._interrupt_checkpoint` / `_run_llm_turn` /
        `_execute_tool_calls` 命中 `pause_token.is_paused` 后统一走
        `act._park_wait_for_user(source="interrupt", ...)`），agent 状态由那次
        **真实**的 `TASK_AWAITING_HUMAN` 事件经 ALM 转成 `waiting_human`——不是本方法
        直接拍的。

        R24（本任务的核实结论，见 task-20-report.md）：spec/brief 写的
        `running --pause--> interrupted` 与实现不符——`interrupted` 只由
        `TaskManager._suspend_task_interrupted`（宿主 outage/崩溃）驱动，操作者暂停
        经检查点 park，落的是 `waiting_human`。本方法因此**不**调用
        `AgentRegistry.apply_input`，全部转移留给真实的 `TASK_AWAITING_HUMAN` 事件
        走 ALM 唯一入口——这里若手动拍一个 `AgentInput.INTERRUPTED`，就会在
        `waiting_human` 之外多出一条假的 `interrupted` 分支，且与实际状态不符
        （run 还没被暂停完，agent 已经被判定"暂停完成"）。

        返回值是**已成功递送暂停信号**（`pause_task` 命中一个在途 run token）的
        agent id 列表——暂停本身是异步生效的，返回时这些 agent 多半仍是 `running`，
        真正落 `waiting_human` 要等它们各自跑到下一个检查点。
        """
        reg = self._agent_registry
        rec = reg._agents.get(agent_id)
        if rec is None:
            raise AgentNotFound(f"unknown agent: {agent_id}")
        if rec.status != "running":
            raise AgentNotRunningError(
                f"agent {agent_id} is {rec.status}, not running; nothing to pause"
            )

        paused: list[str] = []
        for aid in [agent_id, *reg.descendants_of(agent_id)]:
            r = reg._agents.get(aid)
            if r is None or r.status != "running" or not r.current_task_id:
                continue
            if self.pause_task(r.session_id, r.current_task_id):
                paused.append(aid)
        return paused

    async def resume_agent(self, agent_id: str) -> list[str]:
        """恢复 agent 及其全部**由暂停产生**的等待气泡（spec 7；R24）。

        `resume_agent` **不能**无差别恢复 `waiting_human` 的子孙——那个状态同时是
        「被 `pause_agent` 暂停、park 在检查点」和「agent 主动 `ask_user` 正在等
        用户真答」两种截然不同情形的落点，无差别放行等于替用户回答了那个真问题。

        判据（实测确认，见 task-20-report.md）是 `PendingHitl.delivery`：
        - `pause_agent`/`pause_session` 产生的气泡恒为
          `UserTurnDelivery(preface ∈ {PREFACE_AFTER_INTERRUPT, PREFACE_AFTER_INTERRUPT_EDIT})`
          （`act._park_wait_for_user(source="interrupt", ...)` 的唯一产物）；
        - `ask_user` 的真实结构化提问用的是 `ToolResultDelivery`
          （`reply_as_result=True`，见 `control_capability.py` 的 `ask_user`
          构造），与前者的类型本身就不同，天然互斥；
        - act 纯文本收尾的软待命（`source="plain_text"`）虽然**同样**是
          `UserTurnDelivery`，但 `preface == PREFACE_NORMAL`——那不是暂停产生的，
          是正常一轮说完话后的自然等待，`resume_agent` 若把它也放行，等于没有
          任何新用户输入就凭空续了一轮，同样不对。

        三者叠在一起，只有 `UserTurnDelivery` 且 `preface` 落在
        `{PREFACE_AFTER_INTERRUPT, PREFACE_AFTER_INTERRUPT_EDIT}` 才是本方法该碰的。

        续跑走既有的 HITL 冷续跑通路（`reply_to_hitl` → `_resume_after_hitl` →
        `recover_session`），不直接 `apply_input(AgentInput.RESUMED)`——那样会把
        agent 状态拍成 `running`，但驱动它真正再跑起来的 task 这时其实还没有被
        重排/派发，状态与现实不符；`apply_input` 的唯一入口纪律也要求转移经由
        真事件发生，不由调用方越过 TaskManager 直接拍。真正的 `running` 由续跑
        起来之后的那次**真实** `TASK_STARTED` 事件驱动。
        """
        reg = self._agent_registry
        if agent_id not in reg._agents:
            raise AgentNotFound(f"unknown agent: {agent_id}")

        resumed: list[str] = []
        for aid in [agent_id, *reg.descendants_of(agent_id)]:
            r = reg._agents.get(aid)
            if r is None or r.status != "waiting_human":
                continue
            req = self._pause_bubble_of(aid, session_id=r.session_id)
            if req is None:
                continue
            await self.reply_to_hitl(
                HitlReply(hitl_id=req.id, outcome=HITL_OUTCOME_ACCEPTED, message=_RESUME_MARK)
            )
            resumed.append(aid)
        return resumed

    def _pause_bubble_of(self, agent_id: str, *, session_id: str) -> "PendingHitl | None":
        """该 agent 名下**由暂停产生**的未决 wait 气泡（`resume_agent` 的判据，R24）；
        没有则 `None`。判据见 `resume_agent` 文档字符串。
        """
        for req in self.hitl_registry.list_pending(session_id=session_id):
            if (
                req.agent_id == agent_id
                and isinstance(req.delivery, UserTurnDelivery)
                and req.delivery.preface in (PREFACE_AFTER_INTERRUPT, PREFACE_AFTER_INTERRUPT_EDIT)
            ):
                return req
        return None

    # ── 换模型：两条命令（批次 B）──────────────────────────────────────────────
    #
    # llm_* 此后只出现在这两条命令的入参里（以及「建一个 session」的入参里）。
    # 两条都是纯赋值——不入队、不改任何 task 状态、不触发调度，对一个所有 task
    # 都在等人的 agent 调用它完全安全。派发时 `AgentRegistry.resolve_model` 现读
    # record，因此换模型对**尚未派发**的 run 立即生效；已经在跑的 run 手上的
    # `ResolvedModel` 是那次 assemble() 时现解的快照，不会被这两条命令追改。

    async def set_agent_llm(
        self, agent_id: str, *, llm_account: str = "", llm_model: str = "",
        reason: str = "user_selected",
    ) -> bool:
        """host 入口：把 `(llm_account, llm_model)` 包成 `ModelChoice`，转发给 registry。"""
        return await self._agent_registry.set_agent_llm(
            agent_id, ModelChoice(account=llm_account, model=llm_model), reason=reason,
        )

    async def set_session_llm(
        self, session_id: str, *, llm_account: str = "", llm_model: str = "",
        reason: str = "user_selected",
    ) -> int:
        """host 入口：作用于该 session 下 registry 持有的全部 agent record。

        返回值 = 真正改动了 record 的 agent 数（幂等 no-op 不计数）。
        """
        return await self._agent_registry.set_session_llm(
            session_id, ModelChoice(account=llm_account, model=llm_model), reason=reason,
        )

    # ── Phase 1 compat ───────────────────────────────────────────────────────

    async def run_single_task(
        self,
        *,
        session_id: str | None = None,
        template_id: str,
        user_prompt: "str | list[ContentPart]",
        tenant_id: str = "default",
        llm_account: str | None = None,
        llm_model: str | None = None,
    ) -> tuple[RunHandle, LoopState]:
        """Phase 1 compat: run a single task end-to-end and await completion."""
        from ctx_weft.core.content import content_to_text

        sid = session_id or generate_id("ses")
        ctx = ProviderContext(session_id=sid, tenant_id=tenant_id)
        # 入口即拒、不落库：格式/blob 门控须在任何持久化（Session/Task/事件）之前完成
        # （spec 2026-08-24 Phase 3a）。_resolve_llm 是纯查表，此处先行调用安全——
        # 且必须先于 instantiate()：那一步会发 AgentInstantiated 事件，是这条路径上
        # 第一个「persist」，llm 不可解析须在它之前失败，而非之后（此调用只为早失败，
        # 结果不留用——agent 的窗口由下面 instantiate(llm=...) 内部按同一份 choice
        # 重新解出，registry 不缓存 client，这里的返回值即弃是设计的一部分）。
        self._resolve_llm(llm_account, llm_model)
        # validate → normalize 的顺序与另外两个入口共用同一个方法，不再各写一遍。
        # event 侧产物在这条路径上没有**入口事件**载得下它（run_single_task 自己
        # register_task、不经 push_task，也不发 SESSION_CREATED），但它必须挂到 Task 上：
        # 这条路径产出的 task 一样会被 observer reopen，`reopen_task` 要用它发
        # TASK_REQUEUED；丢掉它等于让 reopen 把原始 prompt 从事件流里抹掉（终审 C1）。
        # 代价是携图时会在 event blob store 里留一份暂时无人引用的字节；不为此加分支，
        # 是因为「三入口共用同一个真源」这条不变量比省掉一次 compat 路径上的 put 更值钱
        # （宿主的 event blob 回收本就按自己的保留策略走，见 EventBlobStore 协议）。
        user_prompt, user_prompt_event_jsonable = await self._validate_and_normalize_content(
            user_prompt, sid, tenant_id=tenant_id,
        )
        lm = self._agent_registry

        # llm= 传选择（可空）——不是身份：这条创建路径唯一发 AgentInstantiated 的地方，
        # 选择须住进 agent record，事件层才有真值可报（批次 B）。
        agent, template = await lm.instantiate(
            template_id=template_id, session_id=sid, tenant_id=tenant_id, ctx=ctx,
            llm=ModelChoice(account=llm_account or "", model=llm_model or ""),
        )
        # 这次实际要用的 (client, 身份, 窗口)——agent.loop_guard 已由 instantiate()
        # 内部的 materialize() 按同一份 choice 解出并 stamp，此处只是再取一份供
        # session 窗口对齐 + _execute_task 使用（不缓存，现解现弃）。
        resolved_model = lm.resolve_model(agent.id)

        session = Session(
            id=sid,
            user_prompt=content_to_text(user_prompt),
            status="RUNNING",
            tenant_id=tenant_id,
            root_agent_id=agent.id,
            llm_provider=llm_account or "",
            llm_model=llm_model or "",
            created_at=now_utc(),
        )
        session.context_limit = resolved_model.context_limit
        session.reserved_output_tokens = resolved_model.reserved_output_tokens
        # instantiate() 刻意不解模型（惰性不变量，见 agent_registry.py 的
        # _DEFAULT_CONTEXT_LIMIT 注释）；这条 compat 路径立刻要跑真实的一次 dispatch，
        # 在此显式 stamp 上面已经解出的 resolved_model。
        import dataclasses as _dc
        agent = _dc.replace(agent, loop_guard=LoopGuard(
            context_limit=resolved_model.context_limit,
            reserved_output_tokens=resolved_model.reserved_output_tokens,
        ))
        task = Task(
            id=generate_id("tsk"),
            session_id=sid,
            status="ACTIVE",
            tenant_id=tenant_id,
            assigned_agent_id=agent.id,
            creator_agent_id=agent.id,
            title="User Request",
            description=content_to_text(user_prompt)[:200],
            user_prompt=user_prompt,
            user_prompt_event_jsonable=user_prompt_event_jsonable,
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
        # compat 路径也走同一条会话状态链：这里不经 _register_and_drain（没有队列、
        # 没有 drain），但 run 结束后一样要把「我这边什么情况」报出去，否则 outage /
        # park 的会话状态无人落定（判据只有一条：TM 的聚合信号）。
        task_manager.set_session_manager(self._session_manager)
        self._session_manager.register_session(sid, tenant_id=tenant_id)

        for p in self.providers.get_capability_providers():
            if isinstance(p, ControlCapabilityProvider):
                p.register_session(sid, task_manager, session)
                break
        try:
            try:
                state, handle = await self._execute_task(
                    session=session,
                    task=task,
                    agent=agent,
                    template=template,
                    run_id=generate_id("run"),
                    memory=self.providers.get_memory(),
                    resolved_model=resolved_model,
                    task_manager=task_manager,
                )
            except Exception as e:
                # 崩溃入口（与 TaskManager._run_task 的那条同形、同一个工厂）：_run_loop
                # 重抛，这里交给 TM 落状态发事件，再原样抛给调用方。
                await task_manager.apply_run_outcome(task.id, crash_run_outcome(e))
                raise
            # compat 路径没有队列、不经 _run_task，但一样要有人消费 run 的结局——
            # 否则 outage / park / 取消在这条路径上没人写 task 状态、没人发 task 事件
            # （Task 4 之前是 _run_loop 自己写自己发）。兜底同 _run_task。
            await task_manager.apply_run_outcome(
                task.id,
                state.run_outcome or RunOutcome(
                    kind=RunOutcomeKind.COMPLETED, verdict="success"),
            )
        finally:
            await task_manager.announce_queue_state()
            # **不 forget_session**：状态忘掉之后 SM 就不再「吸收」后续输入了——
            # 冷 HITL 走这条 compat 路径时，park 已把会话推到 WAITING，忘掉之后
            # 应答期的 register_session 拿回默认 RUNNING，之后的 TaskStarted 因
            # 「current 已是 RUNNING」不转移、不发 SessionRunning，投影会整个续跑期
            # 停在 WAITING。本 task 用来替换 _session_done_fired 闩的正是这份记忆。
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
        import dataclasses as _dc

        memory = self.providers.get_memory()
        # 入口即拒、不落库：sm.create_session / sm.resume_session 会立即持久化
        # （instantiate + SESSION_CREATED/RESUMED 事件），所以格式校验与
        # EventBlobStore 门控必须在它们之前。
        # **不做模型能力判断**（spec 2026-08-28-multimodal-adapter-dispatch）：
        # 模态处置归 LLMClient 实现方，core 全程透传。原先为了视觉门控要在这里
        # 惰性解析 LLM，现在整段不碰 LLM，「纯文本不提前解析 LLM」自动成立。
        # 顺序关键：validate 先于 normalize——被拒的内容不该在 blob store 留垃圾。
        blob_store = self.providers.get_memory_blob_store()
        # blob 落盘要一个 session 锚点（宿主按 session_id 登记 workspace），所以
        # 能外部化时必须把 session_id 定下来并透传给 create_session，否则外部化用的
        # session 与真正创建的 session 会是两个 id。
        # 判据必须**两个 store 取或**：memory 侧不可外部化时 event 侧仍会独立把内容
        # ref 化（`content_to_event_jsonable`，且 `validate_content` 的门控只看 event
        # store），只看 memory 侧会让「memory Null + event 真」这一受支持的组合把
        # `session_id=""` 喂给 event blob 的 put，而 create_session 随后又另生成一个真 id。
        can_externalize_either = (
            blob_store.can_externalize or self.providers.get_event_blob_store().can_externalize
        )
        sid = params.session_id or (generate_id("ses") if can_externalize_either else None)
        normalized, user_prompt_event_jsonable = await self._validate_and_normalize_content(
            params.user_prompt, sid or "", tenant_id=params.tenant_id,
        )
        if sid is not None:
            # 两侧都不能外部化时 sid 恒为 params.session_id（可能是 None），此处不改写，
            # 与从前逐字节一致；memory 不可外部化时 `normalized` 就是 params.user_prompt
            # 同一对象，故这一支对纯 event 组合也是无害的。
            params = _dc.replace(params, session_id=sid, user_prompt=normalized)
        sm = self._session_manager
        lm = sm.agent_registry

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
                user_prompt_event_jsonable=user_prompt_event_jsonable,
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
                user_prompt_event_jsonable=user_prompt_event_jsonable,
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

        template = await self._template_lookup.get_template(
            params.template_id,
            None,
            ctx=ProviderContext(session_id=session.id, tenant_id=params.tenant_id),
        )
        task_manager.set_runner(self._make_task_runner(
            session=session,
            template=template,
            template_id=params.template_id,
            lm=lm,
            memory=memory,
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
        # 会话状态的持有者。TM 只往它发事实（announce_queue_state 的三条聚合信号）+
        # 透传 cancel；「有人在等」不再靠注入的 pending-HITL 谓词，而是由 AWAITING_HUMAN
        # 的任务表达（docs/events-v2.md §2.1.1）。
        task_manager.set_session_manager(self._session_manager)
        # 纳入 SM 管理。setdefault 语义，重入安全：多轮对话/恢复重建都会走到这里，
        # 已有状态不被重置（新一轮的显式 RUNNING 由 resume_session 负责）。
        self._session_manager.register_session(session.id, tenant_id=session.tenant_id)
        # 熔断真终结（Task 10）三处注入：cancel 挂起 HITL / 协作取消在途 run / memory 闭合。
        # 均 best-effort——trip 序列本身不因这三者缺失或异常而崩溃（TaskManager 侧已兜底）。
        task_manager.set_cancel_pending_hitl(
            lambda sid=session.id: self._cancel_session_hitl(
                sid, message=CancelReason.FAILURE_THRESHOLD)
        )
        task_manager.set_cancel_inflight(
            lambda tid, sid=session.id: self._cancel_run_token(sid, tid)
        )
        task_manager.set_threshold_finalizer(
            lambda root, ack_tasks, failures, sess=session: self._finalize_threshold_memory(
                sess, root, ack_tasks, failures)
        )
        # 统一取消胶囊闭合（Task 14）：cancel_all / 熔断清场（已启动挂起排队） / 在途协作取消
        # funnel 三处调用点共用同一注入点。
        task_manager.set_cancel_finalizer(
            lambda tasks, reason, sess=session: self._finalize_cancel_memory(sess, tasks, reason)
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
        self._agent_registry.release_session(session_id)
        for _p in self.providers.get_capability_providers():
            if isinstance(_p, SessionScopedCapabilityProvider):
                _p.deregister_session(session_id)

    # ── 熔断真终结（Task 10 runtime 侧）───────────────────────────────────────

    async def _cancel_session_hitl(self, session_id: str, *, message: CancelReason) -> None:
        """取消该 session 全部未决 pending HITL（best-effort，逐个 cancel）。两个调用方：
        熔断 trip 序列第 3 步注入（`message=FAILURE_THRESHOLD`）、`cancel_session` 用户
        主动取消会话（`message=USER_CANCEL`）——各传各的判别值，不再硬编码熔断专属。

        单条取消失败不阻断其余——HitlResolved 需尽量全部先于会话终态发出，但这不是硬要求。
        """
        for req in list(self.hitl_registry.list_pending(session_id=session_id)):
            try:
                await self.hitl.cancel(req.id, message=message)
            except Exception:
                logger.exception(
                    "_cancel_session_hitl: cancel failed for session=%s hitl=%s", session_id, req.id,
                )

    def _cancel_run_token(self, session_id: str, task_id: str) -> bool:
        """trip 序列第 5/6 步注入：对指定在途 task 发协作取消信号（查 `_run_tokens`）。

        只发信号，不代表任务立即终结：本 run 收尾时是否发 TASK_CANCELED 由 `_run_loop`
        finally 的 was_cancelled 守卫按 task.status 判定（熔断已先手标 FAILED 的不发）。
        找不到 token（该 task 此刻并不在跑）→ False。
        """
        tokens = self._run_tokens.get(session_id, {}).get(task_id)
        if tokens is None:
            return False
        tokens.cancel.cancel()
        return True

    async def _finalize_threshold_memory(
        self,
        session: Session,
        root_task: "Task | None",
        ack_tasks: list[Task],
        failures: list[tuple[str, str]],
    ) -> None:
        """trip 序列第 7 步注入：memory 闭合（内联 await，落在 SESSION_FINISHED/SSE 关闭之前）。

        - ack_tasks（已启动带框、被熔断打断的子任务）：把它们的派发对 tool 槽替换为「取消
          文案」——若某条在途任务恰好赶在取消信号前正常收尾，FinalizeStep 的 replace=True
          会再次替换为真实终态，自愈为真相，本次替换不是最后写者也无妨。
        - root finish 对：仅当熔断亲手把 root 判 FAILED 且它已经 started_at（有胶囊可闭）时才写；
          root 已经是别的路径判的终态（比如 FinalizeStep 已闭合）时 caller 传 None，此处直接跳过。
        best-effort per item：单条异常记日志、不阻断其余写入（TaskManager 侧对整个回调的异常
        也已兜底，这里的粒度是"一个坏任务不能拖累其它任务"）。
        """
        from ctx_weft.core.loop.steps.finalize import (
            _ensure_dispatch_frame,
            _put_dispatch_result,
            _synthesize_dispatch_pair,
        )

        memory = self.providers.get_memory()

        for t in ack_tasks:
            try:
                parent_scope = MemoryAddress(
                    session_id=session.id, task_id=t.parent_task_id, agent_id=t.creator_agent_id,
                )
                provider_ctx = ProviderContext(
                    session_id=session.id, tenant_id=session.tenant_id,
                    task_id=t.parent_task_id, agent_id=t.creator_agent_id,
                )
                ts, tool_call_id = await _ensure_dispatch_frame(
                    memory, parent_scope, t, provider_ctx)
                await _put_dispatch_result(
                    memory, parent_scope, t,
                    f"Sub-task '{t.title}' was cancelled mid-run (session failure threshold hit); "
                    f"its partial execution below is incomplete.",
                    ts, provider_ctx, replace=True, tool_call_id=tool_call_id,
                )
            except Exception:
                logger.exception(
                    "_finalize_threshold_memory: ack replace failed for task %s", t.id,
                )

        if root_task is not None and root_task.started_at:
            try:
                scope = MemoryAddress(
                    session_id=session.id, task_id=root_task.id, agent_id=session.root_agent_id,
                )
                provider_ctx = ProviderContext(
                    session_id=session.id, tenant_id=session.tenant_id,
                    task_id=root_task.id, agent_id=session.root_agent_id,
                )
                summary = "Failure threshold hit — consecutive failures: " + "; ".join(
                    f"{i}) {title}: {reason}" for i, (title, reason) in enumerate(failures, start=1)
                )
                await _synthesize_dispatch_pair(
                    memory, scope, root_task,
                    act_recap=(
                        "Session failure threshold was hit (N consecutive sub-task failures); "
                        "terminating this task."
                    ),
                    task_summary=summary,
                    outcome="fail",
                    provider_ctx=provider_ctx,
                    register_bg=False,
                )
            except Exception:
                logger.exception(
                    "_finalize_threshold_memory: root finish pair failed for task %s", root_task.id,
                )

    async def _finalize_cancel_memory(
        self, session: Session, tasks: list[Task], reason: str,
    ) -> None:
        """统一取消胶囊闭合（Task 14）：cancel_all / 熔断清场（已启动挂起排队） / 在途协作取消
        funnel（`on_task_finished(CANCELED)`）三处调用点的公共落点，逐任务调用
        `synthesize_cancel_closure`（ack 终态化 + `[outcome=cancelled]` finish 对，find-only
        对 born-cancel 未铸框的子任务整体跳过）。

        best-effort per task：单条异常记日志、不阻断其余（与 `_finalize_threshold_memory` 同一惯例）。
        """
        from ctx_weft.core.loop.steps.finalize import synthesize_cancel_closure

        memory = self.providers.get_memory()
        for t in tasks:
            try:
                provider_ctx = ProviderContext(
                    session_id=session.id, tenant_id=session.tenant_id,
                    task_id=t.id, agent_id=t.assigned_agent_id or t.creator_agent_id,
                )
                await synthesize_cancel_closure(memory, session.id, t, provider_ctx, reason)
            except Exception:
                logger.exception("_finalize_cancel_memory: closure failed for task %s", t.id)

    def _make_task_runner(
        self,
        *,
        session: Session,
        template: "AgentTemplate",
        template_id: str,
        lm: AgentRegistry,
        memory: MemoryProvider,
        task_manager: TaskManager,
        default_run_id: str,
        handle: "RunHandle | None" = None,
    ) -> "_SessionTaskRunner":
        """构造本 session/run 的两阶段 runner（原闭包工厂的显式化）。

        不再收 llm_account/llm_model：这次要用的模型由 assemble() 经
        `AgentRegistry.materialize`/`resolve_model` 按 agent record 的
        `ModelChoice` 现解，不再是 runner 构造期就定死的会话级值。
        """
        return _SessionTaskRunner(
            runtime=self, session=session, template=template, template_id=template_id,
            lm=lm, memory=memory,
            task_manager=task_manager,
            default_run_id=default_run_id, handle=handle,
        )

    # ── Crash recovery ───────────────────────────────────────────────────────

    async def recover_session(
        self,
        session_id: str,
        *,
        user_reply: "PendingHitl | None" = None,
        resumed_task_id: str | None = None,
        hitl_id: str = "",
    ) -> None:
        """Serialize resume per session, then reuse the live owner or rebuild + drain.

        单 owner 架构：若该 session 已有**存活的 owner TM** 且拥有被应答的 ``resumed_task_id``，
        就把应答作为消息投递给它、就地重驱（``_resume_in_existing_tm``），**不重建 TM**——从根上
        消除"多 TM 顶替/跨 TM 双跑"。仅当无存活 owner（真崩溃冷启动 / ``/resume`` / 活 TM 不含该
        task）才从事件日志重建。per-session 锁把整段过程串行化。

        不收 llm_account/llm_model：续跑路径一概不碰模型（批次 B）。换模型走
        `set_agent_llm`/`set_session_llm` 两条命令，registry 是模型选择的唯一
        住所，续跑只负责把已经存在的选择重新派发出去。

        ``hitl_id``：冷 HITL 应答触发的续跑才有意义——``_resume_after_hitl`` 总是传
        ``req.id``。纯 ``/resume``（无 hitl 语境）留空；活 owner 复用路径里它只在
        approval 分支（无 ``user_reply``）真正被用到，见 `_resume_in_existing_tm`。
        """
        lock = self._resume_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            await self._recover_session_locked(
                session_id, user_reply=user_reply,
                resumed_task_id=resumed_task_id, hitl_id=hitl_id,
            )

    async def _recover_session_locked(
        self,
        session_id: str,
        *,
        user_reply: "PendingHitl | None" = None,
        resumed_task_id: str | None = None,
        hitl_id: str = "",
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
                resumed_task_id=resumed_task_id, hitl_id=hitl_id,
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

        from ctx_weft.core.control.converters import (
            session_from_projection,
            task_from_projection,
        )
        session = session_from_projection(sess_proj)
        # 模型选择不再由续跑覆盖——registry 是唯一住所（`AgentRegistry.load()` 下面
        # 从 `view.agents` 读回，见批次 B）。`session.llm_provider`/`llm_model` 就是
        # SessionCreated 记录的原始值，纯展示用途，派发从不读它们。
        all_tasks = [task_from_projection(tp) for tp in view.tasks.values()]
        # 重放出来的 prompt 带的是 **event ref**（事件 payload 的口径），而它下游要被
        # driver ingest 进 memory。两个 ref 命名空间互不相通，故必须在此过桥：
        # event_blob 取字节 → memory 侧重新归一化。每一步只碰一个 store。
        await self._restore_task_prompts(all_tasks, session_id, sess_proj.tenant_id)

        # 装填 HitlRegistry：**park / 重排的判据从此只读内存**（spec §3.1）。必须在算下面
        # 两个集合之前——registry 空着算出来的 parked 是空集，等于「人还没答，任务却自己
        # 跑起来了」。装填幂等：活 pending 不会被日志里的旧决定盖掉。
        await self.rebuild_hitl(session_id)
        # 有未决 pending HITL 的 task：保持 parked、不重排（人还没答，绝不能自己跑起来）。
        parked_task_ids = {
            r.task_id for r in self.hitl_registry.list_pending(session_id=session_id)
            if r.task_id
        }
        # 挂在**已终局** HITL 上的 task **不在这里另开重排口子**：它们已被 restore 的既有
        # 分支覆盖（ACTIVE → else 分支；SUSPENDED + 子任务全终态 → children 闸门；
        # SUSPENDED + 尚有活子任务 → 子任务收尾时 `_try_resume_parent` 唤醒，而那些子任务
        # 本身也被这一趟 restore 重排了）。硬提前重排反而会把那次合法唤醒静默吞掉
        # （`_try_resume_parent` 以 status == "SUSPENDED" 为门；Task 9 复审 Finding 1）。
        #
        # 崩溃窗口真正会丢的是 **UserTurn 那一类**：它的续跑不是「重排」，而是把人的答复
        # 注入进对话——任务重排了但答复没注入，人说的话就静默消失。见下面
        # `_inject_resolved_user_turns`。

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

        lm = self._agent_registry
        # 跨重启后本进程的 registry 可能是空的：下游经 assemble() 触发的
        # materialize() 一旦撞见未登记的 agent id，需要这份 session 语境才能
        # 回落到正确的 fallback_template_id，而不是「""（无模板）」。幂等：
        # 已登记则不覆盖（同 SessionManager.register_session 口径）。
        lm.register_session(session.id, tenant_id=session.tenant_id, fallback_template_id=template_id)
        template = await self._template_lookup.get_template(
            template_id, None,
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

        # 各 agent 取自己的 template_id（AgentInstantiated 事件投影而来）；投影里没有的
        # （存量事件流）回落 session 模板——喂进 registry，registry 就是那份缓存。
        await self._agent_registry.load(
            view.agents,
            session_id=session.id,
            tenant_id=session.tenant_id,
            fallback_template_id=template_id,
        )

        task_manager.set_runner(self._make_task_runner(
            session=session,
            template=template,
            template_id=template_id,
            lm=lm,
            memory=self.providers.get_memory(),
            task_manager=task_manager,
            default_run_id=generate_id("run"),
        ))
        # act 纯文本暂停（wait_for_user）冷应答：把用户回复注入 task 层并重排（reconcile 覆盖不到,见上）。
        if user_reply is not None:
            await self._inject_user_reply(user_reply, session, task_manager)
        # 崩溃窗口兜底：已终局的 UserTurn 请求，其答复若还没进过对话，在这里补上。
        await self._inject_resolved_user_turns(
            session, task_manager,
            # 与 `restore` 用**同一个**集合：只传 parked 会让一个仍被上一个活 TM 跑着的
            # task 也被补写（复审 Important）。
            parked_or_inflight_task_ids=parked_task_ids | inflight,
            skip_hitl_id=getattr(user_reply, "id", "") if user_reply is not None else "",
        )

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

    async def _restore_task_prompts(
        self, tasks: "list[Task]", session_id: str, tenant_id: str,
    ) -> None:
        """恢复态的 prompt 从 event ref 转回 memory ref。**逐 task、逐字段独立降级：任一
        字段转换失败只降级它自己，不牵连同一 task 的另一字段、不牵连其他 task、更不
        中断整场恢复。**

        转换前先把事件侧的原样形态快照到 `user_prompt_event_jsonable`（见 Task 5）：
        `reopen_task` 要用它发 TASK_REQUEUED，此时它就是从事件里读来的那一份，
        零成本、且与首次发射逐字节相同。**纯文本字段同样要快照**（str 往返即自身），
        否则 reopen 会把「字段没填」误读成「原始 prompt 是空的」（终审 C1）。

        本函数刻意不走 `validate_content`——它的输入直接来自事件重放，不是入口，
        套不上「先 validate 后 normalize」那条不变量（`_validate_and_normalize_content`，
        `runtime.py:573` 附近）。事件日志可能损坏、`EventBlobStore` 实现也可能有 bug，
        `hydrate_event_content` / `normalize_content` 因此可能抛出——`normalize_content`
        对 base64 解码刻意不做 try/except（`content.py` 该函数 docstring 原话），
        正是假定调用方已经过 `validate_content` 筛过一轮，而这里明确没有这层保证。
        抛出时把该字段整体降级成 `downgrade_images_to_text` 产出的确定性文本占位：
        **不允许任何解不开的 ref（event 侧或 memory 侧）流进 task 字段**，并用
        `logger.error` 记下 task id / field name，让问题看得见，而不是被这层降级
        悄悄吞掉。崩溃恢复是最不能再崩一次的地方——一个 task 的坏数据不该拖垮
        整场会话恢复。
        """
        from ctx_weft.core.content import (
            content_to_jsonable, downgrade_images_to_text, hydrate_event_content,
            normalize_content,
        )

        event_blob_store = self.providers.get_event_blob_store()
        blob_store = self.providers.get_memory_blob_store()
        ctx = ProviderContext(session_id=session_id, tenant_id=tenant_id)
        for task in tasks:
            for field_name in ("user_prompt", "original_user_prompt"):
                content = getattr(task, field_name)
                if not content:
                    continue
                # 快照恒先于「要不要转换」的判断：纯文本 prompt 也必须落这一份。
                # str 经 content_to_jsonable 往返即自身、零成本，而少落它的代价是
                # `reopen_task` 拿到 None、被 `_append_text_sections` 当成「base 为空」，
                # 发出的 TASK_REQUEUED 只剩一句修订说明——用户的原始指令在下一次重放
                # 时蒸发（终审 C1）。纯文本恰恰是绝大多数情形。
                setattr(task, f"{field_name}_event_jsonable", content_to_jsonable(content))
                if isinstance(content, str):
                    continue  # 纯文本无 ref 可转，零 blob IO 直通
                try:
                    hydrated = await hydrate_event_content(
                        content, event_blob_store=event_blob_store, ctx=ctx)
                    if blob_store.can_externalize:
                        hydrated = await normalize_content(
                            hydrated, blob_store=blob_store, ctx=ctx)
                except Exception:
                    logger.error(
                        "_restore_task_prompts: task_id=%s field=%s 转换失败，"
                        "降级为纯文本占位（不让解不开的 ref 混进 memory）",
                        task.id, field_name, exc_info=True,
                    )
                    hydrated = downgrade_images_to_text(content)
                setattr(task, field_name, hydrated)

    async def _find_finish_pair_tool_call_id(
        self, memory: MemoryProvider, scope: MemoryAddress, task_id: str, pctx: ProviderContext,
    ) -> str | None:
        """从 memory 找该 task close 时写的占位 finish 对 assistant turn，返回其 finish_task tool_call id。"""
        from ctx_weft.protocols import MemoryAddress, MemoryKind, MemoryScope
        fin = qualify("control:finish_task")
        view = await memory.load_view(
            MemoryAddress(session_id=scope.session_id, agent_id=scope.agent_id),
            MemoryScope.AGENT, pctx, kinds=[MemoryKind.CONVERSATION_TURN],
        )
        for r in reversed(view):  # 新→旧：多副本时最新的 finish 对胜出（同旧 newest-first 语义）
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
        import dataclasses as _dc

        from ctx_weft.core.loop.steps.background_observe import _CLOSE_BOUNDARIES
        try:
            lm = self._agent_registry
            # 水合，不新建：这是重跑一个已存在 agent 打断的段 recap。register_session
            # 保证跨重启后 registry 为空时 materialize 的回落有正确的 session 语境
            # （幂等：session 已登记则不覆盖）。
            lm.register_session(
                session.id, tenant_id=session.tenant_id, fallback_template_id=template_id,
            )
            agent, rm = lm.materialize(agent_id)
            # 窗口以 session.context_limit/reserved_output_tokens 为准（host 配置的预算
            # 天花板，独立于 ModelChoice）——同 _SessionTaskRunner.assemble 的口径。
            agent = _dc.replace(agent, loop_guard=LoopGuard(
                context_limit=session.context_limit,
                reserved_output_tokens=session.reserved_output_tokens,
            ))
            memory = self.providers.get_memory()
            scope = MemoryAddress(session_id=session.id, task_id=task.id, agent_id=agent.id)
            provider_ctx = self._build_provider_ctx(session, task, agent)
            skill_index = self._skill_provider_index()
            assembler = self._build_assembler(memory, provider_ctx, skill_index)
            gateway = self._build_gateway(memory)
            llm = rm.client
            loop_ctx = self._build_loop_ctx(
                assembler, llm, memory, provider_ctx, gateway, skill_index, None, task_manager,
            )
            state = LoopState(
                run_id=generate_id("run"), session=session, task=task, agent=agent,
                scope=scope, extra={"template": template}, resolved_model=rm,
                # 孤儿 run：不走 StepDriver.run，构造时显式钉住 origin，否则
                # launch_background_observe 内部经 make_event(state, ...) 发的
                # 事件 origin 都会是空串。
                origin=EventOrigin.LOOP_BACKGROUND_OBSERVE,
            )
            if boundary in _CLOSE_BOUNDARIES:
                tcid = await self._find_finish_pair_tool_call_id(memory, scope, task.id, provider_ctx)
                if tcid is not None:
                    outcome = "fail" if task.status == "FAILED" else "success"
                    # raw_fold_scope=scope：pending close recap 只在规则 observe 占位 close 时
                    # 存在（延迟折叠，raw 尚 active），重跑替换成功后补删；对已折 raw 是幂等 no-op。
                    register_close_synth(task.id, tcid, scope, outcome, scope)
            launch_background_observe(state, loop_ctx, boundary=boundary)
        except Exception:
            logger.exception("recover: failed to relaunch task recap for task=%s", task.id)

    async def _resume_in_existing_tm(
        self,
        tm: "TaskManager",
        *,
        user_reply: "PendingHitl | None",
        resumed_task_id: str,
        hitl_id: str = "",
    ) -> None:
        """把冷 HITL 应答作为消息投递给**存活的 owner TM**，就地重驱——不重建 TM（单 owner 架构）。

        - 不碰模型：换模型走 `set_agent_llm`/`set_session_llm`，registry 现读现解，
          续跑只管把已经存在的选择重新派发出去（批次 B）。
        - 控制令牌随 run 在派发时发放（per-run registry），无需在此重建。
        - wait_for_user 冷应答注入用户回复到 task 层（`_inject_user_reply` 委托
          `TaskManager.mark_human_resolved` 发 `TaskHumanResolved`）；approval 走 reconcile，由本方法直接调 `resume_task`
          发那条事件——两条路径合起来正好各发一次，不重不漏（D4）。
        - 重排被应答的 task 并重新 drain（``_register_and_drain`` 对同一 TM 幂等：重挂回调 + 派发）。

        ``hitl_id``：仅 approval 分支（``user_reply is None``）用得到，直接传给
        `resume_task`。wait_for_user 分支不需要它——`_inject_user_reply` 用的是自己
        收到的 `user_reply.id`。省略时默认空串，仅供无真实 HITL 语境的既有测试兼容。
        """
        session = tm.session
        if session is None:  # 防御：存活 owner 一定注入过 session
            raise RuntimeError("live TaskManager has no session — cannot resume in place")
        if user_reply is not None:
            await self._inject_user_reply(user_reply, session, tm)
        await tm.resume_task(resumed_task_id, hitl_id=hitl_id)
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

        Calls ``CompactStep.execute`` directly (no step driver) but does emit a
        matching RunStarted/RunFinished pair around it (总账 C5: an orphan run_id
        with no start/finish confused hosts) — the session's projection status is
        still untouched, since RunStarted/RunFinished are reducer no-ops just like
        MemoryCompactStarted / MemoryCompacted.

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
        from ctx_weft.core.state.models import LoopGuard, NormalTaskSettings, Task
        from ctx_weft.protocols import MemoryAddress, ProviderContext

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

            lm = self._agent_registry
            pctx = ProviderContext(session_id=session.id, tenant_id=session.tenant_id)
            # 水合，不新建：target_agent_id 是已存在 agent。register_session 保证跨
            # 重启后 registry 为空时 materialize 的回落有正确的 session 语境（幂等）。
            lm.register_session(
                session.id, tenant_id=session.tenant_id, fallback_template_id=proj.template_id,
            )
            agent, rm = lm.materialize(target_agent_id)
            # materialize 不返回 template（它只读 record，不碰 TemplateLookup）——
            # state.extra 仍需要它（CompactStep 经 extra["template"] 读），单独取一次。
            template = await self._template_lookup.get_template(
                proj.template_id, None, ctx=pctx,
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
                # 跨重启 registry 为空时 materialize 的回落只给得出默认
                # memory_config/loop_config；用刚取到的真实 template 覆盖，
                # 与旧 instantiate_agent(existing_agent_id=...) 每次按 template 现算一致。
                memory_config=template.memory_config,
                loop_config=template.loop_config,
                runtime={"llm_model": rm.model},
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
            llm = rm.client
            loop_ctx = self._build_loop_ctx(
                assembler, llm, memory, provider_ctx, gateway, skill_index, token, None,
            )

            scope = MemoryAddress(session_id=session.id, task_id=task.id, agent_id=agent.id)
            run_id = generate_id("run")
            state = LoopState(
                run_id=run_id,
                session=session,
                task=task,
                agent=agent,
                scope=scope,
                extra={"template": template},
                resolved_model=rm,
                # 孤儿 run：不走 StepDriver.run，构造时显式钉住 origin——下面两条
                # RUN_STARTED/RUN_FINISHED 已各自显式覆盖为 RUNTIME，这里补的是
                # CompactStep.execute 内部经 make_event(state, ...) 发的其余事件
                # （如 MEMORY_COMPACT_* 一类），不补则它们的 origin 是空串。
                origin=EventOrigin.LOOP_COMPACT,
            )
            # 总账 C5：这段独立跑了一个 run_id 却从不发起止——host 会看到凭空出现又
            # 凭空消失的 run。补齐起止事件（payload 结构照抄 `_run_loop` 的实际发射点，
            # 见 task-5-report）；这条路径没有 StepDriver，也没有 RunOutcome，
            # `initial_step` 用它实际跑的那个 step 名 "compact"，`outcome` 按「跑完即
            # 完成 / 崩溃即 interrupted」的既有口径取值。
            await self._event_bus.emit(make_event(state, EventType.RUN_STARTED, payload={
                "run_id": run_id,
                "initial_step": "compact",
            }, origin=EventOrigin.RUNTIME))
            run_error: Exception | None = None
            try:
                outcome = await CompactStep().execute(state, loop_ctx)
                for ev in outcome.events:
                    await self._event_bus.emit(ev)
            except Exception as exc:
                run_error = exc
                raise
            finally:
                await self._event_bus.emit(make_event(state, EventType.RUN_FINISHED, payload={
                    "outcome": (
                        RunOutcomeKind.COMPLETED.value if run_error is None
                        else RunOutcomeKind.INTERRUPTED.value
                    ),
                    "final_status": task.status,
                    "will_retry": False,
                    "total_events": state.sequence_counter,
                    "total_turns": len(state.transcript),
                    "error": str(run_error) if run_error else None,
                    "error_type": type(run_error).__name__ if run_error else None,
                }, origin=EventOrigin.RUNTIME))

            return {"session_id": session.id, "agent_id": agent.id, "task_id": task.id}
        finally:
            self._busy_sessions.discard(session_id)

    def list_pending_hitl(self, session_id: str | None = None) -> "list[HitlRequestView]":
        """未决 HITL 的**只读视图**列表（`session_id=None` = 全部会话）。

        host 面向 HITL 的读入口。刻意不暴露 `HitlRegistry`：`PendingHitl` 是 core 的活
        记录（带等待槽、stage、invocation_key 这些内部键），它自己的 docstring 就写着
        「不出 core」。经由本方法拿到的 `HitlRequestView` 才是契约层类型。

        **只读内存**：注意重启之后 registry 要先被装填（`recover()` / `rebuild_hitl()`）
        才有内容——「恢复是喂进来、不是查回去」（spec §3.1）。
        """
        return [r.to_view() for r in self.hitl_registry.list_pending(session_id=session_id)]

    def list_agents(
        self,
        session_id: str,
        *,
        parent_agent_id: str | None = None,
        include_terminated: bool = False,
    ) -> "list[AgentSummary]":
        """列出该 session 下的 agent（spec 5）——host 面向 agent 发现的读入口。

        不传 `parent_agent_id`：返回该 session 全部 agent 的扁平列表；传了：只返回其
        **直接**子 agent（不展开子孙——层级关系不在接口层嵌套，调用方按 `parent_agent_id`
        自行还原成树）。`include_terminated` 默认 False，避免列表随时间无限膨胀。

        数据源用 `AgentRegistry.agent_ids_of_session`（registry 自扫），不用
        `SessionManager.agent_ids_of`（成员登记表）：后者只在 AGENT_INSTANTIATED /
        AGENT_SPAWNED 时新增、且 runtime 当前从不调用 `forget_session`（见
        `_release_session` 内 "不 forget_session" 的注释），是一份只增不减、
        与 session 同寿命的历史成员名单；而 `AgentRegistry.release_session`
        （由 runtime 的 `_release_session` 在会话终结/取消已空闲会话时调用）会把
        agent 记录从 `_agents` 中真正摘除。若改用前者做 id 源，会话释放之后
        `list_agents` 要么对着已经从 `_agents` 消失的 id 抛 KeyError，要么得再加一层
        "静默跳过缺失记录" 的补丁——不如直接以 `_agents` 自身的 in-memory 现实为准：
        两个来源同出一个 dict，天然自洽，也不会把已经不存在于内存里的 agent 报告出去。
        """
        reg = self._agent_registry
        ids = reg.agent_ids_of_session(session_id)
        if parent_agent_id is not None:
            ids = [i for i in ids if reg._agents[i].parent_agent_id == parent_agent_id]
        out: list[AgentSummary] = []
        for aid in ids:
            rec = reg._agents[aid]
            if not include_terminated and rec.status == "terminated":
                continue
            out.append(AgentSummary(
                agent_id=aid,
                parent_agent_id=rec.parent_agent_id,
                status=rec.status,
                current_task_id=rec.current_task_id,
                spawn_depth=rec.spawn_depth,
            ))
        return out

    def get_agent(self, agent_id: str) -> "AgentDetail":
        """该 agent 的详情视图（spec 5）。未登记的 `agent_id` 抛 `AgentNotFound`。

        `current_task_status`：经 `_task_managers[session_id].get_task(current_task_id)`
        取。两处都可能落空——该 session 的 TaskManager 已被 `_release_session` 回收
        （会话终结/取消已空闲会话后 `_task_managers.pop`），或 task 本身查不到——两种
        情况都不是编程错误，是「这条任务此刻在内存里已经不可寻」的正常状态，因此都
        原样降级成 `None`，不崩、不拿一个假状态字符串糊弄调用方。
        """
        reg = self._agent_registry
        rec = reg._agents.get(agent_id)
        if rec is None:
            raise AgentNotFound(f"unknown agent: {agent_id}")
        task_status: str | None = None
        if rec.current_task_id:
            tm = self._task_managers.get(rec.session_id)
            task = tm.get_task(rec.current_task_id) if tm is not None else None
            task_status = task.status if task is not None else None
        return AgentDetail(
            agent_id=agent_id,
            parent_agent_id=rec.parent_agent_id,
            status=rec.status,
            current_task_id=rec.current_task_id,
            spawn_depth=rec.spawn_depth,
            session_id=rec.session_id,
            template_id=rec.template_id,
            current_task_status=task_status,
        )

    async def send_message(
        self,
        agent_id: str,
        content: "str | list[ContentPart]",
        *,
        session_id: str | None = None,
    ) -> str:
        """向指定 agent 发一条外部消息，返回本次消息落到的 `task_id`（spec §4.1）——
        agent-centric 改造的核心新入口：外部消息从此按 agent 显式寻址，不再隐式挂
        「当前唯一活跃 task」。

        守卫：不存在 / `terminated` / `running` 一律抛错，**不排队**
        （`AgentRegistry.assert_can_receive`，Task 13）；调用方自行重试，或先
        pause/cancel。`session_id` 是可选参数，只用于提前发现「这个 agent 不属于该
        session」这类误用，**不参与路由**——`agent_id` 全局唯一，路由永远只看
        `current_task_id`。

        路由（spec §4.2）：
        - `current_task_id` 对应的 task 已终态（或压根没有）—— 新建一个 task 挂给
          该 agent（`_start_task_for_agent`，走既有 `push_task` 通路）。
        - 未终态 —— 把消息注入这个仍在活的 task 的对话（`_inject_user_turn`，复用
          HITL 回复已经在用的落盘通路）。这一支覆盖了 agent 因 `delegate_task` 处于
          `idle`（当前 task `SUSPENDED` 等子任务）时收到外部消息的场景：消息先落进
          对话，`_try_resume_parent` 在子任务收尾时按既有判据自然唤醒父 task。
        """
        reg = self._agent_registry
        reg.assert_can_receive(agent_id)
        rec = reg._agents[agent_id]
        if session_id is not None and session_id != rec.session_id:
            raise ValueError(
                f"agent {agent_id} belongs to session {rec.session_id!r}, not {session_id!r}"
            )

        current = rec.current_task_id
        if current and not self._task_is_terminal(rec.session_id, current):
            await self._inject_user_turn(current, content, session_id=rec.session_id)
            return current
        return await self._start_task_for_agent(agent_id, content)

    def _task_is_terminal(self, session_id: str, task_id: str) -> bool:
        """`current_task_id` 是否已终态——`send_message` 路由的唯一判据。

        TM 或 task 查无 -> 视为终态：宁可保守地新建一个 task，也不要把外部消息注进
        一个此刻已经不可寻的旧 task（比如该 session 的 TM 已被 `_release_session`
        回收——见 `get_agent` 同一判据下的降级口径）。
        """
        tm = self._task_managers.get(session_id)
        if tm is None:
            return True
        task = tm.get_task(task_id)
        if task is None:
            return True
        return task.status in ("FINISHED", "FAILED", "CANCELED")

    async def _inject_user_turn(
        self, task_id: str, content: "str | list[ContentPart]", *, session_id: str,
    ) -> None:
        """`send_message` 的注入分支：`current_task` 未终态 -> 把新消息当一轮用户
        发言写进该 task 的对话——复用 HITL 回复已经在用的落盘通路
        （`_ingest_user_turn`，从 `_write_hitl_reply_turn` 抽出，两边共用同一次
        memory ingest），不另起一套。

        与 HITL 回复的分野只在"content 怎么来"：那边要从 `PendingHitl.decision`
        派生（拒绝措辞 / 打断续接前缀），这里 content 就是调用方给的原样消息，未经
        任何 HITL 专属加工。

        是否要把 task 重新排进队列执行：
        - `_suspended_on_live_children`（spec §4.2：agent 因 `delegate_task` 处于
          `idle`，当前 task `SUSPENDED` 且仍有未终态子任务）—— 只写记忆、不碰状态：
          `_try_resume_parent` 会在子任务收尾时按 `status == "SUSPENDED"` 这道门
          自然唤醒它，那时它进 act 就看得见这里写下的这一轮（与 `_inject_user_reply`
          完全同一判据、同一处理）。
        - 其余非终态（`PENDING` / `AWAITING_HUMAN` / `INTERRUPTED` / 无子任务的
          `SUSPENDED`）—— `TaskManager.requeue_for_message` 重排：已排队 / 已在跑 /
          已终态它自身 no-op；真正被挡住的会置 `PENDING` 入队并发 `TaskRequeued`
          （**不是** `TaskHumanResolved`——那是 `TaskAwaitingHuman{hitl_id}` 的一对一
          配对解除事件，只属于 HITL 应答路径，发生在这里会留一个配不上对的孤儿事件，
          还会经 ALM 把 agent 状态提前翻成 `running`——task 明明还没真正开跑，见
          `requeue_for_message` 自己的 docstring）。重排成功后补一次 `drain()`——
          `requeue_for_message` 只管入队、不 drain，与 `resume_task` 同一分工。
        """
        tm = self._task_managers.get(session_id)
        if tm is None or tm.session is None:
            raise ValueError(f"no active TaskManager for session {session_id!r}")
        target = tm.get_task(task_id)
        if target is None:
            raise ValueError(f"task {task_id!r} not found in session {session_id!r}")
        session = tm.session
        # agent_id 必须是本 task 对话真正所在的 agent scope，与 `_write_hitl_reply_turn`
        # 走 `_reply_turn_agent_id` 同一回退口径（assigned 优先、creator 兜底）——这里
        # 没有 `PendingHitl.agent_id` 可用（不是 HITL 应答），直接从 task 上取。
        agent_id = target.assigned_agent_id or target.creator_agent_id or ""
        scope = MemoryAddress(session_id=session.id, task_id=target.id, agent_id=agent_id)
        pctx = ProviderContext(
            session_id=session.id, tenant_id=session.tenant_id,
            task_id=target.id, agent_id=agent_id,
        )
        normalized, _event_jsonable = await self._validate_and_normalize_content(
            content, session.id, tenant_id=session.tenant_id,
        )
        await self._ingest_user_turn(
            scope, pctx, normalized, event_id=generate_id("mem"),
            task_id=target.id, source="send_message",
        )
        if _suspended_on_live_children(tm, target):
            logger.info(
                "_inject_user_turn: task %s is SUSPENDED on live children — message "
                "written, state left alone so _try_resume_parent still wakes it",
                target.id)
            return
        # 清旧进展，同 `_inject_user_reply`：新消息意味着有新工作要做，陈旧的
        # outputs/process_report 留着会让 success-guardrail 误判"已经产出过"。
        target.outputs = None
        target.process_report = None
        target.process_report_at = None
        requeued = await tm.requeue_for_message(task_id)
        if requeued:
            asyncio.create_task(tm.drain())

    async def _start_task_for_agent(
        self, agent_id: str, content: "str | list[ContentPart]", **_kw: Any,
    ) -> str:
        """`send_message` 的新建分支：`current_task` 已终态（或压根没有）-> 起一个
        新 task 挂给该 agent，走既有的 `push_task` 通路——与
        `SessionManager._make_root_task_manager` 起 root task 同一套写法，不新造
        一条派发路径。

        复用同一个正在跑的 TaskManager（`self._task_managers[session_id]`）：会话
        建立时 `_register_and_drain` 已经给它 `set_runner` / `set_is_current` /
        挂好 done/idle 回调，这里只管 push 一个新 task 再补一次 drain，不重新接线。

        `assert_can_receive` 已保证 `agent_id` 存在，该 session 的 TM 因此也必然
        还活着——`_release_session` 回收 TM 的同时会一并 `AgentRegistry.
        release_session` 摘掉这个 session 下的全部 agent record（两者同一次调用），
        agent 还在 == TM 还在，故此处直接下标、不再判 None。
        """
        reg = self._agent_registry
        rec = reg._agents[agent_id]
        tm = self._task_managers[rec.session_id]
        normalized, event_jsonable = await self._validate_and_normalize_content(
            content, rec.session_id, tenant_id=rec.tenant_id,
        )
        from ctx_weft.core.content import content_to_text
        task = Task(
            id=generate_id("tsk"),
            session_id=rec.session_id,
            status="ACTIVE",
            tenant_id=rec.tenant_id,
            assigned_agent_id=agent_id,
            creator_agent_id=agent_id,
            title="User Message",
            description=content_to_text(normalized)[:200],
            user_prompt=normalized,
            # 外部消息 = 用户对话：actor 纯文本即暂停等下一条消息（非自动完成）——
            # 与 root task 同一口径（`_make_root_task_manager` 的注释）。
            interaction_mode="interactive",
            created_at=now_utc(),
        )
        await tm.push_task(task, user_prompt_event_jsonable=event_jsonable)
        rec.current_task_id = task.id
        asyncio.create_task(tm.drain())
        return task.id

    async def reply_to_hitl(self, reply: "HitlReply") -> "HitlRequestView | None":
        """host 应答的唯一入口。返回已终局请求的视图；已终局再答 → `None`。

        **冷续跑由本返回值驱动，不挂总线订阅**（spec §7.3 订正）：该总线的 handler
        订阅者在 `emit()` 内部同步 drain，且背压下丢事件——把控制流关键信号挂上去，
        「人答了但会话永不续跑」就成了可能。

        **claimed 分流**：`resolved.claimed` 由 `HitlService._commit` 在取走等待槽的
        同一原子段里判定——True 说明一个活协程正在等这个 hitl_id，投递已把它就地叫醒，
        再触发一次冷续跑就是同一个 task 被驱动两次。
        """
        resolved = await self.hitl.resolve(reply)
        if resolved is None:
            return None                       # 幂等：已终局，不重复续跑
        if resolved.claimed:
            return resolved.to_view()         # 热投递已就地续跑，不得双投
        await self._resume_after_hitl(resolved)
        return resolved.to_view()

    async def _resume_after_hitl(self, req: "PendingHitl") -> None:
        """按 **delivery** 分流续跑——不看 form，不看 capability_id（spec §5）。

        由 `reply_to_hitl` 调用时，`req` 已经**不可逆地终局**（`registry.resolve()`
        已提交、事实已发）——这是「唯一驱动方」路径本身，不是一个可以撤销重试的准备
        阶段。若这里 `recover_session` 抛出，重试 `reply_to_hitl` 只会撞见
        `hitl.resolve()` 对已终局请求的幂等 `None`（不重发事实、不重新触发续跑），
        会话就此永久卡住、且没有第二次机会补上——这正是「既没热投递、也没冷续跑」的
        那个「都没有」路径。异常仍然原样传给调用方（host 需要知道这次应答的续跑没
        成），但**先**用 `hitl_id` 记一条响亮的 exception 日志，让运维不必去反查
        「host 报的这次失败对应哪个已经提交但没跑起来的 HITL」。
        """
        try:
            if isinstance(req.delivery, ToolResultDelivery):
                await self.recover_session(
                    req.session_id, resumed_task_id=req.task_id, hitl_id=req.id,
                )
            elif isinstance(req.delivery, UserTurnDelivery):
                await self.recover_session(
                    req.session_id, user_reply=req, resumed_task_id=req.delivery.task_id,
                    hitl_id=req.id,
                )
            # NoResumeDelivery：纯通知 / 取消，无动作。
        except Exception:
            logger.exception(
                "_resume_after_hitl: cold resume failed after the HITL was already "
                "committed (hitl_id=%s, session_id=%s, task_id=%s, delivery=%s) — "
                "the session will not wake up on its own; a retry of reply_to_hitl "
                "won't help (resolve() is idempotent), this needs manual recovery",
                req.id, req.session_id, req.task_id, type(req.delivery).__name__,
            )
            raise

    async def _inject_user_reply(
        self, req: "PendingHitl", session: Session, task_manager: TaskManager,
    ) -> None:
        """把 act 纯文本暂停（wait_for_user）的用户回复注入对话 **并把 task 掰回可跑状态**。

        = `_write_hitl_reply_turn`（只写记忆，幂等）+ 尾部的 task 状态重置（**不幂等**）。
        只有「正在驱动一次续跑」的调用方才该走这个组合——恢复期的补写用
        `_write_hitl_reply_turn`，见 `_inject_resolved_user_turns`（复审 Critical）。

        **状态重置是有条件的**（复审 I7）：一个「SUSPENDED 且尚有活子任务」的父任务，
        `restore` 刻意不把它重排（它要等子任务收尾时由 `_try_resume_parent` 唤醒，那道门
        正是 `status == "SUSPENDED"`）。在这里无条件翻成 PENDING **却没有人入队**，那次
        合法唤醒就被静默吞掉，父任务永久停摆——与恢复路径上早已拆掉的正是同一个形状。
        `_resume_in_existing_tm` 那条分支不受影响：它随后调 `resume_task()`，会真的入队。

        状态重置与 `TaskHumanResolved` 发射都委托给 `TaskManager.mark_human_resolved`
        （D4；Task 6 收拢），而不是 `resume_task`：重建路径上 `restore()` 早于本方法跑，
        且已把该 task 的状态从 `AWAITING_HUMAN` 翻成了 `PENDING`——等本方法执行到这里时
        状态已经"看不出"曾经被人挡住过，`resume_task` 那种"状态仍是
        AWAITING_HUMAN/SUSPENDED 才发"的判据在这条路径上必然落空。
        `mark_human_resolved` 不看当前状态，只要没走上面 SUSPENDED-on-children 的
        提前返回、且非终态，就发一次——这是 `TaskAwaitingHuman{hitl_id}` 在这条路径上
        唯一的解除点。
        """
        target = task_manager.get_task(req.task_id)
        if target is None:
            logger.warning("wait_for_user cold resume: task %s not found for HITL %s",
                           req.task_id, req.id)
            return
        await self._write_hitl_reply_turn(req, session, target)
        if _suspended_on_live_children(task_manager, target):
            logger.info(
                "_inject_user_reply: task %s is SUSPENDED on live children — reply written, "
                "state left alone so _try_resume_parent still wakes it (hitl=%s)",
                target.id, req.id)
            return
        # 清旧进展（restore 已重排,这里保证进展字段正确）；状态置回与事件发射
        # 交给 TaskManager.mark_human_resolved——它不看当前状态，正对得上
        # restore() 早于本方法跑、已把状态从 AWAITING_HUMAN 翻成 PENDING 的场景
        # （resume_task 的 was_blocked 判据在这里必然落空，见其 docstring）。
        target.outputs = None
        target.process_report = None
        target.process_report_at = None
        await task_manager.mark_human_resolved(target.id, hitl_id=req.id)

    async def _write_hitl_reply_turn(
        self, req: "PendingHitl", session: Session, target: "Task",
    ) -> None:
        """**只把人的答复写进对话，绝不碰 task 状态。**

        拆出来的理由（复审 Critical）：记忆写入靠 `MemoryEvent.id = f"hitlreply:{hitl_id}"`
        幂等，可以在恢复期反复重放；而原先跟在它后面的 task 状态重置
        （`outputs = None` / `status = "PENDING"`）**不幂等**，重放会造成两种实打实的损坏：
        ① 一个 SUSPENDED 在活子任务上的父任务被翻成 PENDING **却没人入队**，
        `_try_resume_parent` 的 `status == "SUSPENDED"` 门随之失效 → 永久停摆；
        ② 早已消费过该答复的 task，其 `outputs` / `process_report` 在此后每次会话恢复时
        被清空 —— 与要关的那个窗口毫无关系的进度损失。

        原 `_inject_user_reply` 的注释与行为在这里逐字保留，仅去掉尾部的状态重置。

        `req` 是新契约的 `PendingHitl`：内容经 `req.decision` 取，打断续接判据经
        `req.delivery.preface`（不再 sniff legacy 的 `context` 字符串）。
        """
        from ctx_weft.core.loop.steps.background_observe import await_pending_background_observe

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
        agent_id = _reply_turn_agent_id(req, target)
        scope = MemoryAddress(session_id=session.id, task_id=target.id, agent_id=agent_id)
        pctx = ProviderContext(
            session_id=session.id, tenant_id=session.tenant_id,
            task_id=target.id, agent_id=agent_id,
        )
        from ctx_weft.core.content import content_with_prefix
        from ctx_weft.protocols.hitl import HITL_OUTCOME_REJECTED

        message = req.decision.message if req.decision else ""
        outcome = req.decision.outcome if req.decision else ""
        is_edit_interrupt = (
            isinstance(req.delivery, UserTurnDelivery)
            and req.delivery.preface == PREFACE_AFTER_INTERRUPT_EDIT
        )

        if outcome == HITL_OUTCOME_REJECTED:
            content = (content_with_prefix(message, "Human declined: ")
                       if message else "Human rejected the request.")
        else:
            content = message or "(no response)"
            # ① 中途打断（未吐 token）续接：补「上一条请求已取消」说明
            # （preface = PREFACE_AFTER_INTERRUPT_EDIT）。
            if is_edit_interrupt:
                from ctx_weft.core.loop.steps.act import _interrupt_edit_prefix
                prev = await self._last_user_prompt(scope, pctx)
                prefix = _interrupt_edit_prefix(prev)
                content = content_with_prefix(content, prefix)
        # 应答可能被重试（host 超时重发 / 用户连点）：`resolve()` 对已终局请求已幂等
        # no-op（不会二次调用本方法），但这里再加一道幂等键——`id` 是 memory 层的幂等键
        # （provider 已实现），确定性地由 hitl_id 派生（spec §7.3/§12.2）。
        await self._ingest_user_turn(
            scope, pctx, content, event_id=f"hitlreply:{req.id}",
            task_id=target.id, source="hitl_reply",
        )

    async def _ingest_user_turn(
        self, scope: "MemoryAddress", pctx: ProviderContext,
        content: "str | list[ContentPart]", *, event_id: str, task_id: str, source: str,
    ) -> None:
        """把一条**已经算好**的用户侧内容，作为一轮 `CONVERSATION_TURN`（role=user）
        写进 `scope`（TASK 视图）——纯落盘这一步，不判断内容该怎么来、也不碰 task 状态。

        `_write_hitl_reply_turn`（HITL 应答，上面）与 `_inject_user_turn`
        （`send_message` 的注入分支，Task 18）共用同一次 `ingest`：两边的差别只在
        content 怎么派生（HITL 要拒绝措辞/打断续接前缀，`send_message` 就是调用方给的
        原样消息）与幂等键怎么起（`hitlreply:{hitl_id}` vs. 一个新生成的 id）——那部分
        差异留在各自调用方，这里不重复实现第二套 ingest。
        """
        from ctx_weft.protocols import MemoryEvent
        await self.providers.get_memory().ingest(
            MemoryEvent(
                id=event_id,
                kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK,
                address=scope,
                content=content,
                timestamp=now_utc(),
                role="user",
                metadata={"task_id": task_id, "source": source},
            ),
            pctx,
        )

    async def _inject_resolved_user_turns(
        self, session: Session, task_manager: TaskManager, *,
        parked_or_inflight_task_ids: "set[str]", skip_hitl_id: str = "",
    ) -> None:
        """恢复期补注入：把该 session 里**已终局的 `UserTurn` 请求**的答复写进对话。

        补的是一个 **`restore` 覆盖不到的洞**（复审 Finding 2）。`ToolResult` 那一类的
        续跑是「补一条 TOOL_RESULT」，由 reconcile 在任务重跑时自己完成；`UserTurn` 的
        续跑却是「把人说的话注入对话」——`restore` 只负责让任务重新入队，注入没人做。
        于是「人答了 → 决定落盘 → 进程在续跑之前崩了」这个窗口里，任务恢复后会重新进
        act，而**人的那句话彻底不见了**。这正是本次重设计要消灭的故障类。

        **只写记忆，绝不碰 task 状态**（`_write_hitl_reply_turn`，复审 Critical）。写入靠
        `MemoryEvent.id = f"hitlreply:{hitl_id}"` 幂等（spec §7.3/§12.2），那个键的存在
        就是为了让这一步可重放：已经注入过 → memory 层 no-op；没注入过 → 这是唯一一次
        把答复救回来的机会。故这里**不需要**记「注入跑没跑过」，那笔账要跨重启，又得多
        一份持久状态（绕回 §3.1）。

        **为什么不碰状态是正确的、而不只是更安全**：parked 真相源改成 registry 之后，
        HITL 已终局的 task 本来就会被 `restore` 重排（ACTIVE → else 分支；SUSPENDED +
        子任务全终态 → children 闸门）。唯一没被重排的形状是「SUSPENDED 在活子任务上」，
        而它恰恰**必须**保持 SUSPENDED——`_try_resume_parent` 以此为门，在子任务收尾时
        唤醒它，那时它进 act 就看得见这里写下的那一轮。三种形状都落对。

        跳过的几类：
        - `skip_hitl_id`：本次调用已由 `user_reply` 显式注入过的那条。
        - `legacy_origin`：本方法关的是**新模型**的崩溃窗口。升级前由 legacy 路径注入过的
          答复，其记忆记录是随机 id、去重不了，补写会凭空多一轮用户发言。
        - `parked_or_inflight_task_ids`：该 task 还挂着别的未决 HITL，或仍在被上一个活
          TaskManager 跑着（与 `restore` 用**同一个**集合——`:1439` 与本处必须一致）。
        - 无 `task_id` / 无 decision 的记录（装填占位项等），以及已终态的 task——
          它不再需要、也不该收到新输入。

        **best-effort**：与 hydration 同一姿态，单条失败只记账、不中断整场恢复。

        ── 代价与它的界（复审 I6）────────────────────────────────────────────
        交互式会话里每一条用户消息都是一次 `UserTurn` HITL，所以「已终局的 UserTurn」
        随会话轮数线性增长，而本方法在**每次冷应答**都会跑一遍。原实现对其中每一条都
        走一次 `_write_hitl_reply_turn`（= 一次 memory ingest，多数是 `hitlreply:` 幂等
        键下的 no-op，外加可能的一次 `_last_user_prompt` 视图读）——O(N) 次写 I/O。

        **界**：先按 task 各读**一次**对话视图，凡是 `hitlreply:{hitl_id}` 已在其中的
        请求直接跳过。这不是启发式：那条记录只可能由 `_write_hitl_reply_turn` 自己写下，
        「在视图里」**就是**「已经注入过」，不存在漏掉一条真正没被续跑的答复的可能。
        视图读不到（provider 不回显 ingest id、记录已被压缩折走）时集合为空 → 退回逐条
        写、行为与加界之前逐字节一致——失败方向朝「多做一次幂等写」，不朝「少救一条答复」。
        于是每次冷应答的代价从 O(N) 次写降到 O(该 session 里有已终局 UserTurn 的 task 数)
        次读。**仍未被界住**的是 `rebuild_hitl` 的整流折叠，见那里的说明。
        """
        candidates: list[tuple[PendingHitl, Any]] = []
        for req in self.hitl_registry.resolved_for_session(session.id):
            if (req.id == skip_hitl_id
                    or req.legacy_origin
                    or not isinstance(req.delivery, UserTurnDelivery)
                    or req.decision is None
                    or not req.task_id
                    or req.task_id in parked_or_inflight_task_ids):
                continue
            target = task_manager.get_task(req.task_id)
            if target is None or target.status in ("FINISHED", "FAILED", "CANCELED"):
                continue                      # 已终态的 task 不再需要（也不该收到）新输入
            candidates.append((req, target))
        if not candidates:
            return

        already: set[str] = set()
        for scope_key in {(t.id, _reply_turn_agent_id(r, t)) for r, t in candidates}:
            already |= await self._injected_reply_ids(session, *scope_key)

        for req, target in candidates:
            if f"hitlreply:{req.id}" in already:
                continue                      # 已经注入过（见上「界」）
            try:
                await self._write_hitl_reply_turn(req, session, target)
            except Exception:
                logger.exception(
                    "_inject_resolved_user_turns: 注入失败 session=%s hitl=%s",
                    session.id, req.id)

    async def _injected_reply_ids(
        self, session: Session, task_id: str, agent_id: str,
    ) -> "set[str]":
        """该 task 对话里已经存在的 `hitlreply:*` 记忆记录 id。读不出来 → 空集（退回逐条写）。

        scope 必须与 `_write_hitl_reply_turn` 写入时**完全一致**（同一个 agent_id，见
        `_reply_turn_agent_id`）——查错 scope 只会查空，退回逐条幂等写，不会误判成
        「已注入」。

        **best-effort，绝不抛**：这是一层纯优化，失败只能让恢复多做几次幂等写，不能
        让整场恢复停下来。
        """
        try:
            view = await self.providers.get_memory().load_view(
                MemoryAddress(session_id=session.id, task_id=task_id, agent_id=agent_id),
                MemoryScope.TASK,
                ProviderContext(session_id=session.id, tenant_id=session.tenant_id,
                                task_id=task_id, agent_id=agent_id),
                kinds=[MemoryKind.CONVERSATION_TURN],
            )
        except Exception:
            logger.debug("_injected_reply_ids: 视图读失败 task=%s，退回逐条幂等写",
                         task_id, exc_info=True)
            return set()
        return {r.id for r in view if isinstance(getattr(r, "id", None), str)
                and r.id.startswith("hitlreply:")}

    async def _last_user_prompt(self, scope: MemoryAddress, pctx: ProviderContext) -> str:
        """取 scope 内最近一条 USER_PROMPT 内容（供 ① 打断续接的「上一条取消」说明）。"""
        from ctx_weft.protocols import MemoryKind, MemoryScope
        try:
            view = await self.providers.get_memory().load_view(
                scope, MemoryScope.TASK, pctx, kinds=[MemoryKind.CONVERSATION_TURN],
            )
        except Exception:
            return ""
        from ctx_weft.core.content import content_to_text
        ups = [r for r in view if r.role == "user"]
        return content_to_text(ups[-1].content) if ups else ""

    async def recover(self) -> int:
        """Recover every still-active session (SessionCreated, no SessionFinished) after a restart.

        Decision is made **in core, from events** (no host projection, no full replay):
        the in-memory ``HitlRegistry`` is refilled (so ``/hitl/pending`` and the reply endpoints
        work), the session is registered with the ``SessionManager``, and **this method stands in
        for the TaskManager**（进程刚起来，`_task_managers` 还是空的）：它拿 `rebuild_hitl`
        刚从日志折出来的未决集合——那正是 TM 会用来聚合的同一份事实——发那一条 TM 信号。
        **恢复不是一种状态**：会话状态照常由 SM 据 TM 的聚合信号判定，恢复路径与正常路径
        走同一条链，SM 的输入类型一个都没变。

        So at startup **nothing drains/runs**: a waiting session waits for a reply, an interrupted
        one waits for ``/resume``. No host callback — the session-level events are handled by the
        host's existing subscribers (projection + SSE). Call in the app lifespan after providers
        are registered, before serving. Returns the count handled.
        """
        try:
            session_ids = await self.event_store.list_active_session_ids()
        except NotImplementedError:
            logger.warning("Recovery: EventStore does not support list_active_session_ids — skipped")
            return 0

        for session_id in session_ids:
            try:
                # 恢复期不再有专门的「PAUSED_HITL vs INTERRUPTED」分支：装填内存 HITL 之后
                # 照常报一句队列状态，会话状态仍由 SM 判定。
                # 「复活不是一种状态」的落地（docs/events-v2.md §2.1.1）。
                # tenant 必须先解出来：`_task_managers` 此刻恒为空（见下）,`_tenant_for_session`
                # 会落到读事件日志那条路（SESSION_CREATED 首条即含真 tenant）——
                # `register_session` 与代发的队列信号都要用同一个值，否则 SM 的 `_states`
                # 留着 "default"，之后由它派生的会话级事件（SessionWaiting 等）照样落错
                # 租户（总账 A5）。
                tenant_id = await self._tenant_for_session(session_id)
                n = await self.rebuild_hitl(session_id)
                self._session_manager.register_session(session_id, tenant_id=tenant_id)
                await self._announce_queue_state_as_tm_proxy(session_id, n, tenant_id=tenant_id)
            except Exception:
                logger.exception("Recovery: failed to recover session %s", session_id)

        return len(session_ids)

    async def _announce_queue_state_as_tm_proxy(
        self, session_id: str, pending_hitl: int, *, tenant_id: str = "default",
    ) -> None:
        """启动恢复期**代 TaskManager** 发那一条队列状态信号（SM 的唯一输入）。

        为什么要代行：`recover()` 跑在进程刚起来的时候，`_task_managers` 恒为空——
        没有代行者的话崩溃会话一条会话级事件都收不到，投影停在崩溃前的 RUNNING，
        「看着在跑却卡住」。而**不能**在这里顺手建一个真 TM：本方法的契约是
        「启动时 nothing drains/runs」，建 TM 有启动即跑活的风险。

        判据与 `TaskManager.announce_queue_state` 同源——有人在等就是 blocked，
        没人在等就是被进程重启打断：

        - 有未决 HITL → ``TaskQueueBlocked{count}``  → SM 判 WAITING
        - 没有        → ``TaskQueueInterrupted``     → SM 判 INTERRUPTED，等 /resume

        「等的是审批面板还是一句话」不在这里区分：那是 delivery 的性质、只有前端需要
        （host 的只读入口 `session_status_after_recover`），会话只有一个 WAITING。

        `tenant_id`：调用方（`recover`）用 `_tenant_for_session` 解出、随 `session_id`
        一并传入——本方法不自己解（避免恢复路径里重复付一次读事件日志的代价）。
        """
        if pending_hitl:
            event_type = EventType.TASK_QUEUE_BLOCKED
            payload: dict = {"count": pending_hitl}
        else:
            event_type = EventType.TASK_QUEUE_INTERRUPTED
            payload = {"reason": "process_restart"}
        await self._event_bus.emit(Event(
            id=generate_id("evt"),
            run_id=None,
            sequence=0,
            session_id=session_id,
            type=event_type,
            timestamp=now_utc(),
            tenant_id=tenant_id,
            origin=EventOrigin.RUNTIME,
            payload=payload,
        ))
        logger.info("Recovery: session %s → %s (%d pending HITL)",
                    session_id, event_type, pending_hitl)

    async def rebuild_hitl(self, session_id: str) -> int:
        """从事件**装填**该 session 的 HITL 内存态，返回 pending 条数。

        **恢复是「喂进来」，不是「查回去」**（spec §3.1）：装填之后 registry 的一切查询
        只读内存，绝不回落去 scan 日志。装填的完备性因此是本路径的责任——漏装的请求
        之后谁也看不见（`list_pending` 看不见 → 任务被误重排；`resolved_for_session`
        看不见 → 人答过的会话永远醒不过来）。

        幂等，可重复调用（`load_snapshot` 对已在内存的活 pending 不覆盖）。启动 `recover`
        用它把 PAUSED 会话的内存态填回来；应答入口也可在内存为空时按需自愈（重启后
        registry 还没被 recover 填上时，据事件即时装填，避免应答 KeyError；spec/07 §9）。

        ── 已知代价：**O(该会话 HITL 事件数)，且这条路每次冷应答都走一遍**（复审 I6）──
        交互式会话里每条用户消息都是一次 `UserTurn` HITL，所以 N 轮对话 ≈ 2N 条 HITL
        事件；每次冷应答重跑一次全量折叠 + 一次装填。读取已经收窄到 `HITL_FOLD_EVENT_TYPES`
        （事件库支持轻查询时不全量回放），blob 还原也只碰**非纯文本**的决定
        （`_hydrate_snapshot_messages` 对 `str` 内容零 blob IO），所以常数很小；但阶数是
        线性的，长会话的每次应答都要付。

        **为什么不截尾**：自然的界是「只折最近一段」，但那会漏掉一条很久以前开出、至今
        未决的请求——`list_pending` 看不见它 ⟹ `parked_task_ids` 少一个 ⟹ 那个任务在人还
        没回答时就被重排跑起来。未决请求的年龄没有上界，任何按条数/时间截尾的界都可能
        踩中它，所以这里**不猜**。真要去掉这个阶数，需要的是事件库侧「只取未终局 HITL」
        的查询能力（或一份 HITL 检查点），那是 provider 契约的改动，不属于本次修复。
        """
        from ctx_weft.core.control.reducers import HITL_FOLD_EVENT_TYPES, fold_hitl_snapshot

        events = await self._read_session_events_of_types(session_id, HITL_FOLD_EVENT_TYPES)
        snapshot = fold_hitl_snapshot(events)
        await self._hydrate_snapshot_messages(snapshot, session_id)
        return self.hitl_registry.load_snapshot(snapshot)

    async def _read_session_events_of_types(
        self, session_id: str, types: "tuple[EventType, ...]",
    ) -> "list[Event]":
        """轻查询取该 session 的指定类型事件；EventStore 未实现轻查询时退化为全量读 + 内存过滤。"""
        try:
            return await self.event_store.read_session_events_of_types(session_id, types)
        except NotImplementedError:
            return [e for e in await self.event_store.read_by_session(session_id)
                    if e.type in types]

    async def _hydrate_snapshot_messages(self, snapshot, session_id: str) -> None:
        """把 `decisions_for` 里的 **event 侧** 内容还原成 memory 侧可用的形态。

        覆盖 `decisions_for` **与** `resolved` 两张表里的全部决定（后者含没有 tool_call_id
        的 `UserTurn` park——它的答复同样要被注入 memory，同样不能带着 event 侧 ref 进去）。

        spec §12.3.3：折叠出来的 message 仍是事件形态（可能是 event blob store 的 ref）。
        直接喂进 memory 会写一个那个 store 永远打不开的引用——图就此静默消失。本方法是
        `fold_hitl_snapshot`（同步、纯函数，结构上做不了 I/O）之后的必经一步，口径与
        现行 `_cold_hitl_decision` 完全一致：hydrate（event → 字节）→ normalize（字节 →
        memory ref），纯文本零成本直通。

        **best-effort，绝不抛**：抛错会卡住整条恢复路径（`recover` 的每个 session、
        `recover_session` 的每次续跑都过这里）。失败一律降级为文本占位——降级本身
        （`downgrade_images_to_text`，纯函数）也在 try 里兜一道，宁可留着原内容也不让
        恢复崩掉。
        """
        # `decisions_for` 与 `resolved` 常指向**同一个** HitlDecision 对象（折叠时同源），
        # 按 id() 去重，避免同一条内容被 hydrate 两遍（第二遍拿到的已是 memory 侧内容，
        # 再喂 hydrate_event_content 会解不开 → 白白降级成占位）。
        targets: dict[int, tuple[str, Any]] = {}
        for key, (decision, _resume_state) in snapshot.decisions_for.items():
            targets.setdefault(id(decision), (str(key), decision))
        for hitl_id, req in snapshot.resolved.items():
            if req.decision is not None:
                targets.setdefault(id(req.decision), (hitl_id, req.decision))
        if not targets:
            return
        from ctx_weft.core.content import (
            downgrade_images_to_text, hydrate_event_content, normalize_content,
        )
        ctx: ProviderContext | None = None
        for key, decision in targets.values():
            message = decision.message
            if not message or isinstance(message, str):
                continue                       # 纯文本无 ref 可转，零 blob IO
            try:
                if ctx is None:
                    ctx = ProviderContext(
                        session_id=session_id,
                        tenant_id=await self._tenant_for_session(session_id))
                hydrated = await hydrate_event_content(
                    message, event_blob_store=self.providers.get_event_blob_store(), ctx=ctx)
                blob_store = self.providers.get_memory_blob_store()
                decision.message = (
                    await normalize_content(hydrated, blob_store=blob_store, ctx=ctx)
                    if blob_store.can_externalize else hydrated)
            except Exception:
                logger.warning(
                    "HITL 恢复：event ref 还原失败，降级为文本占位 (key=%s)", key, exc_info=True)
                try:
                    decision.message = downgrade_images_to_text(message)
                except Exception:                       # pragma: no cover — 纯函数，防御性
                    logger.exception("HITL 恢复：降级占位也失败 (key=%s)", key)

    def _derive_paused_status(self, session_id: str) -> str:
        """由**未决 HITL 的 delivery** 推导**面板提示**；无未决 → `""`。

        判据在 `core.hitl.status.paused_status_for`。返回的 `"PAUSED"` / `"PAUSED_HITL"`
        **不是 `SessionStatus`**——那个值域已把两者合并成 `WAITING`。这里回答的是
        「等的是面板还是一句话」，是 delivery 的性质。唯一去处是下面那个 host 只读入口。
        """
        return paused_status_for(
            r.delivery for r in self.hitl_registry.list_pending(session_id=session_id))

    async def session_status_after_recover(self, session_id: str) -> str:
        """装填该 session 的 HITL 内存态并返回它应处的暂停态（`""` = 无未决，不该暂停）。

        **host 面向的只读查询**：「等的是审批面板（PAUSED_HITL）还是一句话（PAUSED）」是
        `delivery` 的性质、只有前端需要，不上升到会话状态——会话只有一个 WAITING
        （docs/events-v2.md §2.8）。`recover()` 自 Task 6 起不再走这条推导（恢复不分流）。
        """
        await self.rebuild_hitl(session_id)
        return self._derive_paused_status(session_id)

    async def rebuild_all_pending_hitl(self) -> int:
        """据事件重建**所有 active session** 的内存 pending HITL（不发中断、不 drain）,返回总条数。

        供只带 hitl_id 的应答入口（`/hitl/{id}/*`）自愈:重启后内存 registry 为空、又无 session_id
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

    # ── Internal execution ───────────────────────────────────────────────────

    def _build_provider_ctx(self, session: Session, task: Task, agent: Agent) -> ProviderContext:
        return ProviderContext(
            session_id=session.id,
            tenant_id=session.tenant_id,
            task_id=task.id,
            agent_id=agent.id,
            agent_template_id=agent.template_id,
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
            skill_provider_index=skill_index,
            cancel_token=cancel_token,
            task_manager=task_manager,
            hitl=self.hitl,
            waiter=HitlWaiter(self.hitl_registry, timeout_sec=self._hitl_timeout_sec),
            pause_token=pause_token,
            config=self._config,
            # 出网前 rehydrate ref→base64 用（Phase 3b）；未注册时是 NullMemoryBlobStore，
            # rehydrate_content 据其 can_externalize=False 原样返回、零开销。
            blob_store=self.providers.get_memory_blob_store(),
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
        # 段 recap 强一致（spec 2026-07-16 §2）：本 task 若有在途后台 recap
        # （dispatch/interrupt/plain_text 边界），先等它折完再开跑——run 的一切
        # memory 读写都落在折叠结果之上。无 pending 零开销直通。recap 的护栏区
        # （幂等护栏/短段门/事件 emit）在其自吞 try 之外、可能以异常终结，故此处
        # 防御吞掉（降级 = 不等待、段保 raw）；shield 保证 run 被取消时不牵连 recap。
        try:
            await await_pending_background_observe(task.id)
        except Exception:
            logger.exception(
                "_run_loop: pending recap await failed (ignored); task=%s", task.id)

        await self._event_bus.emit(make_event(state, EventType.RUN_STARTED, payload={
            "run_id": run_id,
            "initial_step": initial_step,
        }, origin=EventOrigin.RUNTIME))

        run_error: BaseException | None = None
        was_cancelled = False
        cancel_takes_effect = False
        cancel_source = ""
        try:
            async for outcome in driver.run(state, loop_ctx):
                if outcome.state_patch:
                    state = state.apply_patch(outcome.state_patch)
        except HitlPark as park:
            # run 的结局：这次执行停在「等人答一句」。**task 变成什么不在这里决定**
            # （Task 4）：TaskManager 据本 outcome 走处置表落 AWAITING_HUMAN 并发
            # TaskAwaitingHuman——挡住这个 task 的那一个请求就是 park.hitl_id（act 的
            # tool call 循环是串行的，第一个 park 就 unwind 整个 run，唯一确定）。
            # run_error 保持 None → finally 发 RUN_FINISHED(awaiting_human,
            # will_retry=False)，与委派挂起同形。
            state = state.apply_patch({"run_outcome": RunOutcome(
                kind=RunOutcomeKind.AWAITING_HUMAN, hitl_id=park.hitl_id,
            )})
            logger.info("_run_loop: task %s parked on HITL", task.id)
        except asyncio.CancelledError:
            was_cancelled = True
            # A1 守卫的 run 侧半边：熔断 trip 会**先**把 root 判 FAILED、**再**对在跑的
            # root 发协作取消（内部清场手段），此时这次取消不该把已坐实的终态盖回
            # CANCELED。本 run 不再写 task.status（Task 4：状态归 TM），故把「本来会不会
            # 翻成 CANCELED」记成局部量给下面 RUN_CANCELED 的守卫用；task 侧的同一守卫
            # 长在 TaskManager 的处置入口（终态已坐实 → 不应用 run 的结局）。
            cancel_takes_effect = task.status not in ("FINISHED", "FAILED", "CANCELED")
            # 取消来源（总账 A3 剩下的一条）：token 是本 runtime 的协作取消；否则是外部
            # asyncio 取消（进程 shutdown / `wait_for` 超时）。二者产生同一个
            # `CancelledError`，只有 token 自己能区分。`cancel_token` 不是 `_run_loop`
            # 的形参，实测活在 `loop_ctx.cancel_token`。
            # 裁定（task-3-report.md「裁定后的实现」）：来源标签只进 RUN_CANCELED 的
            # payload，**不进 `RunOutcome.reason`**——后者会流进 `TaskCanceled.payload`，
            # 撞上刻意定下的 R5（CANCELED 不编造 reason，`disposition_for` 里 reason
            # 非空才放键）。RunOutcome.reason 保持空串，与之前行为等价。
            by_token = loop_ctx.cancel_token is not None and loop_ctx.cancel_token.is_cancelled
            cancel_source = "token" if by_token else "external"
            state = state.apply_patch({"run_outcome": RunOutcome(kind=RunOutcomeKind.CANCELED)})
        except LLMOutageError as exc:
            # Task 2（loop 产出 RunOutcome，尚无消费者）：outage 硬编码 retriable=False——
            # **不得**转发 exc.retriable（LLMOutageError.retriable 恒为 True，见
            # protocols/llm.py；今天"outage 从不原地重试"靠的是路径隔离，不是这个标志位，
            # 转发会让 outage 在预算充足时被错误地原地重试。见 task_disposition.py 顶部契约。
            state = state.apply_patch({"run_outcome": RunOutcome(
                kind=RunOutcomeKind.INTERRUPTED, reason=InterruptReason.LLM_OUTAGE,
                error_code=InterruptReason.LLM_OUTAGE, error=str(exc), retriable=False,
            )})
            # 瞬时 LLM 故障自愈耗尽 / 中途断流 → 可恢复中断，**不是** task 失败。
            # task 置 INTERRUPTED（非终态，与 HitlPark 同形）→ _run_task 走挂起分支不判 FINISHED，
            # restore() 在 /resume 时据非终态重排；不发 TASK_FAILED；不增 failure_counter；
            # run_error 保持 None → finally 不再抛出（不经 _handle_task_failure）。
            # task.error / task.error_code 不在这里写：上面构造的 RunOutcome 已带着
            # error=str(exc)、error_code=InterruptReason.LLM_OUTAGE，`_run_task` 拿到
            # 后交 `apply_run_outcome`（task_manager.py）按同样的值写回 task——写两遍是
            # 纯冗余（Task 2 死代码清理）。`announce_queue_state` 读 task.error_code 做
            # 分流发生在 `_settle` 里、`apply_run_outcome` 之后，读到的已经是它写的那份，
            # 时序上稳（见 tests/unit/test_outage_interrupt_reason.py）。
            logger.warning("_run_loop: task %s interrupted by LLM outage: %s", task.id, exc)
            # run 级事实：这次执行死了。**无条件发**，与 task 后续怎么处置无关。
            # 会话状态由 TM 聚合后交给 SM 判定——这里不宣布会话怎么了。
            await self._event_bus.emit(make_event(state, EventType.RUN_INTERRUPTED, payload={
                "reason": InterruptReason.LLM_OUTAGE, "error_message": str(exc)},
                origin=EventOrigin.RUNTIME))
            # task 级事实（TaskInterrupted）不在这里发：outage 的 RunOutcome 带着
            # retriable=False 交给 TaskManager，由处置表判成 INTERRUPTED 并发出——
            # 「outage 从不原地重试」的判据从路径隔离变成了这个显式标志位（Task 4）。
        except Exception as exc:
            run_error = exc
            # 这份 outcome **不是**给 TaskManager 的（本分支下面 `raise run_error`，
            # `state` 根本不返回给调用方；处置用的那份由 `TaskManager._run_task` 的
            # `except Exception` 就地构造）。它是给下面 finally 里 `RUN_FINISHED.outcome`
            # 用的——run 得说出自己是怎么结束的，否则崩溃支会兜底报成 "completed"。
            # retriable 取 `getattr(exc, "retriable", True)`，与 outage 支硬编码的 False
            # **不同源**，不许合并成一份（见 task_disposition.py 顶部契约）。
            state = state.apply_patch({"run_outcome": crash_run_outcome(exc)})
            # 运行层崩溃 = 可恢复中断的临时标记（非终态）：re-raise 交 _handle_task_failure
            # 定夺——原地重试（翻回 PENDING）或挂起等 /resume（保持 SUSPENDED + 发事件）。
            # 真失败只有 observer 判 fail 一条路（FinalizeStep 闭合胶囊、回传父亲）。
            # ContextOverflowError 不再特判终态：retriable=False 使其跳过重试直接挂起，
            # 溢出文案随 task.error / RUN_INTERRUPTED.error_message 抵达 host，错误码
            # 再经 TaskQueueInterrupted.reason 上浮（提示换大窗口模型）。
            # 崩溃发生时 task 是否已是终态（如 observer 已判 FAILED、随后 FinalizeStep 又
            # 抛异常那条窄路径）——是的话下面 RUN_INTERRUPTED 也不发：那次执行的终局
            # 已经由 TaskFailed/RunFinished{FAILED} 宣布过，再发一条 RunInterrupted 会
            # 让「靠类型存在与否判断这次执行是否非正常终止」的 host（docs/events-v2.md
            # §2.4）误报一次「非正常终止」（M1）。与上面 A1 守卫、outage 支的
            # was_interrupted 同一个判据（M3 的教训：别让两处判断各写一份）。
            was_interrupted = task.status not in ("FINISHED", "FAILED", "CANCELED")
            if was_interrupted:
                task.error = str(exc)
            if getattr(exc, "retriable", False):
                logger.warning("_run_loop: task %s failed (retriable): %s", task.id, exc)
            else:
                logger.exception("_run_loop: run failed for task %s", task.id)
            # run 级事实：这次执行确实死了，与 task 接下来是原地重试（TaskRequeued）
            # 还是挂起等 /resume（TaskInterrupted）无关——那个决定归
            # TaskManager._handle_task_failure，在 run 外面、判完才知道；run 域的四条
            # 事实（Started/Canceled/Finished/Interrupted）一律从这里发，别处不发。
            # 但 task 已是终态时不发（见上面 was_interrupted 的注释）。
            if was_interrupted:
                await self._event_bus.emit(make_event(state, EventType.RUN_INTERRUPTED, payload={
                    "reason": InterruptReason.RUN_CRASH,
                    "error_code": crash_error_code(exc),
                    "error_message": str(exc),
                }, origin=EventOrigin.RUNTIME))
        finally:
            self._capability_cache.evict(agent.id)
            # A1 守卫：只有这次取消真的会让 task 翻成 CANCELED 时才发 RUN_CANCELED。熔断
            # trip 序列会先把 root 判 FAILED 再对在跑 root 发协作取消（_cancel_inflight）——
            # 那是内部清场手段，task 已是 FAILED，照发会让 host/postgres 投影把写定的 FAILED
            # 盖回 CANCELED。判据 cancel_takes_effect 在 except 分支里取，与 task 侧的同一
            # 守卫（TaskManager 的处置入口）同源。TASK_CANCELED 不在这里发了（Task 4：task
            # 状态事件只从 TaskManager 出）。RUN_FINISHED 不受此守卫约束，无论如何都发（关 SSE）。
            if was_cancelled and cancel_takes_effect:
                # 取消来源标签（总账 A3）落在这里，不落进 RunOutcome/TaskCanceled：
                # RUN_CANCELED 没有任何 reducer 分支，全仓只有测试查它「发没发」，
                # 对无人消费的事件做纯增量不必顾虑 R5（CANCELED payload 不编造 reason）。
                await self._event_bus.emit(make_event(state, EventType.RUN_CANCELED, payload={
                    "run_id": run_id, "source": cancel_source,
                }, origin=EventOrigin.RUNTIME))
            # will_retry=True suppresses SSE close on the host side.
            # cancelled → False; retriable=False → TaskManager won't retry anyway.
            will_retry = (
                run_error is not None
                and task.retry_count < task.max_retries
                and getattr(run_error, "retriable", True)
            )
            # Task 3：run 自己的结局，run 词表（RunOutcomeKind）五值之一。正常跑完那条路
            # 不经任何 except 分支，`state.run_outcome` 已由 FinalizeStep/SuspendStep 的
            # state_patch 挂好（driver.run 循环里 `state = state.apply_patch(...)`）；
            # 仍为 None 是理论上不可达的兜底（例如驱动没有产出任何 StepOutcome 就正常退出）
            # ——按“没炸、没挂起、没取消”兜底为 COMPLETED，而不是让 host 拿到空 outcome。
            outcome_kind = (
                state.run_outcome.kind if state.run_outcome is not None
                else RunOutcomeKind.COMPLETED
            )
            await self._event_bus.emit(make_event(state, EventType.RUN_FINISHED, payload={
                "outcome": outcome_kind.value,
                # `final_status` 已废弃，下个周期删除——host 应改读上面的 `outcome`
                # （run 词表）。Task 4 起 run 不再写 task 状态，故这里只是**发 RUN_FINISHED
                # 那一刻** task 的状态（多半仍是 ACTIVE）：真正的终态由随后 TaskManager 的
                # 处置写定，靠它推 task 状态的 host 一定要改。
                "final_status": task.status,
                "will_retry": will_retry,
                "total_events": state.sequence_counter,
                "total_turns": len(state.transcript),
                "error": str(run_error) if run_error else None,
                "error_type": type(run_error).__name__ if run_error else None,
            }, origin=EventOrigin.RUNTIME))

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
        resolved_model: ResolvedModel,
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
        # 这次 run 实际用的 client——调用方（AgentBinding.model / run_single_task 的
        # resolved_model）已经解好，这里不再自己解析。三样东西各归各位：选择住
        # agent record，身份/窗口住这份 ResolvedModel，都不回填进 session
        # （回填冻结账号默认的问题见 agent_registry.py ModelChoice 的 docstring）。
        llm = resolved_model.client
        loop_ctx = self._build_loop_ctx(assembler, llm, memory, provider_ctx, gateway, skill_index, cancel_token, task_manager, pause_token=pause_token)

        scope = MemoryAddress(session_id=session.id, task_id=task.id, agent_id=scope_agent_id or agent.id)
        state = LoopState(
            run_id=run_id,
            session=session,
            task=task,
            agent=agent,
            scope=scope,
            extra={"template": template},
            resolved_model=resolved_model,
            # 兜底：StepDriver.run 从 initial_step 开始的每一步都会用 Task 2 的
            # `_STEP_ORIGIN` 表覆盖这个值，这里的 LOOP_DRIVER 只在「驱动器还没跑
            # 第一步就已经有事件要发」这个理论缝隙里生效，不代表任何具体子步骤。
            origin=EventOrigin.LOOP_DRIVER,
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

    原 _make_task_runner 闭包的显式化：闭包捕获 → 实例字段。恢复播种不再靠
    per-runner 缓存——agent 身份/配置的唯一住所是 runtime 级 `AgentRegistry`
    registry（`lm`），恢复路径由 `recover_session` 显式调 `lm.load()` 装填。
    assigned_agent_id 回填 / started_at / TASK_STARTED 均归 TaskManager（两阶段契约）。
    """

    def __init__(
        self,
        *,
        runtime: "CtxWeftRuntime",
        session: Session,
        template: "AgentTemplate",
        template_id: str,
        lm: AgentRegistry,
        memory: MemoryProvider,
        task_manager: TaskManager,
        default_run_id: str,
        handle: "RunHandle | None" = None,
    ) -> None:
        self._runtime = runtime
        self._session = session
        self._template = template
        self._template_id = template_id
        self._registry = lm
        self._memory = memory
        self._task_manager = task_manager
        self._default_run_id = default_run_id
        self._handle = handle

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
                    await self._runtime._template_lookup.resolve_qualified(s.subagent_template, ctx)
                    if s.subagent_template else ""
                ) or self._template_id
                # 分支判据：t.assigned_agent_id 是否为空。真新建走 instantiate——
                # Registry 内部会按因果顺序发 AgentSpawned/SpawnRejected/AgentInstantiated；
                # 已有值时只是按同一 id 重新水合对象（重派发 / 恢复），走 materialize
                # （零事件、只读 record，不会把 spawn_depth 重算成 0）。SpawnDepthExceeded
                # 由 instantiate 内部发完 SpawnRejected 后原样上抛，此处不再捕获。
                if not t.assigned_agent_id:
                    agent, tmpl = await self._registry.instantiate(
                        template_id=sub_tmpl_id, session_id=sess_id, tenant_id=tenant_id,
                        parent_agent_id=t.creator_agent_id, task_id=t.id, ctx=ctx,
                    )
                    # instantiate() 刻意不解模型（惰性不变量）；这里现解一次供
                    # AgentBinding.model——不缓存，现解现弃是设计的一部分
                    # （LLMClientResolver 才是那层缓存）。
                    rm = self._registry.resolve_model(agent.id)
                else:
                    agent, rm = self._registry.materialize(t.assigned_agent_id)
                    # materialize 不返回 template；沿用原行为按 sub_tmpl_id 重新解析
                    # （与 create 分支同一个来源，agent 出身早已由 AgentInstantiated
                    # 事件钉住，这里只是要一份可用的 AgentTemplate 对象）。
                    tmpl = await self._runtime._template_lookup.get_template(
                        sub_tmpl_id, None, ctx=ctx,
                    )
                # 窗口仍以 session.context_limit/reserved_output_tokens 为准——那是
                # host 经 SessionStartParams 显式配置的预算天花板（必填字段，独立于
                # ModelChoice/rm），与「用哪个模型」是两件事：host 完全可能故意配一个
                # 小于模型真实窗口的预算。rm 只贡献 client/身份，不覆盖这里。
                agent = _dc.replace(agent, loop_guard=LoopGuard(
                    context_limit=self._session.context_limit,
                    reserved_output_tokens=self._session.reserved_output_tokens,
                ))
                t.assigned_agent_id = agent.id
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
                return AgentBinding(agent_id=agent.id, agent=agent, template=tmpl,
                                    initial_step=initial, run_id=generate_id("run"), model=rm)

            case _:
                # 非 subagent 任务在**创建者**的 agent scope 上跑（延续创建者对话），
                # 而非一律 root——否则 subagent 派生的非 subagent 子会跑进 root scope、丢失
                # 创建者上下文并污染 root。scope 键与调度串行判定共用 effective_agent_id 单一真相。
                agent, rm = self._registry.materialize(
                    effective_agent_id(t, self._session.root_agent_id or ""),
                )
                # 见上面 subagent 分支同一条注释：窗口以 session 配置为准，rm 只贡献
                # client/身份。
                agent = _dc.replace(agent, loop_guard=LoopGuard(
                    context_limit=self._session.context_limit,
                    reserved_output_tokens=self._session.reserved_output_tokens,
                ))
                initial = await self._reconcile_or(t, agent, "prepare")
                return AgentBinding(agent_id=agent.id, agent=agent, template=self._template,
                                    initial_step=initial, run_id=self._default_run_id, model=rm)

    # ── 阶段 2：执行 ─────────────────────────────────────────────────────────

    async def execute(self, binding: "AgentBinding", task_id: str) -> "RunOutcome | None":
        """驱动 step loop，把 run 的结局交回 TaskManager（Task 4）。

        返回值取自 `_run_loop` **最终返回的那个 state**（`apply_patch` 会换对象，
        入口那个 state 上没有 run_outcome）。返回 None = 这次执行没留下结局（task 已
        不存在），由 TM 按 COMPLETED/success 兜底。崩溃不从这里回：`_run_loop` 重抛，
        TM 的 `except Exception` 就地构造 outcome。
        """
        t = self._task_manager.get_task(task_id)
        if t is None:
            return None
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
                resolved_model=binding.model,
                initial_step=binding.initial_step,
                task_manager=self._task_manager,
                cancel_token=tokens.cancel,
                pause_token=tokens.pause,
            )
        finally:
            self._runtime._deregister_run_tokens(self._session.id, task_id)
        if s is None:
            return None
        if self._handle is not None:
            self._handle._state = s
        return s.run_outcome

    # ── helpers（原闭包内嵌函数）───────────────────────────────────────────────

    async def _reconcile_or(self, t: "Task", agent: "Agent", base: str) -> str:
        """base initial_step；若该 task 最近 assistant turn 有 dangling tool_call → reconcile。"""
        from ctx_weft.protocols.context import ProviderContext as _PCtx
        from ctx_weft.protocols.memory import MemoryAddress as _Scope
        sess_id = self._session.id
        scope = _Scope(session_id=sess_id, task_id=t.id, agent_id=agent.id)
        pctx = _PCtx(session_id=sess_id, tenant_id=self._session.tenant_id,
                     task_id=t.id, agent_id=agent.id)
        if await _task_has_dangling_tool_call(self._memory, scope, pctx):
            return "reconcile"
        return base
