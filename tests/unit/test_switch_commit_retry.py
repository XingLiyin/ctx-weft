"""Retry the original switch commit without holding arbitration during notification."""

import asyncio

import pytest

from ctx_weft.core.capabilities.control_tools import switch_task
from ctx_weft.core.events.commit_gate import CommitGate
from ctx_weft.core.models.task import Task
from ctx_weft.core.orchestrator.task.disposition import RunOutcome, RunOutcomeKind
from ctx_weft.protocols.events import EventType, PersistenceUnavailableError
from ctx_weft.providers.events import InMemoryEventStore, InProcessEventBus
from tests.unit.test_switch_task import setup_switch


def _owner():
    tm, old, ctx, _ = setup_switch()
    store, bus = InMemoryEventStore(), InProcessEventBus()
    bus.attach_commit_gate(CommitGate(store))
    tm._event_bus = bus
    switch_task("New goal", "Complete new work", "New request", ctx=ctx)
    successor = tm._staged[old.id][0][0]
    return tm, old, successor, store, bus


@pytest.mark.parametrize("receipt_lost", [False, True])
async def test_switch_receipt_retry_publishes_once_and_releases_before_notification(monkeypatch, receipt_lost):
    tm, old, successor, store, bus = _owner()
    append = store.append_batch
    attempts = []

    async def fail_once(sid, bid, events):
        attempts.append((bid, tuple(events)))
        if len(attempts) == 1:
            assert tm._lock.locked() and old.status == "ACTIVE"
            assert tm.get_task(successor.id) is None
            if receipt_lost:
                await append(sid, bid, events)
            raise OSError("switch receipt unavailable")
        return await append(sid, bid, events)

    monkeypatch.setattr(store, "append_batch", fail_once)
    outcome = RunOutcome(RunOutcomeKind.TASK_SWITCHED)
    with pytest.raises(PersistenceUnavailableError):
        await tm.apply_run_outcome(old.id, outcome)
    assert not tm._lock.locked() and old.status == "ACTIVE"
    assert tm.get_task(successor.id) is None
    notified = []

    async def notify(event):
        assert not tm._lock.locked()
        assert old.status == "REVIEWING" and tm.get_task(successor.id) is successor
        notified.append(event.id)

    bus.subscribe(EventType.TASK_REVIEWING, notify, required=True, provisional=True)
    assert await asyncio.wait_for(tm.apply_run_outcome(old.id, outcome), 1) == "REVIEWING"
    assert attempts[0] == attempts[1] and len(notified) == 1
    assert len(await store.read_range("s1")) == 1 and not tm._lock.locked()


async def test_switch_notification_retry_finishes_original_delivery_without_recommitting():
    tm, old, successor, store, bus = _owner()
    notified = []

    async def notify(event):
        assert not tm._lock.locked()
        notified.append(event.id)
        if len(notified) == 1:
            raise RuntimeError("required consumer unavailable")

    bus.subscribe(EventType.TASK_REVIEWING, notify, required=True, provisional=True)
    outcome = RunOutcome(RunOutcomeKind.TASK_SWITCHED)
    with pytest.raises(RuntimeError, match="required consumer unavailable"):
        await tm.apply_run_outcome(old.id, outcome)
    assert old.status == "REVIEWING" and not tm._lock.locked()
    assert await asyncio.wait_for(tm.apply_run_outcome(old.id, outcome), 1) == "REVIEWING"
    assert len(notified) == 2 and notified[0] == notified[1]
    assert tm.get_task(successor.id) is successor
    assert len(await store.read_range("s1")) == 1


@pytest.mark.parametrize("receipt_lost", [False, True])
async def test_stopped_switch_recovery_releases_observer_and_enqueues_original_successor(monkeypatch, receipt_lost):
    tm, old, successor, store, bus = _owner()
    old.switch_review_started = True
    tm._running_tasks.add(old.id)
    append = store.append_batch
    calls, notified = [], []

    async def fail_once(sid, bid, events):
        calls.append(bid)
        if len(calls) == 1:
            if receipt_lost:
                await append(sid, bid, events)
            raise OSError("switch receipt unavailable")
        return await append(sid, bid, events)

    async def notify(event):
        notified.append(event.id)

    monkeypatch.setattr(store, "append_batch", fail_once)
    bus.subscribe(EventType.TASK_REVIEWING, notify, required=True, provisional=True)
    with pytest.raises(PersistenceUnavailableError):
        await tm.apply_run_outcome(old.id, RunOutcome(RunOutcomeKind.TASK_SWITCHED))
    assert not old.switch_review_ready.is_set()
    # Runtime confirms pending receipts/delivery before reconciling stopped owners.
    await bus.confirm_pending_storage("s1")
    await asyncio.wait_for(tm.reconcile_stopped_tasks(), 1)
    assert old.status == "REVIEWING" and old.switch_review_ready.is_set()
    assert tm.get_task(successor.id) is successor and old.id not in tm.running_task_ids()
    assert [entry.task_id for entry in tm._queue.peek_all()] == [successor.id]
    assert not tm._pending_switches and not tm._staged and not tm._lock.locked()
    assert len(notified) == 1


async def test_canceling_replaced_caller_preserves_committed_successor_for_another_agent():
    from ctx_weft.core.orchestrator.lifecycle.agent_state import AGENT_CANCEL_REASON

    tm, old, ctx, _ = setup_switch()
    ancestor = Task(id="ancestor", session_id="s1", status="SUSPENDED", assigned_agent_id="another-agent")
    tm.register_task(ancestor)
    old.parent_task_id = ancestor.id
    tm._parent_map[old.id] = ancestor.id
    tm._children_of[ancestor.id] = {old.id}
    switch_task("New goal", "Complete new work", "New request", target_task_id=ancestor.id,
                reason="The user replaced the ancestor goal", ctx=ctx)
    successor = tm._staged[old.id][0][0]
    assert successor.assigned_agent_id == ancestor.assigned_agent_id
    assert await tm.apply_run_outcome(old.id, RunOutcome(RunOutcomeKind.TASK_SWITCHED)) == "REVIEWING"
    await tm.cancel_task_tree(old.id, AGENT_CANCEL_REASON + "canceled")
    await tm._flush_staged(old.id)
    while tm._background_asyncio_tasks:
        await asyncio.gather(*list(tm._background_asyncio_tasks))
    assert old.status == "CANCELED" and successor.status == "PENDING"
    assert [entry.task_id for entry in tm._queue.peek_all()] == [successor.id]


async def test_session_cancel_before_agent_marker_rejects_unsubmitted_switch():
    tm, old, successor, store, _ = _owner()
    # cancel_session closes dispatch before awaiting metadata and agent closure.
    # The exiting actor can reach arbitration during those awaits.
    tm.begin_cancel()
    assert await tm.apply_run_outcome(old.id, RunOutcome(RunOutcomeKind.TASK_SWITCHED)) == "CANCELED"
    assert tm.get_task(successor.id) is None and not tm._staged and not tm._pending_switches
    rows = await store.read_range("s1")
    event, = [r.event for r in rows]
    assert event.type == EventType.TASK_CANCELED and event.payload["reason"] == "user_cancel"
