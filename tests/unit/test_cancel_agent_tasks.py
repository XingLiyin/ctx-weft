"""Agent state and task dispatch remain separate during cancellation."""
import asyncio

import pytest

from ctx_weft.core.models.task import Task
from ctx_weft.core.orchestrator.task.hooks import TaskManagerHooks
from ctx_weft.core.orchestrator.lifecycle.agent_state import AgentInput, AGENT_CANCEL_REASON
from tests.unit.test_out_of_band_verdict import _tm
from tests.unit.test_runtime_agent_api import _rt, _plant


async def test_task_tree_scope_and_active_run_order():
    tm, bus = _tm()
    tasks = [Task(id=tid, session_id="s1", status=status, parent_task_id=parent,
                  creator_agent_id=agent, assigned_agent_id=agent)
             for tid, status, parent, agent in [
                 ("root", "SUSPENDED", None, "root-agent"),
                 ("child", "SUSPENDED", "root", "worker"),
                 ("grand", "ACTIVE", "child", "worker"),
                 ("unrelated-same", "PENDING", None, "root-agent"),
                 ("unrelated-other", "PENDING", None, "worker")]]
    tm.restore(tasks, set(), enqueue=False)
    tm._running_tasks.add("grand")
    tm._dispatch_tasks["grand"] = asyncio.current_task()
    tm._running_agents["grand"] = "worker"
    signaled = []
    tm.set_hooks(TaskManagerHooks(cancel_inflight=signaled.append))
    targets = await tm.cancel_task_tree("root", AGENT_CANCEL_REASON + "test")
    assert set(targets) == {"root", "child", "grand"}
    assert signaled == ["grand"]
    assert tm.get_task("grand").status == "ACTIVE"
    assert tm.get_task("root").status == tm.get_task("child").status == "CANCELED"
    assert all(tm.get_task(tid).status == "PENDING" for tid in ("unrelated-same", "unrelated-other"))
    assert not any(e.task_id == "grand" and e.type == "TaskCanceled" for e in bus.events)
    while tm._background_asyncio_tasks:
        await asyncio.gather(*list(tm._background_asyncio_tasks))


async def test_dispatch_current_task_overrides_queued_routing_hint():
    tm, _ = _tm()
    running = Task(id="running", session_id="s1", status="ACTIVE", assigned_agent_id="agent")
    queued = Task(id="queued", session_id="s1", status="PENDING", assigned_agent_id="agent")
    tm.restore([running, queued], set(), enqueue=False)
    tm._running_tasks.add("running")
    tm._running_agents["running"] = "agent"
    assert tm.current_task_for_agent("agent", "queued") == "running"


async def test_failed_interruption_persistence_does_not_cancel_tasks(monkeypatch):
    rt = _rt()
    _plant(rt, "agent", None, status="waiting_human")
    lm = rt._agent_lifecycle_manager
    lm._agents["agent"].current_task_id = "task"
    tm = rt._task_managers["s1"]
    tm.register_task(Task(id="task", session_id="s1", status="AWAITING_HUMAN", assigned_agent_id="agent"))
    await rt.initialize()
    async def fail(event):
        raise OSError("interruption commit failed")
    monkeypatch.setattr(lm.event_bus, "emit", fail)
    with pytest.raises(OSError, match="interruption commit failed"):
        await rt.cancel_agent("agent")
    assert lm.status_of("agent") == "waiting_human"
    assert tm.get_task("task").status == "AWAITING_HUMAN"


async def test_late_waiting_event_cannot_overtake_interruption(monkeypatch):
    rt = _rt()
    _plant(rt, "agent", None, status="running")
    lm = rt._agent_lifecycle_manager
    original = lm.event_bus.emit
    waiting, release = asyncio.Event(), asyncio.Event()
    emitted = []
    async def delayed(event):
        if event.type == "AgentWaitingHuman":
            waiting.set()
            await release.wait()
        await original(event)
        emitted.append(event.type)
    monkeypatch.setattr(lm.event_bus, "emit", delayed)
    old = asyncio.create_task(lm.apply_input("agent", AgentInput.AWAITING_HUMAN))
    await waiting.wait()
    interrupt = asyncio.create_task(lm.apply_input("agent", AgentInput.CANCEL, reason=AGENT_CANCEL_REASON + "test"))
    release.set()
    await asyncio.gather(old, interrupt)
    assert emitted == ["AgentWaitingHuman", "AgentTerminated"]
    assert lm.status_of("agent") == "terminated"


async def test_concurrent_cancel_finalizers_publish_one_completion():
    tm, bus = _tm()
    task = Task(id="task", session_id="s1", status="CANCELED", error=AGENT_CANCEL_REASON + "test")
    tm.register_task(task)
    locks = {}
    calls = []
    async def finalize(tasks, reason):
        calls.append(tasks[0].id)
        await asyncio.sleep(.01)
    tm.set_hooks(TaskManagerHooks(tree_cancel_finalizer=finalize,
        cancel_guard=lambda tid, name="finish_report": locks.setdefault((tid, name), asyncio.Lock())))
    await asyncio.gather(tm._finalize_tree_cancellations([task], task.error),
                         tm._finalize_tree_cancellations([task], task.error))
    assert calls == ["task"]
    assert sum(e.type == "TaskFinalized" for e in bus.events) == 1
    assert task.finalized


def test_snapshot_retains_unfinished_cancel_effects_and_their_ancestor():
    from ctx_weft.core.control.types import RunStateView, TaskView
    from ctx_weft.core.control.reducers import prune_view_for_snapshot, serialize_view, deserialize_view
    view = RunStateView(run_id="", session_id="s1", task_id="", agent_id="")
    view.tasks["root"] = TaskView(id="root", session_id="s1", status="CANCELED", finalized=True,
                                  error=AGENT_CANCEL_REASON + "test")
    view.tasks["child"] = TaskView(id="child", session_id="s1", status="CANCELED", parent_task_id="root",
                                   error=AGENT_CANCEL_REASON + "test", finalized=False)
    restored = deserialize_view(serialize_view(prune_view_for_snapshot(view)))
    assert set(restored.tasks) == {"root", "child"}
    assert not restored.tasks["child"].finalized
    restored.tasks["child"].finalized = True
    assert not prune_view_for_snapshot(restored).tasks


async def test_durable_accepted_verdict_is_restored_before_interrupt_arbitration():
    tm, bus = _tm()
    task = Task(id="task", session_id="s1", status="AWAITING_HUMAN", assigned_agent_id="root")
    tm.register_task(task)
    await tm.restore_observe_decisions({"obs": {"task_id": "task", "completion": {
        "accepted": True, "action": "close", "disposition": {"status": "FINISHED"}}}})
    await tm.cancel_task_tree("task", AGENT_CANCEL_REASON + "late interruption")
    assert task.status == "FINISHED"
    assert not bus.events


def test_interruption_cause_survives_new_agent_execution_in_task_projection():
    from ctx_weft.core.control.reducers import reduce_events, serialize_view, deserialize_view, prune_view_for_snapshot
    from ctx_weft.core.utils.event import new_event
    from ctx_weft.protocols.events import EventOrigin
    def event(kind, tid, payload):
        return new_event(kind, session_id="s1", tenant_id="default", origin=EventOrigin.RUNTIME,
                         task_id=tid, agent_id="agent", payload=payload)
    events = [event("AgentInstantiated", "old", {"template_id": "tpl"}),
        event("TaskCreated", "old", {"task": {"id": "old", "status": "SUSPENDED", "assigned_agent_id": "agent"}}),
        event("AgentTerminated", "old", {"reason": AGENT_CANCEL_REASON + "test"}),
        event("TaskCreated", "next", {"task": {"id": "next", "status": "ACTIVE", "assigned_agent_id": "agent"}}),
        event("AgentRunning", "next", {"reason": "task_started"}),
        event("TaskInterrupted", "old", {"reason": "crash_recovery", "error_message": "Process interrupted"})]
    view = deserialize_view(serialize_view(prune_view_for_snapshot(reduce_events(events, ""))))
    assert view.agents["agent"].current_task_id == "old"
    assert view.tasks["old"].error == AGENT_CANCEL_REASON + "test"


async def test_cancel_parked_child_settles_even_before_old_slot_is_released():
    tm, bus = _tm()
    parent = Task(id="parent", session_id="s1", status="SUSPENDED", assigned_agent_id="agent")
    child = Task(id="child", session_id="s1", status="AWAITING_HUMAN", parent_task_id="parent", assigned_agent_id="agent")
    tm.restore([parent, child], set(), enqueue=False)
    tm._running_tasks.add("child")  # TaskAwaitingHuman committed; old settle has not released the slot.
    finalized = []
    async def finalize(tasks, reason):
        finalized.extend(task.id for task in tasks)
    tm.set_hooks(TaskManagerHooks(tree_cancel_finalizer=finalize))
    await tm.cancel_task_tree("child", AGENT_CANCEL_REASON + "test")
    while tm._background_asyncio_tasks:
        await asyncio.gather(*list(tm._background_asyncio_tasks))
    await tm._settle("child", "AWAITING_HUMAN")
    assert parent.status == "PENDING"
    assert finalized == ["child"]
    assert sum(e.type == "TaskResumed" for e in bus.events) == 1
    assert sum(e.type == "TaskFinalized" for e in bus.events) == 1


async def test_agent_commit_and_task_cancel_share_verdict_arbitration():
    from tests.unit.test_out_of_band_verdict import _parked, _success
    tm, bus = _tm()
    task = _parked(tm)
    committing, release = asyncio.Event(), asyncio.Event()
    async def commit_agent():
        committing.set()
        await release.wait()
    cancel = asyncio.create_task(tm.cancel_task_tree(task.id, AGENT_CANCEL_REASON + "test",
                                                    before_cancel=commit_agent))
    await committing.wait()
    judge = asyncio.create_task(tm.apply_out_of_band_verdict(task.id, _success()))
    await asyncio.sleep(0)
    assert not judge.done()
    release.set()
    await cancel
    assert await judge is False
    assert task.status == "CANCELED"
    while tm._background_asyncio_tasks:
        await asyncio.gather(*list(tm._background_asyncio_tasks))


def test_unrequested_interrupt_api_is_absent():
    assert not hasattr(_rt(), "interrupt_agent")


async def test_repeated_cancel_of_cascaded_agent_does_not_select_unrelated_task():
    rt = _rt()
    _plant(rt, "agent", None, status="terminated")
    lm = rt._agent_lifecycle_manager
    lm._agents["agent"].current_task_id = "unrelated"
    lm._agents["agent"].status_reason = "parent canceled"
    tm = rt._task_managers["s1"]
    tm.register_task(Task(id="unrelated", session_id="s1", status="PENDING", assigned_agent_id="agent"))
    await rt.initialize()
    assert await rt.cancel_agent("agent") == []
    assert tm.get_task("unrelated").status == "PENDING"


async def test_active_cancel_commit_failure_keeps_nonterminal_state(monkeypatch):
    from ctx_weft.core.orchestrator.task.disposition import RunOutcome, RunOutcomeKind
    tm, bus = _tm()
    task = Task(id="task", session_id="s1", status="ACTIVE", error=AGENT_CANCEL_REASON + "test")
    tm.register_task(task)
    async def fail(event):
        raise OSError("cancel commit failed")
    monkeypatch.setattr(bus, "emit_batch", fail)
    with pytest.raises(OSError, match="cancel commit failed"):
        await tm.apply_run_outcome(task.id, RunOutcome(kind=RunOutcomeKind.CANCELED))
    assert task.status == "ACTIVE" and task.finished_at is None and not task.finalized


async def test_lost_cancel_receipt_reuses_committed_time_and_finalizes_once():
    from datetime import datetime, timezone
    from ctx_weft.core.control.types import TaskView
    from ctx_weft.core.orchestrator.task.disposition import RunOutcome, RunOutcomeKind
    tm, bus = _tm()
    task = Task(id="task", session_id="s1", status="ACTIVE", error=AGENT_CANCEL_REASON + "test")
    tm.register_task(task)
    committed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    persisted = TaskView(id="task", session_id="s1", status="CANCELED",
                         error=task.error, finished_at=committed_at)
    async def read(tid):
        return persisted
    calls = []
    async def finalize(tasks, reason):
        calls.append(tasks[0].id)
    tm.set_hooks(TaskManagerHooks(read_task_state=read, tree_cancel_finalizer=finalize))
    await tm.apply_run_outcome(task.id, RunOutcome(kind=RunOutcomeKind.CANCELED))
    assert task.status == "CANCELED" and task.finished_at == committed_at
    await tm._finalize_tree_cancellations([task], task.error)
    # A projection read started before finalization must not roll the flag back.
    async with tm._lock:
        await tm._commit_task_cancellation(task, {"reason": task.error})
    await tm._finalize_tree_cancellations([task], task.error)
    assert calls == [task.id] and task.finalized
    assert [e.type for e in bus.events] == ["TaskFinalized"]


async def test_committed_finalization_releases_slot_and_notifies_once_under_concurrent_retry():
    from ctx_weft.core.control.types import TaskView
    from ctx_weft.core.loop.driver import ExecutionRegistry

    tm, bus = _tm()
    reason = AGENT_CANCEL_REASON + "test"
    task = Task(id="task", session_id="s1", status="CANCELED", error=reason, assigned_agent_id="worker")
    tm.register_task(task)
    tm._running_tasks.add(task.id)
    tm._running_agents[task.id] = "worker"
    registry = ExecutionRegistry()
    notifications = []

    async def read(tid):
        return TaskView(id=tid, session_id="s1", status="CANCELED", error=reason, finalized=True)

    async def finalize(tasks, cause):
        raise AssertionError("already committed report must not be regenerated")

    async def terminal(tid):
        notifications.append(("task", tid))

    async def done():
        notifications.append(("session", "s1"))

    tm.set_hooks(TaskManagerHooks(read_task_state=read, tree_cancel_finalizer=finalize,
        cancel_guard=lambda tid, step="finish_report": registry.task_guard(tid, step),
        on_task_terminal=terminal, on_session_done=done))
    for _ in range(2):
        await asyncio.gather(*(tm.cancel_task_tree(task.id, reason) for _ in range(3)))
        while tm._background_asyncio_tasks:
            await asyncio.gather(*list(tm._background_asyncio_tasks))
        if tm._session_done_waiter:
            await tm._session_done_waiter
    assert not tm.running_task_ids() and not tm._running_agents and tm.is_done()
    assert notifications == [("task", task.id), ("session", "s1")]
    assert not bus.events
