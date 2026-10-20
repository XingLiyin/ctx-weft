"""Recover real non-close folds and failed HITL commits without losing application work."""

import asyncio

import pytest

from ctx_weft.core.control.reducers import rebuild_view
from ctx_weft.protocols import MemoryAddress, MemoryKind, MemoryScope, ProviderContext
from ctx_weft.protocols.events import EventType
from ctx_weft.providers.events.store.sql import open_sqlite_event_store
from ctx_weft.providers.memory.sql.provider import open_sqlite_memory
from tests._initialization import initialize_all
from tests.integration._lifecycle_crash_worker import make_lifecycle_runtime
from tests.integration.test_lifecycle_crash_boundaries import restart, run_worker
from tests.integration.test_observe_lifecycle_crashes import assert_lifecycles

pytestmark = pytest.mark.asyncio


async def recover_and_drain(store, memory, mode, snapshot):
    rt = await restart(store, memory, mode, snapshot)
    root = (await rt.get_session("crash-session")).data.root_agent_id
    await rt.recover_agent(root, session_id="crash-session")
    tm = rt._task_managers["crash-session"]
    while tm._background_asyncio_tasks:
        # Propagate observer failures; gathering with return_exceptions would hide them.
        await asyncio.gather(*list(tm._background_asyncio_tasks))
    return rt, root


async def assert_cold_recovery_is_noop(store, memory, mode, root):
    head = await store.committed_head("crash-session")
    again, _ = make_lifecycle_runtime(store, memory, mode)
    await initialize_all(again)
    await again.recover_agent(root, session_id="crash-session")
    tm = again._task_managers["crash-session"]
    while tm._background_asyncio_tasks:
        await asyncio.gather(*list(tm._background_asyncio_tasks))
    assert await store.committed_head("crash-session") == head


@pytest.mark.parametrize("snapshot", [False, True])
@pytest.mark.parametrize("cut", ["observe_completed", "segment_fold", "recovery_segment_fold"])
async def test_non_close_completed_recovers_real_fold(tmp_path, snapshot, cut):
    mode = "background_retry"
    await run_worker(tmp_path, mode, "observe_completed" if cut == "recovery_segment_fold" else cut)
    if cut == "recovery_segment_fold":
        await run_worker(tmp_path, mode, "segment_fold", recovering=True)
    async with open_sqlite_event_store(tmp_path / "events.db") as store:
        async with open_sqlite_memory(tmp_path / "memory.db") as memory:
            before = await rebuild_view(store, "crash-session")
            pending, = before.pending_observe.values()
            assert pending["boundary"] == "plain_text"
            assert pending["completion"]["action"] == "recap"
            assert pending["completion"]["result"]["act_recap"] == "reviewed"
            assert not pending["completion"]["short_segment"]
            rt, root = await recover_and_drain(store, memory, mode, snapshot)
            task, = await rt.list_tasks("crash-session")
            assert task.data.status == "AWAITING_HUMAN"
            summaries = await memory.load_view(
                MemoryAddress(session_id="crash-session", agent_id=root, task_id=task.data.task_id),
                MemoryScope.TASK, ProviderContext(session_id="crash-session", tenant_id="default"),
                kinds=[MemoryKind.SUMMARY])
            assert len(summaries) == 1
            assert "reviewed" in str(summaries[0].content)
            assert rt._llm.judgements == rt._llm.actor_count == 0
            rows = await store.read_range("crash-session")
            assert_lifecycles(rows)
            view = await rebuild_view(store, "crash-session")
            assert not view.pending_observe and not view.open_runs
            await assert_cold_recovery_is_noop(store, memory, mode, root)


@pytest.mark.parametrize("snapshot", [False, True])
@pytest.mark.parametrize("second_cut", [None, "hitl_resolved", "task_finished", "observe_applied"])
async def test_failed_hitl_commit_keeps_completed_until_cold_recovery(tmp_path, snapshot, second_cut):
    mode = "plain_text"
    await run_worker(tmp_path, mode, "hitl_commit_failed")
    async with open_sqlite_event_store(tmp_path / "events.db") as store:
        before = await rebuild_view(store, "crash-session")
        pending, = before.pending_observe.values()
        assert pending["completion"]["accepted"]
        assert pending["completion"]["action"] == "close"
        rows = await store.read_range("crash-session")
        assert sum(r.event.type == EventType.TASK_FINALIZED for r in rows) == 1
        assert not any(r.event.type in {EventType.HITL_RESOLVED, EventType.TASK_FINISHED,
                                       EventType.OBSERVE_APPLIED} for r in rows)
    if second_cut:
        await run_worker(tmp_path, mode, second_cut, recovering=True)
    async with open_sqlite_event_store(tmp_path / "events.db") as store:
        async with open_sqlite_memory(tmp_path / "memory.db") as memory:
            rt, root = await recover_and_drain(store, memory, mode, snapshot)
            task, = await rt.list_tasks("crash-session")
            assert task.data.status == "FINISHED"
            assert not rt.hitl.registry.list_pending("crash-session")
            assert rt._llm.judgements == rt._llm.actor_count == 0
            rows = await store.read_range("crash-session")
            assert_lifecycles(rows)
            positions = {}
            for kind in (EventType.OBSERVE_COMPLETED, EventType.TASK_FINALIZED,
                         EventType.HITL_RESOLVED, EventType.TASK_FINISHED, EventType.OBSERVE_APPLIED):
                matches = [r.position for r in rows if r.event.type == kind]
                assert len(matches) == 1, (kind, matches)
                positions[kind] = matches[0]
            assert (positions[EventType.OBSERVE_COMPLETED] < positions[EventType.TASK_FINALIZED]
                    < positions[EventType.HITL_RESOLVED] < positions[EventType.TASK_FINISHED]
                    < positions[EventType.OBSERVE_APPLIED])
            publications = await memory.load_view(
                MemoryAddress(session_id="crash-session"), MemoryScope.SESSION,
                ProviderContext(session_id="crash-session", tenant_id="default"),
                kinds=[MemoryKind.PUBLICATION])
            assert len(publications) == 1
            view = await rebuild_view(store, "crash-session")
            assert not view.pending_observe and not view.open_runs
            await assert_cold_recovery_is_noop(store, memory, mode, root)
