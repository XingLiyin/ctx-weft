"""Task waits reuse committed prefixes, without treating coroutine exit as Applied."""
# ruff: noqa: F811 -- shared parametrized pytest fixture.
import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from ctx_weft.core.control.reducers import reduce_events
from ctx_weft.core.control.task_settlement import (
    advance_task_settlement,
    load_task_settlement_baseline,
)
from ctx_weft.core.control.types import ObserveWaitBaseline, TaskSettlementWait
from ctx_weft.core.runtime import TurnHandle
from ctx_weft.protocols.events import EventType
from tests._snapshot_helpers import seed_snapshot
from tests.unit.test_read_primitives_conformance import store  # noqa: F401
from tests.unit.test_runtime_initialization_queries import container, runtime
from tests.unit.test_scoped_recovery_reads import ev, seed


async def pending(store, sid, tid, *, through_position, progress, baseline=None):
    if progress.progress is None:
        progress.progress = await load_task_settlement_baseline(
            store, sid, tid, through_position=through_position, baseline=baseline)
    else:
        progress.progress = await advance_task_settlement(
            store, progress.progress, through_position=through_position)
    return progress.progress.pending_observe


@pytest.mark.parametrize("version", [None, 11, 999])
async def test_wait_prefix_reads_old_metadata_once_and_tracks_all_observes(store, monkeypatch, version):
    events = [ev("created", EventType.TASK_CREATED, task={"id": "target"}),
              ev("first", EventType.OBSERVE_REQUESTED, owner="parent", task_id="target", observe_id="one"),
              ev("second", EventType.OBSERVE_REQUESTED, task_id="target", observe_id="two"),
              ev("finished", EventType.TASK_FINISHED),
              ev("foreign", EventType.OBSERVE_REQUESTED, owner="other", task_id="other",
                 observe_id="foreign", report="forbidden-body")]
    await seed(store, *events)
    if version is not None:
        await seed_snapshot(store, "session", reduce_events(events, "session"), cut=5,
                            overrides={"projection_version": version})
    reads, snapshots = [], []
    original, last = store.read_range, store.read_last_of_type

    async def read(sid, **kwargs):
        assert kwargs["related_task_id"] == "target" and kwargs["projection"]
        rows = await original(sid, **kwargs)
        assert "forbidden-body" not in repr(rows)
        reads.append((kwargs["after_position"], kwargs["through_position"], [r.event.id for r in rows]))
        return rows

    async def read_last(*args, **kwargs):
        result = await last(*args, **kwargs)
        assert "forbidden-body" not in repr(result)
        snapshots.append(result)
        return result

    monkeypatch.setattr(store, "read_range", read)
    monkeypatch.setattr(store, "read_last_of_type", read_last)
    progress = TaskSettlementWait()
    head = await store.committed_head("session")
    assert set(await pending(store, "session", "target",
        through_position=head, progress=progress)) == {"one", "two"}
    assert progress.progress.task_status == "FINISHED"
    first_reads = len(reads)
    assert set(await pending(store, "session", "target",
        through_position=head, progress=progress)) == {"one", "two"}
    assert len(reads) == first_reads and len(snapshots) == 1
    assert reads[0][0] == (5 if version == 11 else 0)

    for event, expected in [
        (ev("completed", EventType.OBSERVE_COMPLETED, task_id="target", observe_id="one",
            report="forbidden-body"), {"one", "two"}),
        (ev("two-applied", EventType.OBSERVE_APPLIED, task_id="target", observe_id="two"), {"one"}),
        (ev("third", EventType.HITL_OPENED, owner="parent",
            observe_request={"task_id": "target", "observe_id": "three"}, prompt="forbidden-body"),
         {"one", "three"}),
        (ev("one-applied", EventType.OBSERVE_APPLIED, task_id="target", observe_id="one"), {"three"}),
        (ev("three-applied", EventType.OBSERVE_APPLIED, task_id="target", observe_id="three"), set()),
    ]:
        previous = progress.progress.position
        await seed(store, event)
        head = await store.committed_head("session")
        assert set(await pending(store, "session", "target",
            through_position=head, progress=progress)) == expected
        assert reads[-1] == (previous, head, [event.id])
    assert len(snapshots) == 1


async def test_legacy_started_only_snapshot_keeps_task_key_until_done(store):
    events = [ev("created", EventType.TASK_CREATED, task={"id": "target"}),
              ev("started", "TaskRecapStarted", task_id="target"),
              ev("finished", EventType.TASK_FINISHED)]
    await seed(store, *events)
    await seed_snapshot(store, "session", reduce_events(events, "session"), cut=3,
                        overrides={"projection_version": 11})
    progress = TaskSettlementWait()
    assert set(await pending(store, "session", "target",
        through_position=4, progress=progress)) == {"target"}
    await seed(store, ev("done", "TaskRecapDone", task_id="target"))
    assert not await pending(store, "session", "target",
        through_position=5, progress=progress)


async def test_snapshot_and_owner_baseline_after_requested_cut_are_not_used(store):
    events = [ev("created", EventType.TASK_CREATED, task={"id": "target"}),
              ev("request", EventType.OBSERVE_REQUESTED, task_id="target", observe_id="obs"),
              ev("finished", EventType.TASK_FINISHED)]
    await seed(store, *events)
    applied = ev("applied", EventType.OBSERVE_APPLIED, task_id="target", observe_id="obs")
    await seed(store, applied)
    await seed_snapshot(store, "session", reduce_events([*events, applied], "session"), cut=4)
    progress = TaskSettlementWait()
    assert set(await pending(store, "session", "target",
        through_position=3, progress=progress, baseline=ObserveWaitBaseline(5, {}))) == {"obs"}
    assert progress.progress.position == 3
    assert not await pending(store, "session", "target",
        through_position=5, progress=progress)


@pytest.mark.parametrize("old_cut", [0, 2])
async def test_each_wait_prefers_new_snapshot_over_old_memory_baseline(store, monkeypatch, old_cut):
    events = [ev("created", EventType.TASK_CREATED, task={"id": "target"}),
              ev("request", EventType.OBSERVE_REQUESTED, task_id="target", observe_id="obs"),
              ev("applied", EventType.OBSERVE_APPLIED, task_id="target", observe_id="obs"),
              ev("finished", EventType.TASK_FINISHED)]
    await seed(store, *events)
    await seed_snapshot(store, "session", reduce_events(events, "session"), cut=4)
    core = runtime(store)
    core._observe_wait_baselines["session"] = ObserveWaitBaseline(old_cut,
        {"obs": {"task_id": "target", "observe_id": "obs"}} if old_cut else {})
    original, reads = store.read_range, []

    async def read(sid, **kwargs):
        assert kwargs["after_position"] >= 4, "new wait replayed history before the newer snapshot"
        rows = await original(sid, **kwargs)
        reads.append([row.event.id for row in rows])
        return rows

    monkeypatch.setattr(store, "read_range", read)
    handle = TurnHandle(session_id="session", task_id="target", agent_id="agent", template_id="tpl",
                        event_bus=core.event_bus, _is_finished=core._task_is_settled)
    # Each invocation owns a fresh cursor; the runtime baseline remains old.
    await handle.wait_for_finish(timeout=2)
    await handle.wait_for_finish(timeout=2)
    assert reads == [[], []]
    assert core._observe_wait_baselines["session"].position == old_cut


async def test_newer_memory_baseline_is_not_rewound_to_old_snapshot(store):
    events = [ev("created", EventType.TASK_CREATED, task={"id": "target"}),
              ev("request", EventType.OBSERVE_REQUESTED, task_id="target", observe_id="obs")]
    await seed(store, *events)
    await seed_snapshot(store, "session", reduce_events(events, "session"), cut=2)
    await seed(store, ev("applied", EventType.OBSERVE_APPLIED, task_id="target", observe_id="obs"),
               ev("finished", EventType.TASK_FINISHED), ev("foreign", "Test", owner="foreign"))
    state = await load_task_settlement_baseline(store, "session", "target", through_position=6,
        baseline=ObserveWaitBaseline(5, {}), loaded_task_status="FINISHED")
    assert state.position == 6 and state.task_status == "FINISHED" and not state.pending_observe


async def test_malformed_new_snapshot_keeps_earlier_baseline_and_replays_suffix(store):
    events = [ev("created", EventType.TASK_CREATED, task={"id": "target"}),
              ev("request", EventType.OBSERVE_REQUESTED, task_id="target", observe_id="obs"),
              ev("finished", EventType.TASK_FINISHED)]
    await seed(store, *events)
    await seed_snapshot(store, "session", reduce_events(events, "session"), cut=3,
                        overrides={"state_blob": {"tasks": {"target": "invalid"}}})
    state = await load_task_settlement_baseline(store, "session", "target", through_position=4,
        baseline=ObserveWaitBaseline(2, {"obs": {"task_id": "target", "observe_id": "obs"}}))
    assert state.position == 4 and set(state.pending_observe) == {"obs"}
    assert state.task_status == "FINISHED"


@pytest.mark.parametrize("fault", ["error", "cancel"])
async def test_failed_delta_does_not_publish_partial_prefix(store, monkeypatch, fault):
    await seed(store, ev("one", EventType.OBSERVE_REQUESTED, task_id="target", observe_id="one"))
    progress = TaskSettlementWait()
    await pending(store, "session", "target", through_position=1, progress=progress)
    await seed(store, ev("applied", EventType.OBSERVE_APPLIED, task_id="target", observe_id="one"),
               ev("two", EventType.OBSERVE_REQUESTED, task_id="target", observe_id="two"))
    original, entered = store.read_range, asyncio.Event()

    async def interrupted(*args, **kwargs):
        await original(*args, **kwargs)
        if fault == "error":
            raise OSError("lost read")
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(store, "read_range", interrupted)
    if fault == "error":
        with pytest.raises(OSError, match="lost read"):
            await pending(store, "session", "target", through_position=3, progress=progress)
    else:
        waiting = asyncio.create_task(pending(
            store, "session", "target", through_position=3, progress=progress))
        await asyncio.wait_for(entered.wait(), 2)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    assert progress.progress.position == 1 and set(progress.progress.pending_observe) == {"one"}
    monkeypatch.setattr(store, "read_range", original)
    assert set(await pending(store, "session", "target",
        through_position=3, progress=progress)) == {"two"}
    assert progress.progress.position == 3


async def test_loaded_owner_reuses_complete_recovery_baseline(store, monkeypatch):
    core = runtime(store)
    handle = await container(core, "session")
    events = [replace(event, agent_id=handle.root_agent_id) for event in [
        ev("created", EventType.TASK_CREATED,
           task={"id": "target", "assigned_agent_id": handle.root_agent_id}),
        ev("finished", EventType.TASK_FINISHED),
        ev("request", EventType.OBSERVE_REQUESTED, task_id="target", observe_id="obs")]]
    await seed(store, *events)
    assert core.forget_session("session")
    await core.rebuild_session("session", strict=True)
    baseline = core._observe_wait_baselines["session"]
    # A launched/failed recovery is no longer deferred, but remains durable work.
    core._prepared_recoveries.pop("session")
    original, reads = store.read_range, []
    monkeypatch.setattr(store, "read_last_of_type", AsyncMock(side_effect=AssertionError("reread snapshot")))

    async def read(sid, **kwargs):
        assert kwargs["related_task_id"] == "target"
        assert kwargs["after_position"] >= baseline.position
        reads.append(kwargs)
        return await original(sid, **kwargs)

    monkeypatch.setattr(store, "read_range", read)
    progress = TaskSettlementWait()
    assert not await core._task_is_settled("session", "target", wait=progress)
    assert not await core._task_is_settled("session", "target", wait=progress)
    assert not reads
    await seed(store, ev("applied", EventType.OBSERVE_APPLIED, task_id="target", observe_id="obs"))
    assert await core._task_is_settled("session", "target", wait=progress)
    assert len(reads) == 1 and "obs" in baseline.pending_observe
    assert core.forget_session("session")
    assert "session" not in core._observe_wait_baselines


async def test_cold_pruned_task_status_is_read_once_without_bodies_and_keeps_advancing(store, monkeypatch):
    events = [ev("created", EventType.TASK_CREATED, task={"id": "target", "user_prompt": "forbidden-body"}),
              ev("finished", EventType.TASK_FINISHED, outputs="forbidden-body"),
              ev("request", EventType.OBSERVE_REQUESTED, task_id="target", observe_id="obs")]
    await seed(store, *events)
    view = reduce_events(events, "session")
    view.tasks.clear()  # An old/pruned snapshot must not invent the task's status.
    await seed_snapshot(store, "session", view, cut=3)
    core, progress = runtime(store), TaskSettlementWait()
    original, reads = store.read_range, []

    async def read(sid, **kwargs):
        reads.append(kwargs)
        rows = await original(sid, **kwargs)
        assert "forbidden-body" not in repr(rows)
        return rows

    monkeypatch.setattr(store, "read_range", read)
    assert not await core._task_is_settled("session", "target", wait=progress)
    count = len(reads)
    assert progress.progress.task_status == "FINISHED"
    assert not await core._task_is_settled("session", "target", wait=progress)
    assert len(reads) == count
    await seed(store, ev("requeued", EventType.TASK_REQUEUED),
               ev("applied", EventType.OBSERVE_APPLIED, task_id="target", observe_id="obs"))
    assert not await core._task_is_settled("session", "target", wait=progress)
    assert progress.progress.task_status == "PENDING"
    await seed(store, ev("finished-again", EventType.TASK_FINISHED))
    assert await core._task_is_settled("session", "target", wait=progress)
    assert all(query["after_position"] >= 4 for query in reads[count:])
    assert not core._task_managers


async def test_concurrent_waits_keep_independent_prefixes_and_unknown_receipts_gate_finish(store):
    await seed(store, ev("created", EventType.TASK_CREATED, task={"id": "target"}),
               ev("finished", EventType.TASK_FINISHED),
               ev("request", EventType.OBSERVE_REQUESTED, task_id="target", observe_id="obs"))
    core = runtime(store)
    checked, contexts, unavailable = asyncio.Event(), [], False

    async def is_finished(sid, tid, *, wait):
        nonlocal unavailable
        contexts.append(wait)
        result = await core._task_is_settled(sid, tid, wait=wait)
        if len({id(item) for item in contexts}) == 2:
            checked.set()
        if result and not recovery.await_count:
            unavailable = True  # Receipt becomes unknown during the awaited query.
        return result

    async def recover(sid):
        nonlocal unavailable
        assert sid == "session"
        unavailable = False

    recovery = AsyncMock(side_effect=recover)
    handle = TurnHandle(session_id="session", task_id="target", agent_id="agent", template_id="tpl",
        event_bus=core.event_bus, _is_finished=is_finished,
        _storage_health=lambda _: "unknown receipt" if unavailable else None,
        _storage_recovery=recovery)
    before = set(core.event_bus._subscribers)
    first = asyncio.create_task(handle.wait_for_finish(timeout=3))
    second = asyncio.create_task(handle.wait_for_finish(timeout=3))
    try:
        await asyncio.wait_for(checked.wait(), 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not second.done()
        await seed(store, ev("applied", EventType.OBSERVE_APPLIED, task_id="target", observe_id="obs"))
        # No event notification: polling must discover the committed suffix.
        await asyncio.wait_for(second, 2)
    finally:
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
    assert set(core.event_bus._subscribers) == before
    assert len({id(item) for item in contexts}) == 2
    recovery.assert_awaited_once_with("session")
    assert not core._task_managers
