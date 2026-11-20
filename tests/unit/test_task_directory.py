"""Task directories page committed identities without hydrating execution or bodies."""
# ruff: noqa: F811
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ctx_weft import SessionNotFound, TaskIdPage
from ctx_weft.protocols.events import EventType, EventValueSource, StoredEventValue
from tests.unit.test_read_primitives_conformance import store  # noqa: F401
from tests.unit.test_runtime_initialization_queries import runtime
from tests.unit.test_scoped_recovery_reads import ev, seed


async def test_directory_deduplicates_before_paging_and_freezes_membership(store, monkeypatch):
    await seed(store, ev("session", EventType.SESSION_CREATED),
        ev("z", EventType.TASK_CREATED, task={"id": "a", "outputs": "large" * 10000}),
        ev("y", EventType.TASK_REVIEWING, successor={"task": {"id": "b"}}),
        ev("x", EventType.TASK_REVIEWING, successor={"task": {"id": "b"}}),
        ev("noise", EventType.LLM_RESPONSE_FINISHED, content="large" * 10000),
        ev("w", EventType.TASK_CREATED, task={"id": "c"}))
    core = runtime(store)
    monkeypatch.setattr(store, "read_range", AsyncMock(side_effect=AssertionError("body replay")))
    first = await core.list_task_ids("session", limit=2)
    assert isinstance(first, TaskIdPage) and first.ids == ("a", "b")
    await seed(store, ev("later", EventType.TASK_CREATED, task={"id": "d"}))
    second = await runtime(store).list_task_ids("session", cursor=first.next_cursor, limit=2)
    assert second.ids == ("c",) and second.next_cursor is None
    assert first.committed_position == second.committed_position == 6
    assert not core._task_managers
    assert (await core.list_task_ids("session")).ids == ("a", "b", "c", "d")


async def test_legacy_creation_envelopes_and_malformed_values(store):
    await seed(store, ev("session", EventType.SESSION_CREATED),
        ev("old", EventType.TASK_CREATED, owner="legacy"),
        ev("not-string", EventType.TASK_CREATED, owner="fallback", task={"id": 42}),
        ev("review", EventType.TASK_REVIEWING, owner="parent", successor={"task": {"id": "new"}}),
        ev("broken", EventType.TASK_REVIEWING, owner="parent", successor={"task": {"id": 42}}))
    assert (await runtime(store).list_task_ids("session")).ids == ("legacy", "fallback", "new")


async def test_directory_rejects_cross_session_and_bad_cursors(store):
    await seed(store, ev("session", EventType.SESSION_CREATED),
        ev("a", EventType.TASK_CREATED, task={"id": "a"}),
        ev("b", EventType.TASK_CREATED, task={"id": "b"}))
    core = runtime(store)
    page = await core.list_task_ids("session", limit=1)
    with pytest.raises(ValueError, match="cursor"):
        await core.list_task_ids("other", cursor=page.next_cursor)
    for cursor in ("broken", "", base64.urlsafe_b64encode(json.dumps([1, "session", 999, 1, "a"]).encode()).decode()):
        with pytest.raises(ValueError, match="cursor"):
            await core.list_task_ids("session", cursor=cursor)
    with pytest.raises(SessionNotFound):
        await core.list_task_ids("missing")
    for limit in (0, 501, True, 1.5):
        with pytest.raises(ValueError, match="limit"):
            await core.list_task_ids("session", limit=limit)


async def test_empty_directory_and_same_batch_confirmation(store):
    event = ev("session", EventType.SESSION_CREATED)
    await seed(store, event)
    core = runtime(store)
    assert (await core.list_task_ids("session")).ids == ()
    task = ev("created", EventType.TASK_CREATED, task={"id": "task"})
    await store.append_batch("session", "original", [task])
    before = await core.list_task_ids("session")
    await store.append_batch("session", "original", [task])
    assert await core.list_task_ids("session") == before


async def test_distinct_scalar_query_preserves_literal_keys_and_types(store):
    await seed(store, ev("a", "Sample", **{'quoted"key': {"id": "a"}}),
        ev("b", "Sample", **{'quoted"key': {"id": "b"}}),
        ev("dup", "Sample", **{'quoted"key': {"id": "a"}}),
        ev("wrong", "Sample", **{'quoted"key': {"id": 1}}),
        ev("scalar", "Sample", **{'quoted"key': "not-an-object"}))
    rows = await store.read_distinct_values("session",
        sources={"Sample": EventValueSource(('quoted"key', "id"))}, through_position=5,
        after=StoredEventValue("a", 1), limit=1)
    assert rows == [StoredEventValue("b", 2)]


async def test_postgres_directory_groups_before_cursor_and_limits_scalar_rows():
    from sqlalchemy.dialects.postgresql import dialect
    from ctx_weft.providers.events.store.sql.store import SqlEventStore

    factory = MagicMock()
    db = factory.return_value.__aenter__.return_value
    db.get_bind = MagicMock(return_value=SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))
    db.execute = AsyncMock(return_value=[])
    await SqlEventStore(factory).read_distinct_values("session",
        sources={"Created": EventValueSource(("task", "id"), "task_id")},
        through_position=100, after=StoredEventValue("a", 10), limit=2)
    query = str(db.execute.await_args.args[0].compile(dialect=dialect())).lower()
    assert "min(" in query and "group by" in query and "limit" in query
    assert query.startswith("select anon_1.value, anon_1.position")


async def test_public_task_detail_reads_only_target_facts_and_snapshot_slice(store, monkeypatch):
    from ctx_weft.core.control.reducers import reduce_events
    from tests._snapshot_helpers import seed_snapshot

    events = [ev("session", EventType.SESSION_CREATED, session_id="session"),
        ev("created", EventType.TASK_CREATED, task={"id": "target", "title": "kept", "user_prompt": "input"}),
        ev("foreign", EventType.TASK_CREATED, owner="foreign", task={"id": "foreign"}),
        ev("observe", EventType.OBSERVE_REQUESTED, owner="foreign", task_id="foreign", observe_id="obs")]
    await seed(store, *events)
    await seed_snapshot(store, "session", reduce_events(events, "session"), cut=4)
    read_last, read_range = store.read_last_of_type, store.read_range

    async def last(sid, kind, **kwargs):
        if kind == EventType.STATE_SNAPSHOT:
            paths = kwargs["projection"].payload_fields_by_type[kind]
            assert not any("pending_observe" in path for path in paths)
        return await read_last(sid, kind, **kwargs)

    async def scoped(sid, **kwargs):
        assert kwargs["related_task_id"] == "target"
        assert EventType.OBSERVE_REQUESTED not in kwargs["include_types"]
        return await read_range(sid, **kwargs)

    monkeypatch.setattr(store, "read_last_of_type", last)
    monkeypatch.setattr(store, "read_range", scoped)
    task = (await runtime(store).get_task("session", "target")).data
    assert task.title == "kept" and task.user_prompt == "input"
