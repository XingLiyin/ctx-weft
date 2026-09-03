"""RecognizeIntentStep: single-shot step that fills task title/description and session goal.

Launched concurrently with ActStep (see launch_recognize_intent) on a snapshot of the loop
state, reusing the capability set PrepareStep already bound. Assembles with
purpose="recognize_intent"; CapabilitySource auto-filters tools to update_task_metadata.
Skips entirely if the title is already set or no recognize_intent tool is bound.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any

from ctx_weft.core.content import content_to_text
from ctx_weft.core.loop.driver import LoopContext, LoopState, Step, StepOutcome, make_event
from ctx_weft.core.loop.llm_gateway import (
    stream_llm, apply_dynamic_max_tokens, request_prompt_estimate, resolve_llm_identity,
)
from ctx_weft.core.orchestrator.task_disposition import RunOutcomeKind
from ctx_weft.protocols.events import EventOrigin, EventType
from ctx_weft.core.utils import generate_id
from ctx_weft.protocols.capability import ToolCapability

logger = logging.getLogger(__name__)

# Strong refs to fire-and-forget tasks when no TaskManager is available (avoids GC).
_background_tasks: set[asyncio.Task] = set()


def should_recognize_intent(task: Any) -> bool:
    """True when this is the root task (no parent) and still lacks a title."""
    return getattr(task, "parent_task_id", None) is None and not getattr(task, "title", "")


def launch_recognize_intent(state: LoopState, ctx: LoopContext) -> asyncio.Task:
    """Fire-and-forget: run RecognizeIntentStep concurrently with act on a snapshot state.

    The snapshot shares agent/task/scope/session (read-mostly) but has its own run_id,
    sequence_counter and a copied extra dict, so it never corrupts the live act state.
    Returns the created asyncio.Task (callers may ignore it).
    """
    snapshot = LoopState(
        run_id=generate_id("run"),
        session=state.session,
        task=state.task,
        agent=state.agent,
        scope=state.scope,
        extra=dict(state.extra),
        resolved_model=state.resolved_model,
        # 孤儿 run：不走 StepDriver.run 的每步 origin 赋值机制（Task 2 的
        # `_STEP_ORIGIN`），构造时就得显式钉住，否则这里发的全部事件
        # （含下面 RUN_STARTED/RUN_FINISHED）origin 都会是空串。
        origin=EventOrigin.LOOP_RECOGNIZE_INTENT,
    )

    async def _run() -> None:
        # 总账 C5：这段快照造了自己的 run_id 却从没配起止事件——host 会看到凭空
        # 出现又凭空消失的 run。补齐（payload 结构照抄 `_run_loop` 的实际发射点，
        # 见 task-5-report）；这条路径没有 StepDriver 也没有 RunOutcome，
        # `initial_step` 用它实际跑的那个 step 名 "recognize_intent"。
        await ctx.event_bus.emit(make_event(snapshot, EventType.RUN_STARTED, payload={
            "run_id": snapshot.run_id,
            "initial_step": "recognize_intent",
        }))
        run_error: Exception | None = None
        try:
            await RecognizeIntentStep().execute(snapshot, ctx)
        except Exception as exc:
            run_error = exc
            logger.exception("recognize_intent concurrent run failed (ignored)")
        finally:
            await ctx.event_bus.emit(make_event(snapshot, EventType.RUN_FINISHED, payload={
                "outcome": (
                    RunOutcomeKind.COMPLETED.value if run_error is None
                    else RunOutcomeKind.INTERRUPTED.value
                ),
                "final_status": snapshot.task.status,
                "will_retry": False,
                "total_events": snapshot.sequence_counter,
                "total_turns": len(snapshot.transcript),
                "error": str(run_error) if run_error else None,
                "error_type": type(run_error).__name__ if run_error else None,
            }))

    task = asyncio.create_task(_run())
    tm = getattr(ctx, "task_manager", None)
    if tm is not None and hasattr(tm, "track_background"):
        tm.track_background(task)
    else:
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    return task


class RecognizeIntentStep(Step):
    """Single-shot LLM step that enriches task metadata and session goal."""

    name = "recognize_intent"

    async def execute(self, state: LoopState, ctx: LoopContext) -> StepOutcome:
        target_task = state.task
        target_task_id = target_task.id

        if target_task and target_task.title:
            await ctx.event_bus.emit(make_event(state, EventType.RECOGNIZE_INTENT_SKIPPED, payload={
                "task_id": target_task_id,
                "target_task_id": target_task_id,
            }))
            return StepOutcome(next_step=None)

        bound = list(state.extra.get("bound_capabilities", []))
        has_mf_tool = any(
            isinstance(c, ToolCapability) and "recognize_intent" in c.purposes for c in bound
        )
        if not has_mf_tool:
            logger.warning("RecognizeIntentStep: no recognize_intent tool bound for task %s", target_task_id)
            await ctx.event_bus.emit(make_event(state, EventType.RECOGNIZE_INTENT_SKIPPED, payload={
                "task_id": target_task_id,
                "target_task_id": target_task_id,
                "reason": "no_tools",
            }))
            return StepOutcome(next_step=None)

        # ── Assemble prompt via shared assembler (CapabilitySource filters by purpose) ──
        from ctx_weft.core.assembler.assembler import ContextRequest

        request = ContextRequest(
            purpose="recognize_intent",
            scope=state.scope,
            task=target_task,
            agent=state.agent,
            session=state.session,
            template=state.extra.get("template"),
            bound_capabilities=bound,
            token_counter=ctx.llm.tokenizer.count,
        )
        prompt = await ctx.assembler.assemble(request)

        from ctx_weft.protocols import LLMRequest, LLMUsage

        llm_request = LLMRequest(
            model=resolve_llm_identity(state)[0],
            system=prompt.system,
            messages=prompt.messages,
            tools=prompt.tools,
        )

        await ctx.event_bus.emit(make_event(state, EventType.RECOGNIZE_INTENT_STARTED, payload={
            "task_id": target_task_id,
            "target_task_id": target_task_id,
        }))
        await ctx.event_bus.emit(make_event(state, EventType.RECOGNIZE_INTENT_LLM_PROMPT, payload={
            "system": prompt.system,
            "messages": [
                {"role": m.role, "content": content_to_text(m.content)}
                for m in prompt.messages
            ],
            "tool_names": [t.name for t in prompt.tools],
        }))

        tool_name = ""
        tool_args: dict[str, Any] = {}
        usage = LLMUsage()
        try:
            _guard = getattr(state.agent, "loop_guard", None)
            llm_request.prompt_token_estimate = request_prompt_estimate(
                ctx.llm.tokenizer, llm_request, _guard, None)
            apply_dynamic_max_tokens(ctx, llm_request, _guard)
            async for chunk in stream_llm(
                ctx.llm, llm_request,
                blob_store=getattr(ctx, "blob_store", None),
                provider_ctx=getattr(ctx, "provider_ctx", None),
            ):
                if chunk.kind == "tool_call" and chunk.tool_call:
                    tool_name = chunk.tool_call.name
                    tool_args = chunk.tool_call.arguments
                elif chunk.kind == "usage" and chunk.usage is not None:
                    usage = chunk.usage
        except Exception as exc:
            if getattr(exc, "retriable", False):
                logger.warning("RecognizeIntentStep: LLM call failed for task %s: %s", target_task_id, exc)
            else:
                logger.exception("RecognizeIntentStep: LLM call failed for task %s", target_task_id)
            return StepOutcome(next_step=None)

        if tool_name and ctx.capability_gateway is not None:
            await ctx.capability_gateway.invoke(
                tool_name=tool_name,
                arguments=tool_args,
                state=state,
                ctx=ctx,
            )

        await ctx.event_bus.emit(make_event(state, EventType.RECOGNIZE_INTENT_TOOL_CALL, payload={
            "title": tool_args.get("title", ""),
            "description": tool_args.get("description", ""),
            "session_goal": tool_args.get("session_goal", ""),
        }))
        await ctx.event_bus.emit(make_event(state, EventType.RECOGNIZE_INTENT_COMPLETED, payload={
            "title": tool_args.get("title", ""),
            "description": tool_args.get("description", ""),
            "session_goal": tool_args.get("session_goal", ""),
            # 只透出不记账：意图识别 LLM 开销此前无账可查（spec 2026-07-16 §5.7）
            "usage": dataclasses.asdict(usage),
        }))

        return StepOutcome(next_step=None)
