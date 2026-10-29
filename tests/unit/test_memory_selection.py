"""Memory selection is lightweight; loading preserves the selected immutable input."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import event

from ctx_weft.core.assembler.sources.agent_recall import AgentRecallSource
from ctx_weft.core.loop.steps.observe import capture_context, context_records
from ctx_weft.protocols import (
    MemoryAddress,
    MemoryEvent,
    MemoryEventType,
    MemoryKind,
    MemoryScope,
    ProviderContext,
)
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from ctx_weft.providers.memory.sql import open_sqlite_memory

BASE = datetime(2026, 1, 1, tzinfo=UTC)
ADDRESS = MemoryAddress(session_id="s", task_id="t", agent_id="a")
CTX = ProviderContext(session_id="s", tenant_id="tenant")


@pytest.fixture(params=["memory", "sqlite"])
async def memory(request, tmp_path):
    if request.param == "memory":
        yield InMemoryMemoryProvider()
    else:
        async with open_sqlite_memory(tmp_path / "selection.db") as provider:
            yield provider


def turn(record_id, seconds=0, **kwargs):
    return MemoryEvent(id=record_id, address=ADDRESS, content=record_id,
        timestamp=BASE + timedelta(seconds=seconds), role="assistant",
        kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK, **kwargs)


async def test_cutoff_order_isolation_and_fold_between_selection_and_load(memory):
    for record in [turn("later", 2), turn("first", 0), turn("tie", 0)]:
        await memory.ingest(record, CTX)
    await memory.ingest(turn("other-tenant"), ProviderContext(session_id="s", tenant_id="other"))
    ids = await memory.load_view_ids(ADDRESS, MemoryScope.TASK, CTX, end_at=BASE.replace(tzinfo=None))
    assert ids == ["first", "tie"]
    await memory.fold(ids, [turn("replacement", 1)], CTX)
    assert await memory.load_view_ids(ADDRESS, MemoryScope.TASK, CTX) == ["replacement", "later"]
    records = await memory.read_records(list(reversed(ids)) + ids, CTX)
    assert [(r.id, r.content) for r in records] == [("first", "first"), ("tie", "tie")]
    assert await memory.read_records(ids, ProviderContext(session_id="wrong", tenant_id="tenant")) == []
    assert await memory.read_records(ids, ProviderContext(session_id="s", tenant_id="other")) == []


async def test_legacy_dispatch_selection_uses_the_same_pairing_as_loading(memory):
    for record_id, type_, tcid, second in [
        ("orphan", MemoryEventType.TASK_DISPATCH, "orphan-call", 0),
        ("dispatch", MemoryEventType.TASK_DISPATCH, "paired-call", 1),
        ("result", MemoryEventType.TASK_DISPATCH_RESULT, "paired-call", 2),
    ]:
        await memory.ingest(MemoryEvent(id=record_id, type=type_, address=ADDRESS,
            content="legacy", timestamp=BASE + timedelta(seconds=second),
            metadata={"tool_call_id": tcid}), CTX)
    address = MemoryAddress(session_id="s", agent_id="a")
    ids = await memory.load_view_ids(address, MemoryScope.AGENT, CTX)
    assert ids == ["dispatch", "result"]
    records = await memory.read_records(ids, CTX)
    assert [r.id for r in records] == ids
    assert [r.role for r in records] == ["assistant", "tool"]
    # Visibility is chosen before the cutoff; loading must not hide the
    # dispatch again when its result is outside the observation's time window.
    partial = await memory.load_view_ids(address, MemoryScope.AGENT, CTX,
                                        end_at=BASE + timedelta(seconds=1))
    assert partial == ["dispatch"]
    assert [r.id for r in await memory.read_records(partial, CTX)] == partial


async def test_loading_preserves_selected_order_for_cross_task_timestamp_ties(memory):
    # Sequence numbers are per task, so different tasks can share both keys.
    for record_id, task_id in [("z-first", "one"), ("a-second", "two")]:
        record = turn(record_id)
        record.address = MemoryAddress(session_id="s", agent_id="a", task_id=task_id)
        await memory.ingest(record, CTX)
    address = MemoryAddress(session_id="s", agent_id="a")
    ids = await memory.load_view_ids(address, MemoryScope.TASK, CTX)
    assert [r.id for r in await memory.read_records(ids, CTX)] == ids


async def test_sql_selection_does_not_load_content_or_blob_references(tmp_path, monkeypatch):
    from ctx_weft.providers.memory.sql import provider as sql

    async with open_sqlite_memory(tmp_path / "lightweight.db") as memory:
        await memory.ingest(turn("large"), CTX)
        statements = []
        engine = memory._factory.kw["bind"].sync_engine

        def trace(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement.lower())

        event.listen(engine, "before_cursor_execute", trace)
        def forbidden(*args, **kwargs):
            pytest.fail("ID selection must not decode records or load blob references")
        monkeypatch.setattr(sql, "_row_to_record", forbidden)
        monkeypatch.setattr(sql, "_declared_refs", forbidden)
        try:
            assert await memory.load_view_ids(ADDRESS, MemoryScope.TASK, CTX) == ["large"]
        finally:
            event.remove(engine, "before_cursor_execute", trace)
        assert len(statements) == 1
        assert "memory_events.content" not in statements[0]
        assert "memory_blob_refs" not in statements[0]


async def test_sql_read_chunks_normalize_legacy_pairs_together(tmp_path, monkeypatch):
    from ctx_weft.providers.memory.sql import provider as sql

    monkeypatch.setattr(sql, "_READ_BATCH_SIZE", 1)
    async with open_sqlite_memory(tmp_path / "chunks.db") as memory:
        for record_id, type_ in [("call", MemoryEventType.TASK_DISPATCH),
                                 ("result", MemoryEventType.TASK_DISPATCH_RESULT)]:
            await memory.ingest(MemoryEvent(id=record_id, type=type_, address=ADDRESS,
                content="legacy", timestamp=BASE, metadata={"tool_call_id": "tc"}), CTX)
        records = await memory.read_records(["result", "call", "call"], CTX)
        assert [r.id for r in records] == ["call", "result"]


async def test_capture_defers_loading_and_observe_reuses_records(memory, monkeypatch):
    await memory.ingest(turn("original"), CTX)
    state = NS(session=NS(id="s"), task=NS(id="t", outputs="original"),
               agent=NS(id="a"), extra={})
    options = {"segment_end_at": (BASE + timedelta(seconds=1)).isoformat()}
    ctx = NS(memory=memory, provider_ctx=CTX, execution=NS(options=options))
    original_read = memory.read_records
    reads = []

    async def tracked(ids, ctx):
        reads.append(tuple(ids))
        return await original_read(ids, ctx)

    monkeypatch.setattr(memory, "read_records", tracked)
    await capture_context(state, ctx, options)
    assert reads == [] and options["context_record_ids"] == ["original"]
    await memory.fold(["original"], [turn("new-summary", 2)], CTX)
    await memory.ingest(turn("next-reply", 3), CTX)
    assert (await context_records(state, ctx))[0].content == "original"
    assert (await context_records(state, ctx))[0].content == "original"
    assert reads == [("original",)]


async def test_prompt_loads_both_scopes_in_one_batch(memory, monkeypatch):
    await memory.ingest(turn("task-turn"), CTX)
    await memory.ingest(MemoryEvent(id="agent-summary", kind=MemoryKind.SUMMARY,
        scope=MemoryScope.AGENT, address=ADDRESS, content="summary", timestamp=BASE), CTX)
    original_read = memory.read_records
    reads = []

    async def tracked(ids, ctx):
        reads.append(tuple(ids))
        return await original_read(ids, ctx)

    monkeypatch.setattr(memory, "read_records", tracked)
    blocks = [b async for b in AgentRecallSource().fetch(
        NS(scope=ADDRESS, extra={}), NS(memory=memory, provider_ctx=CTX))]
    assert len(blocks) == 2
    assert reads == [("task-turn", "agent-summary")]
    assert not hasattr(memory, "load_view")
