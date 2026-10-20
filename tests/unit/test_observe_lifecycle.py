"""Observation stages stay durable across errors, retries and old event names."""
import pytest

from ctx_weft.core.control.reducers import reduce_events, deserialize_view, serialize_view
from ctx_weft.core.loop.driver import ExecutionMode, StepExecution, make_event
from ctx_weft.core.loop.steps.observe import begin, complete
from ctx_weft.core.loop.steps.observe import ObserveStep, ObservationResult
from ctx_weft.protocols.events import EventType


async def test_application_failure_keeps_completed_pending(fake_state_ctx, monkeypatch):
    state, ctx = fake_state_ctx
    ctx.execution = StepExecution(ExecutionMode.BACKGROUND, {"boundary": "dispatch", "judge_task": False})
    step = ObserveStep()

    async def fail(*args):
        await complete(state, ctx, ObservationResult(act_recap="saved report"))
        raise OSError("memory unavailable")

    monkeypatch.setattr(step, "_background_body", fail)
    with pytest.raises(OSError, match="memory unavailable"):
        await step.execute(state, ctx)
    types = [event.type for event in ctx.event_bus.emitted]
    assert types == [EventType.OBSERVE_REQUESTED, EventType.OBSERVE_STARTED, EventType.OBSERVE_COMPLETED]
    view = reduce_events(ctx.event_bus.emitted, state.run_id)
    info, = view.pending_observe.values()
    assert info["completion"]["result"]["act_recap"] == "saved report"


async def test_started_is_durable_before_observation_failure(fake_state_ctx, monkeypatch):
    state, ctx = fake_state_ctx
    step = ObserveStep()

    async def fail(*args):
        assert [e.type for e in ctx.event_bus.emitted] == [EventType.OBSERVE_REQUESTED, EventType.OBSERVE_STARTED]
        raise OSError("observation interrupted")

    monkeypatch.setattr(step, "_observe", fail)
    with pytest.raises(OSError):
        await step.execute(state, ctx)
    assert not any(e.type in {EventType.OBSERVE_COMPLETED, EventType.OBSERVE_APPLIED}
                   for e in ctx.event_bus.emitted)


async def test_retry_started_preserves_completion_and_effects(fake_state_ctx):
    state, ctx = fake_state_ctx
    await begin(state, ctx, "dispatch", False)
    await complete(state, ctx, ObservationResult(act_recap="saved report"))
    identity = state.extra["observe_id"]
    effect = make_event(state, EventType.MEMORY_INGESTED, {"source": "root_finish_pair"}, causation_id=identity)
    retry = make_event(state, EventType.OBSERVE_STARTED, {"task_id": state.task.id, "observe_id": identity})
    view = reduce_events([*ctx.event_bus.emitted, effect, retry], state.run_id)
    info = deserialize_view(serialize_view(view)).pending_observe[identity]
    assert info["completion"]["result"]["act_recap"] == "saved report"
    assert info["effects"] == ["MemoryIngested:root_finish_pair"]


@pytest.mark.parametrize("requested", [False, True])
def test_legacy_recap_events_still_fold(fake_state_ctx, requested):
    state, _ = fake_state_ctx
    payload = {"task_id": state.task.id, "observe_id": "legacy", "boundary": "finish"}
    events = [make_event(state, "TaskRecapRequested" if requested else "TaskRecapStarted", payload)]
    view = reduce_events(events, state.run_id)
    assert view.pending_observe["legacy"]["task_id"] == state.task.id
    events.append(make_event(state, "TaskRecapDone", payload))
    assert not reduce_events(events, state.run_id).pending_observe


async def test_foreground_recap_only_also_emits_all_stages(fake_state_ctx):
    state, ctx = fake_state_ctx
    ctx.execution = StepExecution(ExecutionMode.FOREGROUND, {"boundary": "dispatch", "judge_task": False})
    await ObserveStep().execute(state, ctx)
    assert [e.type for e in ctx.event_bus.emitted] == [
        EventType.OBSERVE_REQUESTED, EventType.OBSERVE_STARTED,
        EventType.OBSERVE_COMPLETED, EventType.OBSERVE_APPLIED]
    assert not reduce_events(ctx.event_bus.emitted, state.run_id).pending_observe


async def test_replay_does_not_resurrect_explicitly_canceled_task(fake_state_ctx, monkeypatch):
    state, ctx = fake_state_ctx
    from ctx_weft.core.loop.steps.observe import Verdict
    await begin(state, ctx, "finish_park", True)
    result = ObservationResult(act_recap="saved", verdict=Verdict("success", "saved"))
    completion = await complete(state, ctx, result, action="recap")
    completion["action"] = "close"
    completion["disposition"] = {"status": "FINISHED", "event_type": "TaskFinished", "payload": {}}
    identity = state.extra["observe_id"]
    state.task.status = "CANCELED"
    ctx.execution = StepExecution(ExecutionMode.BACKGROUND,
        {"boundary": "finish_park", "observe_id": identity, "completion": completion})
    async def forbidden(*args):
        raise AssertionError("canceled task must not be observed or finalized")
    step = ObserveStep()
    monkeypatch.setattr(step, "_execute_background", forbidden)
    await step.execute(state, ctx)
    assert state.task.status == "CANCELED"
    assert ctx.event_bus.emitted[-1].type == EventType.OBSERVE_APPLIED


async def test_foreground_request_precedes_step_execution(fake_state_ctx, monkeypatch):
    from ctx_weft.core.loop.driver import StepDriver
    state, ctx = fake_state_ctx
    step = ObserveStep()
    async def fail(*args):
        raise OSError("crash during observation")
    monkeypatch.setattr(step, "_observe", fail)
    with pytest.raises(OSError):
        await StepDriver({"observe": step}).execute("observe", state, ctx)
    types = [e.type for e in ctx.event_bus.emitted]
    assert types[:3] == [EventType.OBSERVE_REQUESTED, EventType.STEP_STARTED, EventType.OBSERVE_STARTED]


async def test_background_intent_survives_failure_before_scheduling(fake_state_ctx, monkeypatch):
    from ctx_weft.core.loop.driver import StepDriver
    state, ctx = fake_state_ctx
    driver = StepDriver({"observe": ObserveStep()})

    def fail_to_schedule(*args, **kwargs):
        assert [e.type for e in ctx.event_bus.emitted] == [EventType.OBSERVE_REQUESTED]
        raise RuntimeError("stopped before task creation")

    monkeypatch.setattr(driver, "start", fail_to_schedule)
    with pytest.raises(RuntimeError, match="before task creation"):
        await driver.start_durable("observe", state, ctx,
                                   options={"boundary": "dispatch", "judge_task": False})
    view = reduce_events(ctx.event_bus.emitted, state.run_id)
    info, = view.pending_observe.values()
    assert info["task_id"] == state.task.id
    assert info["execution_mode"] == "background"
    assert not info.get("completion")


async def test_parked_intent_is_reused_without_separate_request(fake_state_ctx, monkeypatch):
    from ctx_weft.core.loop.driver import StepDriver
    from ctx_weft.core.loop.steps.observe import make_observe_request
    state, ctx = fake_state_ctx
    driver = StepDriver({"observe": ObserveStep()})
    intent = make_observe_request(state, "finish_park", True)
    parked = make_event(state, EventType.HITL_OPENED,
                        {"task_id": state.task.id, "observe_request": intent})
    seen = {}
    def capture_start(name, state, ctx, *, options):
        seen.update(options)
    monkeypatch.setattr(driver, "start", capture_start)
    await driver.start_durable("observe", state, ctx, options=intent)
    assert not ctx.event_bus.emitted
    assert seen == intent
    info = reduce_events([parked], state.run_id).pending_observe[intent["observe_id"]]
    assert info["boundary"] == "finish_park"


async def test_failed_foreground_wakes_close_report_without_applying(fake_state_ctx, monkeypatch):
    import asyncio
    from ctx_weft.core.loop.driver import StepDriver
    from ctx_weft.core.loop.steps.observe import ObserveStep
    state, ctx = fake_state_ctx
    ready = asyncio.Event()
    original_emit = ctx.event_bus.emit
    async def record(event):
        await original_emit(event)
        if event.type == EventType.OBSERVE_COMPLETED:
            ready.set()
    monkeypatch.setattr(ctx.event_bus, "emit", record)
    step = ObserveStep()
    async def report(*args):
        return ObservationResult("durable report", "summary")
    monkeypatch.setattr(step, "_llm_observe", report)
    class FailedFinalize:
        name = "finalize"
        async def execute(self, *args):
            raise OSError("finish memory unavailable")
    driver = StepDriver({"observe": step, "finalize": FailedFinalize()})
    pending = driver.start("observe", state, ctx, options={"boundary": "finish", "judge_task": False})
    async with asyncio.timeout(2):
        await ready.wait()
        with pytest.raises(OSError):
            await driver.execute("finalize", state, ctx)
        with pytest.raises(RuntimeError, match="did not persist a finish pair"):
            await pending
    assert not any(e.type == EventType.OBSERVE_APPLIED for e in ctx.event_bus.emitted)
    view = reduce_events(ctx.event_bus.emitted, state.run_id)
    info, = view.pending_observe.values()
    assert info["completion"]["result"]["act_recap"] == "durable report"
    assert not driver.executions._finish_waiters


async def test_recovered_failed_disposition_does_not_clear_final_answer(fake_state_ctx):
    from ctx_weft.core.loop.steps.finalize import FinalizeStep
    from ctx_weft.core.loop.steps.observe import Verdict
    from ctx_weft.core.models.task import Task
    state, ctx = fake_state_ctx
    state.task = Task(id="t1", session_id="s1", status="INTERRUPTED", outputs="final answer", retry_count=0, max_retries=3)
    state.verdict = Verdict("continue", "")
    decision = {"status": "FAILED", "event_type": "TaskFailed", "payload": {}}
    state.extra.update(observe_decision={"disposition": decision, "verdict": {"task_outcome": "continue"}},
                       observe_effects=["TaskFinalized:"])
    outcome = await FinalizeStep().execute(state, ctx)
    assert state.task.outputs == "final answer"
    assert outcome.events[0].payload["outcome"] == "fail"
    assert outcome.state_patch["run_outcome"].observe_disposition == decision
