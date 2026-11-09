"""Pause replacement is one durable batch and arbitrates concurrent replies."""

import asyncio

import pytest

from ctx_weft.core.control.reducers import fold_hitl_snapshot
from ctx_weft.core.events.commit_gate import CommitGate
from ctx_weft.protocols.events import EventType, PersistenceUnavailableError
from ctx_weft.protocols.hitl import HitlAsk, HitlReply, UserTurnDelivery
from ctx_weft.providers.events import InMemoryEventStore, InProcessEventBus
from tests.unit.test_hitl_service import _service


async def _open(svc, *, replacement="", preface="normal"):
    return await svc.open(HitlAsk(form="wait", delivery=UserTurnDelivery(
        task_id="t1", preface=preface)), session_id="s1", task_id="t1", stage="tool",
        unattended=False, replace_user_turn_id=replacement)


@pytest.mark.parametrize("receipt_lost", [False, True])
async def test_failed_replacement_retries_exact_batch_before_late_reply(monkeypatch, receipt_lost):
    store, bus = InMemoryEventStore(), InProcessEventBus()
    bus.attach_commit_gate(CommitGate(store))
    svc = _service(bus)
    old = await _open(svc)
    append = store.append_batch
    attempts = []

    async def fail_once(sid, bid, events):
        attempts.append((bid, tuple(events)))
        if len(attempts) == 1:
            if receipt_lost:
                await append(sid, bid, events)
            raise OSError("replacement receipt unavailable")
        return await append(sid, bid, events)

    monkeypatch.setattr(store, "append_batch", fail_once)
    with pytest.raises(PersistenceUnavailableError):
        await _open(svc, replacement=old.id, preface="after_interrupt_edit")
    assert svc.registry.list_pending("s1") == [old]
    durable = fold_hitl_snapshot([r.event for r in await store.read_range("s1")])
    assert (old.id in durable.pending) is not receipt_lost
    # Replacement reserved first; a late reply confirms its original batch,
    # rather than reviving the old wait or creating a second transaction.
    assert await svc.resolve(HitlReply(hitl_id=old.id, agent_id="", outcome="accepted",
                                      message="late reply"), defer=True) is None
    assert attempts[0] == attempts[1]
    pending, = svc.registry.list_pending("s1")
    assert pending.id != old.id and pending.delivery.preface == "after_interrupt_edit"
    assert old.decision.outcome == "cancelled"
    events = [r.event for r in await store.read_range("s1")]
    assert [e.type for e in events] == [EventType.HITL_OPENED, EventType.HITL_RESOLVED,
                                      EventType.HITL_OPENED]
    assert list(fold_hitl_snapshot(events).pending) == [pending.id]
    assert not svc._pending_opens and not svc._replacing_user_turns


async def test_reply_during_replacement_confirmation_waits_without_duplicate_commit(monkeypatch):
    store, bus = InMemoryEventStore(), InProcessEventBus()
    bus.attach_commit_gate(CommitGate(store))
    svc = _service(bus)
    old = await _open(svc)
    entered, release = asyncio.Event(), asyncio.Event()
    append = store.append_batch
    calls = []

    async def hold(sid, bid, events):
        calls.append(bid)
        entered.set()
        await release.wait()
        return await append(sid, bid, events)

    monkeypatch.setattr(store, "append_batch", hold)
    opening = asyncio.create_task(_open(svc, replacement=old.id))
    reply = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        reply = asyncio.create_task(svc.resolve(HitlReply(
            hitl_id=old.id, agent_id="", outcome="accepted", message="late"), defer=True))
        await asyncio.sleep(0)
        assert not reply.done() and svc.registry.list_pending("s1") == [old]
        release.set()
        new, answer = await asyncio.wait_for(asyncio.gather(opening, reply), 1)
        assert answer is None and len(calls) == 1
        assert svc.registry.list_pending("s1") == [new]
    finally:
        release.set()
        jobs = [j for j in (opening, reply) if j is not None]
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)


async def test_reply_before_replacement_is_preserved():
    svc = _service(InProcessEventBus())
    old = await _open(svc)
    await svc.resolve(HitlReply(hitl_id=old.id, agent_id="", outcome="accepted",
                                message="user's input"), defer=True)
    new = await _open(svc, replacement=old.id)
    assert old.pending_decision.message == "user's input"
    assert svc.registry.list_pending("s1") == [new]


async def test_recovery_from_replacement_notification_does_not_wait_on_itself():
    bus = InProcessEventBus()
    svc = _service(bus)
    old = await _open(svc)

    async def recover(event):
        await svc.confirm_opens("s1")

    bus.subscribe(None, recover, required=True, provisional=True)
    new = await asyncio.wait_for(_open(svc, replacement=old.id), 1)
    assert svc.registry.list_pending("s1") == [new]
