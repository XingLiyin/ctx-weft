"""SuspendStep：当 actor 调用 delegate_task / delegate_plan 时写挂起摘要。

Phase 4 §4.5。
"""

from __future__ import annotations

import logging
from typing import Any

from ctx_weft.core.loop.driver import LoopContext, LoopState, Step, StepOutcome, make_event
from ctx_weft.protocols.events import EventType
from ctx_weft.core.utils import now_utc
from ctx_weft.protocols import MemoryEvent, MemoryKind, MemoryScope

logger = logging.getLogger(__name__)


class SuspendStep(Step):
    """Writes suspension summary to memory and marks task as SUSPENDED."""

    name = "suspend"

    async def execute(self, state: LoopState, ctx: LoopContext) -> StepOutcome:
        task = state.task
        events: list[Any] = []

        # 1) Ingest user prompt if not already
        if task.user_prompt and not task.user_prompt_in_memory:
            await ctx.memory.ingest(
                MemoryEvent(
                    kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK,
                    address=state.scope,
                    content=task.user_prompt,
                    timestamp=now_utc(),
                    role="user",
                    metadata={"task_id": task.id},
                ),
                ctx.provider_ctx,
            )
            task.user_prompt_in_memory = True

        # 2) Build suspension summary from spawn titles written by control tool
        from ctx_weft.core.state.models import NormalTaskSettings
        titles: list[str] = []
        if isinstance(task.settings, NormalTaskSettings):
            titles = list(task.settings.spawn_titles)
            task.settings.spawn_titles = []
        if titles:
            summary = f"Delegated to sub-task(s): {', '.join(repr(t) for t in titles)}. Awaiting completion."
        else:
            summary = "Agent suspended, awaiting sub-task completion."

        # v2 P1（2026-07-27）：不再写 OBSERVER_SUMMARY（不进装配的死写点）；
        # summary 仅进 TASK_SUSPENDED 事件 payload。

        # task.status is already "SUSPENDED" — set by the control tool function body
        events.append(make_event(
            state, EventType.TASK_SUSPENDED,
            payload={
                "task_id": task.id,
                "summary": summary,
                "spawn_titles": titles,
            },
        ))

        # dispatch 段边界（spec 2026-07-16）：父坐实 SUSPENDED 后 fire-and-forget 后台
        # recap，折派发前 raw——挂起空窗跑 LLM。所有委派父生效（不加 _is_own_root 门控）；
        # resume 竞态由 _run_loop 入口 await_pending_background_observe 封死。
        from ctx_weft.core.loop.steps.background_observe import launch_background_observe
        launch_background_observe(state, ctx, boundary="dispatch")

        return StepOutcome(
            next_step=None,  # loop stops; TaskManager will re-queue when children done
            state_patch={"act_exit_reason": "suspended"},
            events=events,
        )
