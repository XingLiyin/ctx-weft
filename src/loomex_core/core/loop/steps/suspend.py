"""SuspendStep：当 actor 调用 delegate_task / delegate_plan 时写挂起摘要。

Phase 4 §4.5。
"""

from __future__ import annotations

import logging
from typing import Any

from loomex_core.core.loop.driver import LoopContext, LoopState, Step, StepOutcome, make_event
from loomex_core.core.events import EventType
from loomex_core.core.utils import now_utc
from loomex_core.protocols import MemoryEvent, MemoryEventType

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
                    type=MemoryEventType.USER_PROMPT,
                    scope=state.scope,
                    content=task.user_prompt,
                    timestamp=now_utc(),
                    role="user",
                    metadata={"task_id": task.id},
                ),
                ctx.provider_ctx,
            )
            task.user_prompt_in_memory = True

        # 2) Build suspension summary from spawn titles written by control tool
        from loomex_core.core.state.models import NormalTaskSettings
        titles: list[str] = []
        if isinstance(task.settings, NormalTaskSettings):
            titles = list(task.settings.spawn_titles)
            task.settings.spawn_titles = []
        if titles:
            summary = f"Delegated to sub-task(s): {', '.join(repr(t) for t in titles)}. Awaiting completion."
        else:
            summary = "Agent suspended, awaiting sub-task completion."

        await ctx.memory.ingest(
            MemoryEvent(
                type=MemoryEventType.OBSERVER_SUMMARY,
                scope=state.scope,
                content=summary,
                timestamp=now_utc(),
                role="assistant",
                metadata={"task_id": task.id, "outcome": "suspended"},
            ),
            ctx.provider_ctx,
        )

        # task.status is already "SUSPENDED" — set by the control tool function body
        events.append(make_event(
            state, EventType.TASK_SUSPENDED,
            payload={
                "task_id": task.id,
                "summary": summary,
                "spawn_titles": titles,
            },
        ))

        return StepOutcome(
            next_step=None,  # loop stops; TaskManager will re-queue when children done
            state_patch={"act_exit_reason": "suspended"},
            events=events,
        )
