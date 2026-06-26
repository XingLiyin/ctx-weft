"""Step 抽象 + StepDriver。

设计文档 §6.2 / §6.4。
"""

from __future__ import annotations

import dataclasses
import logging
from abc import abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from ctx_weft.core.assembler import AssembledPrompt, ContextAssembler
from ctx_weft.core.events import Event, EventBus, EventType
from ctx_weft.core.events.types import EVENT_TYPES

from ctx_weft.core.state.models import Agent, Session, Task
from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols import (
    LLMClient, MemoryEvent, MemoryEventType, MemoryProvider, MemoryScope, ProviderContext,
)

if TYPE_CHECKING:
    from ctx_weft.core.loop.steps.observe import Verdict
    from ctx_weft.core.loop.steps.act import TurnRecord
    from ctx_weft.core.control.tokens import CancelToken, PauseToken
    from ctx_weft.core.loop.capability_gateway import CapabilityGateway
    from ctx_weft.core.orchestrator import CapabilityCache, TaskManager
    from ctx_weft.core.orchestrator.hitl_manager import HitlManager
    from ctx_weft.protocols.capability import CapabilityProvider
    from ctx_weft.protocols.template import TemplateResolver

logger = logging.getLogger(__name__)


# ── Step / StepOutcome ────────────────────────────────────────────────────────


@dataclass
class StepOutcome:
    """每步执行的统一返回（设计文档 §6.2）。"""

    next_step: str | None
    state_patch: dict[str, Any] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    request_pause: bool = False


@runtime_checkable
class Step(Protocol):
    name: str

    @abstractmethod
    async def execute(
        self,
        state: "LoopState",
        ctx: "LoopContext",
    ) -> StepOutcome: ...


# ── LoopState ─────────────────────────────────────────────────────────────────


@dataclass
class LoopState:
    """Loop 跨 Step 共享的可变状态。Driver 维护，按 state_patch 增量更新。"""

    run_id: str
    session: Session
    task: Task
    agent: Agent
    scope: MemoryScope
    sequence_counter: int = 0  # 每发一个事件 +1

    # 由 PrepareStep 写入
    assembled_prompt: AssembledPrompt | None = None
    # 由 ActStep 写入
    transcript: list[TurnRecord] = field(default_factory=list)
    act_exit_reason: str = ""
    # 由 ObserveStep 写入
    verdict: Verdict | None = None

    # 其他扩展字段
    extra: dict[str, Any] = field(default_factory=dict)

    def apply_patch(self, patch: dict[str, Any]) -> "LoopState":
        """应用 state_patch 返回新 LoopState（浅拷贝）。"""
        if not patch:
            return self
        return dataclasses.replace(self, **patch)


# ── LoopContext ───────────────────────────────────────────────────────────────


@dataclass
class RunPhase:
    """Per-run loop-progress flags (set by ActStep), used to pick the interrupt phase.

    produced      — 本 run 是否吐过 token（区分①未出 token / ②已出 token）。
    in_tool_loop  — 是否已进入工具调用循环（③）。
    """

    produced: bool = False
    in_tool_loop: bool = False


@dataclass
class LoopContext:
    """每次 loop run 一个，包装所有跨 step 的依赖。"""

    assembler: ContextAssembler
    llm: LLMClient
    memory: MemoryProvider
    event_bus: EventBus
    provider_ctx: ProviderContext
    # capability 解析（Phase 3+）
    capability_cache: CapabilityCache|None = None
    capability_providers: list[CapabilityProvider]|None = None
    capability_gateway: CapabilityGateway|None = None
    authorizer: Any = None
    skill_provider_index: dict = None  # provider_name → SkillCapabilityProvider；PrepareStep 用于加载 Level2
    # 控制令牌（Phase 6）
    cancel_token: CancelToken|None = None
    pause_token: PauseToken|None = None
    # run 级阶段标记（ActStep 维护；曾挂在 CancelToken 上）
    run_phase: RunPhase = field(default_factory=RunPhase)
    # 配置
    config: Any = None
    # 模板解析器（Phase 4）
    template_resolver: TemplateResolver|None = None
    # TaskManager 引用（Phase 5+）；PrepareStep compact dispatch 用；None 时退化为 inline compact
    task_manager: TaskManager|None = None
    # HitlManager 引用；ActStep interactive 任务纯文本 park 等用户用；None 时降级为旧的自动完成
    hitl_manager: "HitlManager|None" = None


# ── Helpers ───────────────────────────────────────────────────────────────────


def make_event(
    state: LoopState,
    type: str,
    payload: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    causation_id: str | None = None,
) -> Event:
    """构造一个 Event，自动分配 id + sequence + timestamp。"""
    if type not in EVENT_TYPES:
        # V1 严格：未在白名单的类型直接拒绝（设计文档 §14.3）
        raise ValueError(f"Unknown event type: {type}; not in EVENT_TYPES")
    state.sequence_counter += 1
    return Event(
        id=generate_id("evt"),
        run_id=state.run_id,
        sequence=state.sequence_counter,
        session_id=state.session.id,
        type=type,
        timestamp=now_utc(),
        tenant_id=state.session.tenant_id,
        task_id=state.task.id,
        agent_id=state.agent.id,
        payload=payload or {},
        metadata=metadata or {},
        causation_id=causation_id,
    )


async def _persist_user_prompt(state, ctx) -> None:
    """task 启动时持久化 raw user_prompt（呈现态框架由 composer 渲染期生成，不落库）。"""
    task = state.task
    if not task.user_prompt or task.user_prompt_in_memory:
        return
    from ctx_weft.core.utils import content_to_text, now_utc
    text = (task.user_prompt if isinstance(task.user_prompt, str)
            else content_to_text(task.user_prompt))
    await ctx.memory.ingest(
        MemoryEvent(
            type=MemoryEventType.USER_PROMPT,
            scope=state.scope,
            content=text,
            timestamp=now_utc(),
            role="user",
            metadata={"task_id": task.id},
        ),
        ctx.provider_ctx,
    )
    task.user_prompt_in_memory = True


# ── StepDriver ────────────────────────────────────────────────────────────────


@dataclass
class StepDriver:
    """驱动 Step 链。从 initial_step 开始，按 outcome.next_step 顺序执行。"""

    steps: dict[str, Step]
    initial_step: str = "prepare"

    async def _ensure_blackboard_subscriptions(self, state: LoopState, ctx: LoopContext) -> None:
        """本 task 订阅相关任务结果 topic（幂等）。

        分两类 intent，使 observe 渲染时能区分可操作范围：
          - predecessor：同 plan 前序（tracking_task_ids）——只读上下文
          - subtask：已派生的子任务（children_of）——可被 review / reopen
        """
        tm = ctx.task_manager
        if tm is None:
            return
        task = state.task
        predecessors = set(task.tracking_task_ids or [])
        children = tm.children_of(task.id)
        predecessors.discard(task.id)
        children.discard(task.id)
        children -= predecessors  # 同一 topic 不重复订阅；前序优先按只读处理
        for intent, topics in (("predecessor", predecessors), ("subtask", children)):
            for topic in topics:
                try:
                    await ctx.memory.subscribe_topic(
                        session_id=task.session_id,
                        topic=topic,
                        intent=intent,  # type: ignore[arg-type]
                        ctx=ctx.provider_ctx,
                        task_id=task.id,
                    )
                except Exception:
                    logger.exception("blackboard subscribe failed: task=%s topic=%s", task.id, topic)

    async def run(
        self,
        initial_state: LoopState,
        ctx: LoopContext,
    ) -> AsyncIterator[StepOutcome]:
        state = initial_state

        # 任务启动时立即持久化 raw user_prompt，保证 resume 时对话上下文完整可重建
        # （呈现态框架 ## Current Task/Message 由 composer 渲染期生成，不落库）
        await _persist_user_prompt(state, ctx)

        # Blackboard 订阅：本 task 订阅相关任务的结果 topic，下一次 reason 即可感知。
        # 幂等，每次 run 都执行：① 同 plan 前序（tracking_task_ids）② 已派生的子任务。
        await self._ensure_blackboard_subscriptions(state, ctx)

        next_step_name: str | None = self.initial_step

        while next_step_name is not None:
            # 软打断（interrupt）由 act 的 checkpoint 负责 park，这里不硬取消（否则 step 间命中会误终态）；
            # 仅硬取消（cancel 模式）在 step 边界抛 CancelledError。
            tok = ctx.cancel_token
            if tok is not None and tok.is_cancelled and getattr(tok, "mode", "cancel") == "cancel":
                tok.raise_if_cancelled()

            step = self.steps.get(next_step_name)
            if step is None:
                raise ValueError(f"Step '{next_step_name}' not registered")

            # emit StepStarted
            start_ev = make_event(state, EventType.STEP_STARTED, payload={"step_name": step.name})
            await ctx.event_bus.emit(start_ev)

            try:
                outcome = await step.execute(state, ctx)
            except Exception as e:
                fail_ev = make_event(
                    state, EventType.STEP_FAILED,
                    payload={"step_name": step.name, "error_code": type(e).__name__, "error_message": str(e)},
                )
                await ctx.event_bus.emit(fail_ev)
                raise

            # apply state patch
            if outcome.state_patch:
                state = state.apply_patch(outcome.state_patch)

            # emit collected events
            for ev in outcome.events:
                await ctx.event_bus.emit(ev)

            # emit StepCompleted
            done_ev = make_event(
                state, EventType.STEP_COMPLETED,
                payload={"step_name": step.name, "next_step": outcome.next_step},
            )
            await ctx.event_bus.emit(done_ev)

            yield outcome

            next_step_name = outcome.next_step
