"""Foreground/background scheduling belongs to StepDriver, not individual launchers."""
import asyncio
from types import SimpleNamespace as NS

import pytest

from ctx_weft.core.loop.driver import (
    ExecutionMode,
    ExecutionRegistry,
    StepDriver,
    StepOutcome,
    make_event,
)
from ctx_weft.core.utils.clock import now_utc
from ctx_weft.protocols.events import EventOrigin, EventType


async def test_older_observe_stays_live_after_latest_report_finishes():
    registry = ExecutionRegistry()
    release = asyncio.Event()
    older = asyncio.create_task(release.wait())
    latest = asyncio.create_task(asyncio.sleep(0))
    registry.register("old-run", "task", "observe", older, agent_id="agent", observe_id="old-observe")
    registry.register("latest-run", "task", "observe", latest, agent_id="agent", observe_id="latest-observe")
    await latest
    assert registry.pending_run_id("task") is None
    assert registry.observe_is_live("old-observe")
    assert not registry.observe_is_live("latest-observe")
    waiter = asyncio.create_task(registry.wait_agent_observes("agent"))
    await asyncio.sleep(0)
    assert not waiter.done()
    release.set()
    await older
    assert await waiter
    assert not registry.observe_is_live("old-observe")
    assert not registry.observe_agents


async def test_durable_observe_intent_precedes_enqueue_and_survives_commit_failure(fake_state_ctx):
    state, ctx = fake_state_ctx
    executed = []

    async def execute(snapshot, local_ctx):
        executed.append(snapshot.run_id)
        return StepOutcome(None)

    driver = StepDriver({"observe": NS(name="observe", execute=execute)})
    emit = ctx.event_bus.emit

    async def fail(event):
        raise OSError("intent commit failed")

    ctx.event_bus.emit = fail
    with pytest.raises(OSError):
        await driver.start_durable("observe", state, ctx, options={"boundary": "interrupt"})
    assert not driver.executions.tasks and not executed
    ctx.event_bus.emit = emit
    task = await driver.start_durable("observe", state, ctx, options={"boundary": "interrupt"})
    assert ctx.event_bus.emitted[0].type == EventType.OBSERVE_REQUESTED
    assert not executed
    await task


async def test_two_observe_intents_for_one_task_are_independent(fake_state_ctx):
    from ctx_weft.core.control.reducers import reduce_events, serialize_view, deserialize_view

    state, ctx = fake_state_ctx
    async def execute(snapshot, local_ctx):
        return StepOutcome(None)
    driver = StepDriver({"observe": NS(name="observe", execute=execute)})
    first = await driver.start_durable("observe", state, ctx, options={"boundary": "interrupt"})
    second = await driver.start_durable("observe", state, ctx, options={"boundary": "dispatch"})
    await asyncio.gather(first, second)
    view = reduce_events(ctx.event_bus.emitted, state.run_id)
    assert len(view.pending_observe) == 2
    view = deserialize_view(serialize_view(view))
    keys = list(view.pending_observe)
    assert all(view.pending_observe[key]["segment_end_at"] for key in keys)
    done = make_event(state, EventType.OBSERVE_APPLIED,
                      {"task_id": state.task.id, "observe_id": keys[0]})
    from ctx_weft.core.control.reducers import apply_events
    apply_events([done], view)
    assert list(view.pending_observe) == [keys[1]]


@pytest.mark.parametrize("name", ["observe", "recognize_intent"])
async def test_background_snapshot_context_and_lifecycle(fake_state_ctx, name):
    state, ctx = fake_state_ctx
    seen = {}
    release = asyncio.Event()
    entered = asyncio.Event()
    tracked = []
    ctx.task_manager.track_background = tracked.append
    state.sequence_counter = 9
    state.transcript.append("original")
    state.extra["key"] = "before"
    ctx.run_phase.produced = True

    async def execute(snapshot, local_ctx):
        seen.update(state=snapshot, ctx=local_ctx)
        entered.set()
        await release.wait()
        return StepOutcome(None, {"act_exit_reason": "background-only"})

    driver = StepDriver({name: NS(name=name, execute=execute)})
    before = now_utc()
    pending = driver.start(name, state, ctx, options={"boundary": "interrupt"})
    launched = now_utc()
    assert driver.executions.pending_run_id(state.task.id, name)
    state.extra["key"] = "after"
    state.transcript.append("new")
    await entered.wait()
    snapshot, local_ctx = seen["state"], seen["ctx"]
    assert snapshot is not state and local_ctx is not ctx
    assert snapshot.run_id != state.run_id
    assert snapshot.task is state.task  # TaskManager remains the status authority.
    assert snapshot.extra["key"] == "before"
    assert snapshot.transcript == (["original"] if name == "observe" else [])
    assert local_ctx.run_phase is not ctx.run_phase
    assert not local_ctx.run_phase.produced
    assert local_ctx.execution.mode == ExecutionMode.BACKGROUND
    assert before <= local_ctx.execution.started_at <= launched
    assert ctx.execution.mode == ExecutionMode.FOREGROUND
    assert tracked == [pending]
    release.set()
    result = await pending
    await asyncio.sleep(0)
    assert result.state_patch["act_exit_reason"] == "background-only"
    assert state.act_exit_reason == "" and state.sequence_counter == 9
    assert not driver.executions.tasks and not driver.executions.latest
    events = ctx.event_bus.emitted
    assert [e.type for e in events] == [EventType.RUN_STARTED, EventType.RUN_FINISHED]
    assert [e.sequence for e in events] == [1, 2]
    assert all(e.run_id == snapshot.run_id for e in events)
    origin = (EventOrigin.LOOP_BACKGROUND_OBSERVE if name == "observe"
              else EventOrigin.LOOP_RECOGNIZE_INTENT)
    assert all(e.origin == origin for e in events)
    assert events[-1].payload["outcome"] == "completed"


@pytest.mark.parametrize("name", ["prepare", "act", "compact", "finalize", "suspend", "reconcile"])
async def test_foreground_steps_cannot_be_launched_in_background(fake_state_ctx, name):
    state, ctx = fake_state_ctx
    driver = StepDriver({name: NS(name=name)})
    with pytest.raises(ValueError, match="cannot execute"):
        driver.start(name, state, ctx)
    assert not driver.executions.tasks
    assert not ctx.event_bus.emitted


async def test_metadata_cannot_advance_the_foreground_chain(fake_state_ctx):
    state, ctx = fake_state_ctx
    driver = StepDriver({"recognize_intent": NS(name="recognize_intent")})
    with pytest.raises(ValueError, match="cannot execute"):
        await driver.execute("recognize_intent", state, ctx)


@pytest.mark.parametrize("failure", ["error", "cancel", "next_step"])
async def test_background_failure_and_cancellation_are_reported(fake_state_ctx, failure):
    state, ctx = fake_state_ctx
    entered = asyncio.Event()

    async def execute(state, ctx):
        entered.set()
        if failure == "error":
            raise RuntimeError("failure")
        if failure == "cancel":
            await asyncio.Event().wait()
        return StepOutcome("act")

    driver = StepDriver({"observe": NS(name="observe", execute=execute)})
    task = driver.start("observe", state, ctx)
    await entered.wait()
    if failure == "cancel":
        task.cancel()
    exception = {"error": RuntimeError, "cancel": asyncio.CancelledError, "next_step": ValueError}
    with pytest.raises(exception[failure]):
        await task
    await asyncio.sleep(0)
    assert not driver.executions.tasks
    final = ctx.event_bus.emitted[-1]
    assert final.type == EventType.RUN_FINISHED
    assert final.payload["outcome"] == ("canceled" if failure == "cancel" else "interrupted")
    assert not any(e.type == EventType.STEP_STARTED for e in ctx.event_bus.emitted)


async def test_foreground_event_order_and_options(fake_state_ctx):
    state, ctx = fake_state_ctx

    async def execute(current, local_ctx):
        assert current is state
        assert local_ctx.execution.mode == ExecutionMode.FOREGROUND
        assert local_ctx.execution.options["judge_task"] is True
        assert local_ctx.execution.options["observe_id"]
        assert local_ctx.execution.options["execution_mode"] == "foreground"
        return StepOutcome("finalize", events=[
            make_event(current, EventType.OBSERVE_STARTED, {"task_id": current.task.id})])

    driver = StepDriver({"observe": NS(name="observe", execute=execute)})
    result = await driver.execute("observe", state, ctx, options={"judge_task": True})
    assert result.next_step == "finalize"
    assert [e.type for e in ctx.event_bus.emitted] == [
        EventType.OBSERVE_REQUESTED, EventType.STEP_STARTED,
        EventType.OBSERVE_STARTED, EventType.STEP_COMPLETED]
    assert [e.sequence for e in ctx.event_bus.emitted] == [1, 2, 3, 4]
    assert ctx.execution.options == {}


async def test_next_step_options_are_copied_and_not_inherited(fake_state_ctx):
    state, ctx = fake_state_ctx
    options = {"trigger": "test"}
    seen = []

    async def prepare(current, local_ctx):
        seen.append(dict(local_ctx.execution.options))
        return StepOutcome("compact", next_step_options=options)

    async def compact(current, local_ctx):
        seen.append(dict(local_ctx.execution.options))
        local_ctx.execution.options["trigger"] = "modified"
        return StepOutcome("act")

    async def act(current, local_ctx):
        seen.append(dict(local_ctx.execution.options))
        return StepOutcome(None)

    driver = StepDriver({name: NS(name=name, execute=execute) for name, execute in
                         [("prepare", prepare), ("compact", compact), ("act", act)]})
    outcomes = [outcome async for outcome in driver.run(state, ctx)]
    assert len(outcomes) == 3
    assert seen == [{}, {"trigger": "test"}, {}]
    assert options == {"trigger": "test"}
    assert ctx.execution.options == {}


async def test_foreground_observe_transition_passes_explicit_options(fake_state_ctx):
    state, ctx = fake_state_ctx
    state.act_exit_reason = "max_turns"
    seen = []

    async def act(current, local_ctx):
        return StepOutcome("observe", next_step_options={
            "boundary": "context_limit", "judge_task": False})

    async def observe(current, local_ctx):
        seen.append(dict(local_ctx.execution.options))
        return StepOutcome("finalize")

    async def finalize(current, local_ctx):
        assert local_ctx.execution.options == {}
        return StepOutcome(None)

    driver = StepDriver({name: NS(name=name, execute=execute) for name, execute in
                         [("act", act), ("observe", observe), ("finalize", finalize)]}, initial_step="act")
    _ = [outcome async for outcome in driver.run(state, ctx)]
    assert seen[0]["boundary"] == "context_limit"
    assert seen[0]["judge_task"] is False
    requested = next(e for e in ctx.event_bus.emitted if e.type == EventType.OBSERVE_REQUESTED)
    assert requested.payload["boundary"] == "context_limit"
    assert requested.payload["judge_task"] is False


async def test_registry_waiter_cancellation_does_not_cancel_work():
    registry = ExecutionRegistry()
    release = asyncio.Event()
    task = asyncio.create_task(release.wait())
    registry.register("run", "task", "observe", task, agent_id="agent")
    waiter = asyncio.create_task(registry.wait("task"))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not task.done()
    release.set()
    await task


async def test_foreground_chain_applies_patch_before_completion_event(fake_state_ctx):
    state, ctx = fake_state_ctx
    replacement = NS(**vars(state.task))
    replacement.id = "patched-task"

    async def prepare(current, local_ctx):
        return StepOutcome("act", {"task": replacement})

    async def act(current, local_ctx):
        assert current.task is replacement
        return StepOutcome(None)

    driver = StepDriver({
        "prepare": NS(name="prepare", execute=prepare),
        "act": NS(name="act", execute=act),
    })
    outcomes = [outcome async for outcome in driver.run(state, ctx)]
    assert len(outcomes) == 2
    completed = [e for e in ctx.event_bus.emitted if e.type == EventType.STEP_COMPLETED]
    assert all(e.task_id == "patched-task" for e in completed)
    assert [e.sequence for e in ctx.event_bus.emitted] == [1, 2, 3, 4]


async def test_foreground_failure_keeps_existing_step_events(fake_state_ctx):
    state, ctx = fake_state_ctx

    async def execute(state, ctx):
        raise RuntimeError("failure")

    driver = StepDriver({"act": NS(name="act", execute=execute)})
    with pytest.raises(RuntimeError):
        await driver.execute("act", state, ctx)
    assert [e.type for e in ctx.event_bus.emitted] == [
        EventType.STEP_STARTED, EventType.STEP_FAILED]
    assert ctx.event_bus.emitted[-1].payload["error_code"] == "RuntimeError"


@pytest.mark.parametrize("receipt_lost", [False, True])
@pytest.mark.parametrize("storage_still_down", [False, True])
async def test_storage_failure_does_not_submit_step_failed(
    fake_state_ctx, receipt_lost, storage_still_down,
):
    from ctx_weft.core.events.commit_gate import CommitGate
    from ctx_weft.protocols.events import PersistenceUnavailableError
    from ctx_weft.providers.events import InMemoryEventStore, InProcessEventBus

    state, ctx = fake_state_ctx
    store = InMemoryEventStore()
    bus = InProcessEventBus()
    isolated = []
    bus.attach_commit_gate(CommitGate(
        store, on_unavailable=lambda sid, exc: isolated.append((sid, exc))))
    ctx.event_bus = bus
    original = store.append_batch
    cause = OSError("original commit confirmation unavailable")
    attempts = []
    failed = False

    async def append(sid, bid, events):
        nonlocal failed
        attempts.extend(e.type for e in events)
        if any(e.type == EventType.LLM_PROMPT_SENT for e in events):
            failed = True
            if receipt_lost:
                await original(sid, bid, events)
            raise cause
        if failed and storage_still_down:
            raise OSError("secondary commit failure must not mask original")
        return await original(sid, bid, events)

    store.append_batch = append
    notified = []

    async def collect(event):
        notified.append(event)

    bus.subscribe(None, collect)

    async def execute(current, local_ctx):
        await local_ctx.event_bus.emit(make_event(current, EventType.LLM_PROMPT_SENT, {}))
        return StepOutcome(None)

    driver = StepDriver({"act": NS(name="act", execute=execute)})
    with pytest.raises(PersistenceUnavailableError) as raised:
        await driver.execute("act", state, ctx)
    assert raised.value.__cause__ is cause
    assert isolated == [(state.session.id, cause)]
    assert attempts == [EventType.STEP_STARTED, EventType.LLM_PROMPT_SENT]
    assert [event.type for event in notified] == [EventType.STEP_STARTED]
    stored = await store.read_range(state.session.id)
    assert [row.event.type for row in stored] == [EventType.STEP_STARTED] + (
        [EventType.LLM_PROMPT_SENT] if receipt_lost else [])


async def test_guard_survives_cancelled_waiter_and_cleans_up():
    registry = ExecutionRegistry()
    entered = []

    async def enter(name):
        async with registry.task_guard("task", "observe"):
            entered.append(name)

    async with registry.task_guard("task", "observe"):
        cancelled = asyncio.create_task(enter("cancelled"))
        survivor = asyncio.create_task(enter("survivor"))
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert not entered
        assert registry._guards[("task", "observe")][1] == 2
    await survivor
    assert entered == ["survivor"]
    assert not registry._guards


async def test_registries_isolate_sessions_even_with_equal_task_ids():
    first, second = ExecutionRegistry(), ExecutionRegistry()
    task = asyncio.create_task(asyncio.sleep(0))
    first.register("run", "task", "observe", task, agent_id="agent")
    assert second.pending_run_id("task") is None
    async with first.task_guard("task", "observe"):
        async with second.task_guard("task", "observe"):
            pass
    await task


async def test_mechanical_request_failure_never_executes_observe_or_schedules_report(monkeypatch):
    from tests.unit.test_observe_outcomes import _mech_state_ctx
    from ctx_weft.core.loop.steps.observe import ObserveStep
    state, ctx = _mech_state_ctx(exit_reason="actor_done")
    invoked = []
    async def forbidden(*args):
        invoked.append("execute")
        raise AssertionError("foreground must not execute ObserveStep")
    def forbidden_start(*args, **kwargs):
        invoked.append("start")
        raise AssertionError("uncommitted intent must not start a report")
    async def fail(event):
        assert event.type == EventType.OBSERVE_REQUESTED
        assert event.payload["judge_task"] is True
        assert "verdict" not in event.payload
        raise OSError("requested commit failed")
    monkeypatch.setattr(ObserveStep, "execute", forbidden)
    driver = StepDriver({"observe": ObserveStep()})
    monkeypatch.setattr(driver, "start", forbidden_start)
    ctx.event_bus.emit = fail
    with pytest.raises(OSError, match="requested commit failed"):
        await driver.execute("observe", state, ctx)
    assert not invoked and not driver.executions.tasks
