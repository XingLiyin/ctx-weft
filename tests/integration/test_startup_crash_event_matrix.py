"""Restart at durable event boundaries, with and without recovery snapshots."""

import pytest

from ctx_weft.core.control.reducers import rebuild_view, prune_view_for_snapshot
from ctx_weft.core.utils.event import new_event
from ctx_weft.protocols.events import EventType
from ctx_weft.providers.events import InMemoryEventStore
from tests._snapshot_helpers import seed_snapshot
from tests.unit.test_runtime_initialization_queries import append, container, runtime

pytestmark = pytest.mark.asyncio


@pytest.fixture(params=["memory", "sqlite"])
async def store(request, tmp_path):
    if request.param == "memory":
        yield InMemoryEventStore()
    else:
        from ctx_weft.providers.events.store.sql import open_sqlite_event_store
        async with open_sqlite_event_store(tmp_path / "events.db") as event_store:
            yield event_store


async def snapshot_if_requested(store, sid, snapshot):
    if snapshot:
        view = prune_view_for_snapshot(await rebuild_view(store, sid))
        await seed_snapshot(store, sid, view, cut=await store.committed_head(sid))


@pytest.mark.parametrize("snapshot", [False, True], ids=["log", "snapshot"])
@pytest.mark.parametrize("status,event", [
    ("PENDING", None),
    ("ACTIVE", EventType.TASK_STARTED),
    ("REVIEWING", EventType.TASK_REVIEWING),
    ("AWAITING_HUMAN", EventType.TASK_AWAITING_HUMAN),
    ("SUSPENDED", EventType.TASK_SUSPENDED),
    ("INTERRUPTED", EventType.TASK_INTERRUPTED),
    ("FINISHED", EventType.TASK_FINISHED),
    ("FAILED", EventType.TASK_FAILED),
    ("CANCELED", EventType.TASK_CANCELED),
])
async def test_restart_preserves_durable_task_state(store, snapshot, status, event):
    old = runtime(store)
    h = await container(old)
    await append(store, h.session_id,
        (EventType.TASK_CREATED, "t", h.root_agent_id,
         {"task": {"id": "t", "assigned_agent_id": h.root_agent_id, "title": "work"}}))
    if event is not None:
        await append(store, h.session_id,
                     (event, "t", h.root_agent_id, {"assigned_agent_id": h.root_agent_id}))
    await snapshot_if_requested(store, h.session_id, snapshot)
    restarted = runtime(store)
    await restarted.initialize()
    rows = await store.read_range(h.session_id)
    assert not any(row.event.type == EventType.SESSION_STATUS_CHANGED for row in rows)
    expected = "INTERRUPTED" if status == "ACTIVE" else status
    assert (await restarted.get_task(h.session_id, "t")).data.status == expected
    assert not restarted._task_managers
    assert restarted._agent_lifecycle_manager.all_agent_ids() == []
    head = await store.committed_head(h.session_id)
    await runtime(store).initialize()
    assert await store.committed_head(h.session_id) == head
    await restarted.rebuild_session(h.session_id, strict=True)
    assert (await restarted.get_task(h.session_id, "t")).data.status == expected
    assert await store.committed_head(h.session_id) == head


@pytest.mark.parametrize("snapshot", [False, True], ids=["log", "snapshot"])
@pytest.mark.parametrize("cut", ["run_started", "run_finished"])
async def test_restart_closes_unfinished_foreground_run(store, snapshot, cut):
    old = runtime(store)
    h = await container(old)
    await append(store, h.session_id,
        (EventType.TASK_CREATED, "t", h.root_agent_id,
         {"task": {"id": "t", "assigned_agent_id": h.root_agent_id}}),
        (EventType.TASK_STARTED, "t", h.root_agent_id, {"assigned_agent_id": h.root_agent_id}),
        (EventType.AGENT_RUNNING, "t", h.root_agent_id, {}))
    start = new_event(EventType.RUN_STARTED, session_id=h.session_id,
                      tenant_id="default", origin="test", run_id="lost-run",
                      task_id="t", agent_id=h.root_agent_id, sequence=1)
    await store.append_batch(h.session_id, start.id, [start])
    if cut == "run_finished":
        end = new_event(EventType.RUN_FINISHED, session_id=h.session_id,
                        tenant_id="default", origin="test", run_id="lost-run",
                        task_id="t", agent_id=h.root_agent_id, sequence=2)
        await store.append_batch(h.session_id, end.id, [end])
        await append(store, h.session_id, (EventType.TASK_FINISHED, "t", h.root_agent_id, {}))
    await snapshot_if_requested(store, h.session_id, snapshot)
    await runtime(store).initialize()
    rows = await store.read_range(h.session_id, include_types=(EventType.RUN_FINISHED,))
    assert [row.event.run_id for row in rows] == ["lost-run"], (
        f"restart left execution open after {cut}")
    head = await store.committed_head(h.session_id)
    await runtime(store).initialize()
    assert await store.committed_head(h.session_id) == head


@pytest.mark.parametrize("scenario", ["cross_cut", "no_delta", "finished_after_cut", "started_after_cut",
                                      "finished_before_cut", "terminal_task_background"])
async def test_run_recovery_reads_only_snapshot_delta(store, monkeypatch, scenario):
    old = runtime(store)
    h = await container(old)
    sid, aid = h.session_id, h.root_agent_id
    await append(store, sid, (EventType.TASK_CREATED, "t", aid,
                             {"task": {"id": "t", "assigned_agent_id": aid}}))

    async def event(kind, sequence):
        ev = new_event(kind, session_id=sid, tenant_id="default", origin="test",
                       run_id="cross-snapshot-run", task_id="t", agent_id=aid,
                       sequence=sequence, payload={"initial_step": "background_observe"})
        await store.append_batch(sid, ev.id, [ev])

    if scenario != "started_after_cut":
        await event(EventType.RUN_STARTED, 2)
        await event(EventType.STEP_STARTED, 7)
    if scenario == "finished_before_cut":
        await event(EventType.RUN_FINISHED, 9)
    if scenario == "terminal_task_background":
        await append(store, sid, (EventType.TASK_FINISHED, "t", aid, {"outputs": "done"}))
    cut = await store.committed_head(sid)
    snapshot_view = prune_view_for_snapshot(await rebuild_view(store, sid))
    if scenario == "terminal_task_background":
        assert "t" not in snapshot_view.tasks
        assert snapshot_view.open_runs["cross-snapshot-run"]["task_id"] == "t"
    await seed_snapshot(store, sid, snapshot_view, cut=cut)
    if scenario == "started_after_cut":
        await event(EventType.RUN_STARTED, 2)
    if scenario in {"cross_cut", "started_after_cut", "terminal_task_background"}:
        await event(EventType.LLM_RESPONSE_FINISHED, 12)
        # A lower sequence committed later must not lower the recovery sequence.
        await event(EventType.STEP_COMPLETED, 10)
    elif scenario == "finished_after_cut":
        await event(EventType.RUN_FINISHED, 12)

    head = await store.committed_head(sid)
    reads = []
    original_read = store.read_range

    async def delta_only(session_id, **kwargs):
        reads.append(kwargs)
        if kwargs.get("related_task_id"):
            assert scenario == "terminal_task_background"
            assert kwargs["related_task_id"] == "t"
            assert kwargs["after_position"] == 0
            assert kwargs["through_position"] == head + 2
            return await original_read(session_id, **kwargs)
        assert kwargs.get("after_position") == cut, kwargs
        assert kwargs.get("through_position") == head, kwargs
        return await original_read(session_id, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(store, "read_range", delta_only)
        await runtime(store).initialize()
    assert len(reads) == (2 if scenario == "terminal_task_background" else 1)
    rows = await original_read(sid, after_position=head)
    run_events = [r.event for r in rows if r.event.run_id == "cross-snapshot-run"]
    if scenario in {"finished_before_cut", "finished_after_cut"}:
        assert not run_events
    else:
        assert [ev.type for ev in run_events] == [EventType.RUN_INTERRUPTED, EventType.RUN_FINISHED]
        assert [ev.sequence for ev in run_events] == ([8, 9] if scenario == "no_delta" else [13, 14])
        assert all(ev.task_id == "t" and ev.agent_id == aid for ev in run_events)
    assert not (await rebuild_view(store, sid)).open_runs
    settled_head = await store.committed_head(sid)
    await runtime(store).initialize()
    assert await store.committed_head(sid) == settled_head


async def test_old_snapshot_without_run_state_falls_back_to_log(store):
    old = runtime(store)
    h = await container(old)
    ev = new_event(EventType.RUN_STARTED, session_id=h.session_id, tenant_id="default",
                   origin="test", run_id="old-open-run", task_id="pruned-task", sequence=5)
    await store.append_batch(h.session_id, ev.id, [ev])
    view = await rebuild_view(store, h.session_id)
    from ctx_weft.core.control.reducers import serialize_view
    blob = serialize_view(view)
    blob.pop("open_runs")
    await seed_snapshot(store, h.session_id, view,
                        cut=await store.committed_head(h.session_id),
                        overrides={"projection_version": 6, "state_blob": blob})
    await runtime(store).initialize()
    rows = await store.read_range(h.session_id, include_types=(EventType.RUN_FINISHED,))
    assert [(r.event.run_id, r.event.sequence) for r in rows] == [("old-open-run", 7)]


@pytest.mark.parametrize("snapshot", [False, True], ids=["log", "snapshot"])
@pytest.mark.parametrize("delivery_kind", ["tool", "user", "notification"])
@pytest.mark.parametrize("cut", ["opened", "deferred", "retracted", "resolved", "closed"])
async def test_restart_at_hitl_event_boundary(store, snapshot, delivery_kind, cut):
    from ctx_weft.protocols.hitl import (
        HitlAsk, HitlReply, NoResumeDelivery, ToolResultDelivery, UserTurnDelivery,
    )

    old = runtime(store)
    h = await container(old)
    await append(store, h.session_id,
        (EventType.TASK_CREATED, "t", h.root_agent_id,
         {"task": {"id": "t", "assigned_agent_id": h.root_agent_id}}),
        (EventType.TASK_STARTED, "t", h.root_agent_id, {"assigned_agent_id": h.root_agent_id}),
        (EventType.AGENT_RUNNING, "t", h.root_agent_id, {}))
    delivery = {"tool": ToolResultDelivery("call"), "user": UserTurnDelivery("t"),
                "notification": NoResumeDelivery()}[delivery_kind]
    req = await old.hitl.open(
        HitlAsk(form="approval", delivery=delivery), session_id=h.session_id,
        task_id="t", agent_id=h.root_agent_id, stage="authz", unattended=False)
    if cut != "opened":
        await old.hitl.resolve(HitlReply(hitl_id=req.id, outcome="accepted",
                                        agent_id=h.root_agent_id, message="yes"),
                               defer=cut in {"deferred", "retracted"})
        if cut == "retracted":
            await old.hitl.release(req.id)
        elif cut == "closed":
            await old.hitl.close(req)
    await snapshot_if_requested(store, h.session_id, snapshot)
    rt = runtime(store)
    await rt.initialize()
    pending = cut in {"opened", "deferred", "retracted"}
    expected = "AWAITING_HUMAN" if pending and delivery_kind != "notification" else "INTERRUPTED"
    assert (await rt.get_task(h.session_id, "t")).data.status == expected
    assert bool(await rt.list_pending_hitl(session_id=h.session_id)) == pending
    head = await store.committed_head(h.session_id)
    await runtime(store).initialize()
    assert await store.committed_head(h.session_id) == head
    await rt.rebuild_session(h.session_id, strict=True)
    assert bool(await rt.list_pending_hitl(session_id=h.session_id)) == pending
    assert (await rt.get_task(h.session_id, "t")).data.status == expected
