"""Wait for a parked run in tests that intentionally do not finish their task."""

import asyncio
from contextvars import ContextVar


_review_intent = ContextVar("test_review_intent", default=None)


def track_review_intent(runtime):
    """Expose the assembler's real intent to scripted LLMs in the same task."""
    build = runtime._build_assembler

    def tracked(*args, **kwargs):
        assembler = build(*args, **kwargs)
        assemble = assembler.assemble

        async def capture(request):
            _review_intent.set(request.intent if request.purpose == "observe" else None)
            return await assemble(request)

        assembler.assemble = capture
        return assembler

    runtime._build_assembler = tracked
    return runtime


def is_recap_request(request):
    intent = _review_intent.get()
    return (intent is not None and not intent.judge_task
            and any(tool.name == "control__report_observation" for tool in request.tools))


def install_mechanical_report_fixture(predicate=lambda state: True, *, before=None):
    """Construct the supported mechanical-close/background-report handoff.

    This fixture seeds the legacy durable handoff explicitly: ordinary actor
    exits now use foreground judgement. Report execution, Finalize and recovery
    still run through the real driver and providers.
    """
    from dataclasses import asdict

    from ctx_weft.core.loop.driver import StepDriver, make_event
    from ctx_weft.core.loop.steps.observe import (
        ObservationResult, ObserveStep, apply_foreground_assessment,
        capture_context, make_observe_request, planned_disposition,
    )
    from ctx_weft.protocols.events import EventType

    execute = StepDriver._execute_foreground

    async def handoff(driver, name, state, ctx, *, options=None):
        if name == "finalize" and predicate(state) and state.extra.get("observe_decision"):
            state.extra["observe_id"] = state.extra["observe_decision"]["observe_id"]
        if name != "observe" or not predicate(state):
            return await execute(driver, name, state, ctx, options=options)
        if before is not None:
            await before(state, ctx)
        boundary = (options or {}).get("boundary") or state.act_exit_reason or "normal"
        observation = ObservationResult(verdict=ObserveStep()._mechanical_verdict(state, boundary))
        apply_foreground_assessment(state, observation, boundary)
        # The durable decision owns foreground finalization until its receipt;
        # the report producer itself still executes in BACKGROUND below.
        request = make_observe_request(state, boundary, False, mode="foreground")
        request.update(verdict=asdict(observation.verdict),
                       disposition=asdict(planned_disposition(state, observation)),
                       report_boundary=boundary)
        request["disposition"]["payload"].pop("outputs", None)
        await ctx.task_manager.commit_round(state.task.id)
        await capture_context(state, ctx, request)
        # No foreground Observe invocation owns this report. Only Finalize
        # adopts its ID; canceling the actor must not close the report again.
        state.extra["observe_decision"] = request
        await ctx.event_bus.emit(make_event(state, EventType.OBSERVE_REQUESTED, request))
        await driver.start_durable("observe", state, ctx, options=request)
        outcome = ObserveStep._outcome(observation, "finalize")
        return state.apply_patch(outcome.state_patch), outcome

    StepDriver._execute_foreground = handoff


async def close_runtime(runtime, *, timeout=10.0):
    """Stop test sessions through the public API and join their real producers."""
    for session_id, manager in list(runtime._task_managers.items()):
        await runtime.cancel_session(session_id)
        async with asyncio.timeout(timeout):
            while manager._dispatch_tasks or manager._background_asyncio_tasks:
                await asyncio.sleep(0.01)


async def wait_for_park(handle, *, timeout=5.0):
    async with asyncio.timeout(timeout):
        while True:
            state = handle._state
            recap = (handle._pending_observe(handle.session_id, handle.task_id)
                     if handle._pending_observe else None)
            if state is not None and state.task.status in {
                    "AWAITING_HUMAN", "SUSPENDED", "INTERRUPTED"} and recap is None:
                return state
            await asyncio.sleep(0.01)
