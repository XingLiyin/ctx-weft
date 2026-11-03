"""Assistant memory proofs are independent of state snapshots and survive crashes."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from ctx_weft.core.control.reducers import (
    assistant_memory_floor,
    snapshot_event_payload,
    snapshot_facts_from_event,
)
from ctx_weft.core.control.snapshot_writer import SnapshotWriter
from ctx_weft.core.control.types import RunStateView
from ctx_weft.core.utils.ids import generate_id
from ctx_weft.protocols import MemoryAddress, MemoryEvent, MemoryKind, MemoryScope, ProviderContext
from ctx_weft.protocols.events import Event, EventType
from ctx_weft.providers.events import InMemoryEventStore
from ctx_weft.providers.events.store.sql import open_sqlite_event_store
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from ctx_weft.providers.memory.sql import open_sqlite_memory
from tests.integration.test_minimal_loop import (
    InlineAgentTemplateProvider,
    make_echo_template,
    make_runtime,
)

NOW = datetime(2026, 11, 3, tzinfo=UTC)
SESSION = SimpleNamespace(id="s", tenant_id="acme")
CTX = ProviderContext(session_id="s", tenant_id="acme")
ADDRESS = MemoryAddress(session_id="s", task_id="t", agent_id="a")


async def append(store, kind, **payload):
    event = Event(id=generate_id("evt"), run_id="r", sequence=1, session_id="s",
                  tenant_id="acme", task_id="t", agent_id="a", type=kind,
                  origin="loop.act", timestamp=NOW, payload=payload)
    await store.append_batch("s", event.id, [event])
    return event


async def finished(store, memory_id="reply", **payload):
    return await append(store, EventType.LLM_RESPONSE_FINISHED, memory_id=memory_id,
                        content="saved answer", turn=2, usage={}, **payload)


async def ingest(memory, record_id="reply"):
    return await memory.ingest(MemoryEvent(
        id=record_id, kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK,
        address=ADDRESS, timestamp=NOW, role="assistant", content="saved answer"), CTX)


async def snapshot(store, memory):
    trigger = await append(store, EventType.RUN_FINISHED, outcome="completed")
    writer = SnapshotWriter(store, every_n_events=1, get_memory=lambda: memory)
    await writer.on_event(trigger)
    stored = await store.read_last_of_type("s", EventType.STATE_SNAPSHOT)
    return snapshot_facts_from_event(stored.event)


@pytest.mark.parametrize("proof, expected", [(None, 0), (-1, 0), (True, 0),
                                            (1.5, 0), ("2", 0), (6, 0), (2, 2)])
def test_invalid_proofs_do_not_inherit_the_state_cut(proof, expected):
    payload = snapshot_event_payload(RunStateView("r", "s", "", ""), cut=5,
                                     chain_depth=0, reason="test")
    if proof is None:
        payload.pop("assistant_memory_verified_position")  # Legacy snapshot.
    else:
        payload["assistant_memory_verified_position"] = proof
    event = Event("snapshot", "r", 1, "s", EventType.STATE_SNAPSHOT, NOW, payload=payload)
    assert snapshot_facts_from_event(event).assistant_memory_verified_position == expected


@pytest.mark.asyncio
async def test_sql_recovery_repairs_a_gap_before_a_newer_state_snapshot(tmp_path):
    events_path, memory_path = tmp_path / "events.db", tmp_path / "memory.db"
    async with open_sqlite_event_store(events_path) as store, open_sqlite_memory(memory_path) as memory:
        await append(store, EventType.SESSION_CREATED, root_agent_id="a")
        event = await finished(store, tool_calls=[
            {"id": "tc_saved", "name": "lookup", "arguments": {"q": "hi"}},
            {"id": "tc_delegate", "name": "control__delegate_task", "arguments": {}},
            {"id": "tc_silent", "name": "control__update_task_metadata", "arguments": {}},
        ])
        before = await snapshot(store, memory)
        assert before.last_commit_position == 3
        assert before.assistant_memory_verified_position == 1
        assert "t" not in before.state_blob["tasks"]
        assert await memory.read_records(["reply"], CTX) == []

    # Both databases are closed/reopened, and the failed producer no longer exists.
    async with open_sqlite_event_store(events_path) as store, open_sqlite_memory(memory_path) as memory:
        runtime = make_runtime(event_store=store, agent_provider=InlineAgentTemplateProvider())
        runtime.providers.register_memory(memory)
        await runtime._restore_assistant_messages(SESSION)
        record, = await memory.read_records(["reply"], CTX)
        assert record.id == event.payload["memory_id"]
        assert record.content == "saved answer" and record.timestamp == NOW
        assert record.address == ADDRESS and record.role == "assistant"
        assert record.metadata["turn"] == 2
        assert record.metadata["tool_call_count"] == 3
        assert record.metadata["tool_calls"] == [{"id": "tc_saved", "name": "lookup", "input": {"q": "hi"}}]
        assert "request_id" not in record.metadata
        await memory.fold([record.id], [], CTX)
        await runtime._restore_assistant_messages(SESSION)
        assert await memory.load_view_ids(ADDRESS, MemoryScope.TASK, CTX) == []
        archived, = await memory.read_records([record.id], CTX)
        assert archived.content == "saved answer"
        after = await snapshot(store, memory)
        assert after.assistant_memory_verified_position == after.last_commit_position


@pytest.mark.asyncio
async def test_verified_prefix_is_not_reread_and_only_missing_bodies_are_loaded(monkeypatch):
    store, memory = InMemoryEventStore(), InMemoryMemoryProvider()
    await append(store, EventType.SESSION_CREATED, root_agent_id="a")
    await finished(store, "old")
    await ingest(memory, "old")
    proof = await snapshot(store, memory)
    await finished(store, "new")
    reads = []
    real_read = store.read_range

    async def read(session_id, **kwargs):
        reads.append(kwargs)
        return await real_read(session_id, **kwargs)

    monkeypatch.setattr(store, "read_range", read)
    runtime = make_runtime(event_store=store, agent_provider=InlineAgentTemplateProvider())
    runtime.providers.register_memory(memory)
    await runtime._restore_assistant_messages(SESSION)
    assert all(call["after_position"] >= proof.assistant_memory_verified_position for call in reads)
    assert all(call["include_types"] == (EventType.LLM_RESPONSE_FINISHED,) for call in reads)
    bodies = [call for call in reads if call.get("projection") is None]
    assert len(bodies) == 1
    assert bodies[0]["through_position"] - bodies[0]["after_position"] == 1
    new, = await memory.read_records(["new"], CTX)
    assert new.content == "saved answer"


@pytest.mark.asyncio
async def test_snapshot_keeps_old_proof_when_memory_cannot_be_checked(monkeypatch):
    store, memory = InMemoryEventStore(), InMemoryMemoryProvider()
    await append(store, EventType.SESSION_CREATED, root_agent_id="a")
    await finished(store)
    await ingest(memory)
    old = await snapshot(store, memory)
    await finished(store, "missing")

    async def unavailable(*args):
        raise OSError("memory offline")

    monkeypatch.setattr(memory, "read_records", unavailable)
    new = await snapshot(store, memory)
    assert new.last_commit_position > old.last_commit_position
    assert new.assistant_memory_verified_position == old.assistant_memory_verified_position


@pytest.mark.asyncio
async def test_fixed_snapshot_cut_does_not_verify_a_concurrent_later_reply(monkeypatch):
    store, memory = InMemoryEventStore(), InMemoryMemoryProvider()
    await append(store, EventType.SESSION_CREATED, root_agent_id="a")
    await finished(store)
    await ingest(memory)
    real_read = memory.read_records
    injected = False

    async def read(ids, ctx):
        nonlocal injected
        result = await real_read(ids, ctx)
        if not injected:
            injected = True
            await finished(store, "later")
        return result

    monkeypatch.setattr(memory, "read_records", read)
    facts = await snapshot(store, memory)
    assert facts.assistant_memory_verified_position == facts.last_commit_position == 3
    assert await memory.read_records(["later"], CTX) == []
    runtime = make_runtime(event_store=store, agent_provider=InlineAgentTemplateProvider())
    runtime.providers.register_memory(memory)
    await runtime._restore_assistant_messages(SESSION)
    later, = await memory.read_records(["later"], CTX)
    assert later.content == "saved answer"


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_ack", [False, True])
async def test_repair_write_failure_and_lost_commit_ack(tmp_path, monkeypatch, lost_ack):
    async with open_sqlite_event_store(tmp_path / "events.db") as store, open_sqlite_memory(tmp_path / "memory.db") as memory:
        await finished(store)
        runtime = make_runtime(event_store=store, agent_provider=InlineAgentTemplateProvider())
        runtime.providers.register_memory(memory)
        real_ingest = memory.ingest

        async def fail(event, ctx):
            if lost_ack:
                await real_ingest(event, ctx)
            raise OSError("write failed or acknowledgement lost")

        monkeypatch.setattr(memory, "ingest", fail)
        if lost_ack:
            await runtime._restore_assistant_messages(SESSION)
        else:
            with pytest.raises(OSError):
                await runtime._restore_assistant_messages(SESSION)
            assert await memory.read_records(["reply"], CTX) == []
        monkeypatch.setattr(memory, "ingest", real_ingest)
        await runtime._restore_assistant_messages(SESSION)
        record, = await memory.read_records(["reply"], CTX)
        assert record.content == "saved answer"


@pytest.mark.asyncio
async def test_no_memory_reader_never_claims_the_state_cut_as_verified():
    store = InMemoryEventStore()
    await append(store, EventType.SESSION_CREATED, root_agent_id="a")
    await finished(store)
    trigger = await append(store, EventType.RUN_FINISHED)
    await SnapshotWriter(store, every_n_events=1).on_event(trigger)
    assert await assistant_memory_floor(store, "s", head=await store.committed_head("s")) == 0


@pytest.mark.asyncio
async def test_watermark_never_crosses_the_first_gap():
    store, memory = InMemoryEventStore(), InMemoryMemoryProvider()
    await append(store, EventType.SESSION_CREATED, root_agent_id="a")
    for record_id in ("one", "two", "three", "four"):
        await finished(store, record_id)
    await ingest(memory, "one")
    await ingest(memory, "three")
    assert (await snapshot(store, memory)).assistant_memory_verified_position == 2
    await ingest(memory, "two")
    assert (await snapshot(store, memory)).assistant_memory_verified_position == 4
    assert await memory.read_records(["four"], CTX) == []


@pytest.mark.asyncio
async def test_legacy_snapshot_ahead_of_a_gap_starts_verification_from_zero():
    store, memory = InMemoryEventStore(), InMemoryMemoryProvider()
    await finished(store)
    payload = snapshot_event_payload(RunStateView("r", "s", "", ""), cut=1,
                                     chain_depth=0, reason="legacy")
    payload.pop("assistant_memory_verified_position")
    await append(store, EventType.STATE_SNAPSHOT, **payload)
    runtime = make_runtime(event_store=store, agent_provider=InlineAgentTemplateProvider())
    runtime.providers.register_memory(memory)
    await runtime._restore_assistant_messages(SESSION)
    record, = await memory.read_records(["reply"], CTX)
    assert record.content == "saved answer"


@pytest.mark.asyncio
async def test_cold_rebuild_automatically_repairs_finished_memory(tmp_path):
    from sqlalchemy import delete

    from ctx_weft.core.runtime import SessionStartParams
    from ctx_weft.providers.llm.adapters.mock import MockLLMAdapter, MockResponse
    from ctx_weft.providers.memory.sql.models import MemoryEventModel
    from tests._initialization import initialize_all

    provider = InlineAgentTemplateProvider()
    provider.register(make_echo_template())
    async with open_sqlite_event_store(tmp_path / "events.db") as store, open_sqlite_memory(tmp_path / "memory.db") as memory:
        first = make_runtime(event_store=store, agent_provider=provider,
                             llm=MockLLMAdapter([MockResponse(text="original answer")], observer_verdict="success"))
        first.providers.register_memory(memory)
        await initialize_all(first)
        handle = await first.start_session(SessionStartParams.create(
            template_id="agent:tpl_echo", user_prompt="hi"))
        await handle.wait_for_finish(timeout=10)
        responses = await store.read_range(handle.session_id, include_types=(EventType.LLM_RESPONSE_FINISHED,))
        response, = [row.event for row in responses if row.event.payload.get("memory_id")]
        record_id = response.payload["memory_id"]
        # Leave the finished fact while removing its row, reproducing a missed write.
        async with memory._factory() as db, db.begin():
            await db.execute(delete(MemoryEventModel).where(MemoryEventModel.id == record_id))
        pctx = ProviderContext(session_id=handle.session_id)
        assert await memory.read_records([record_id], pctx) == []
        writer = SnapshotWriter(store, every_n_events=1, get_memory=lambda: memory)
        await writer._write(handle.session_id, response, "test_gap")

        llm = MockLLMAdapter([])
        second = make_runtime(event_store=store, agent_provider=provider, llm=llm)
        second.providers.register_memory(memory)
        await second.rebuild_session(handle.session_id, strict=True)
        restored, = await memory.read_records([record_id], pctx)
        assert restored.content == "original answer"
        assert restored.timestamp == response.timestamp
        assert llm.last_request is None


@pytest.mark.asyncio
async def test_automatic_task_retry_repairs_the_previous_response_first(monkeypatch):
    from ctx_weft.core.runtime import SessionStartParams
    from ctx_weft.providers.llm.adapters.mock import MockLLMAdapter, MockResponse

    provider = InlineAgentTemplateProvider()
    provider.register(make_echo_template())
    memory = InMemoryMemoryProvider()
    real_ingest = memory.ingest
    failed_id = None

    async def fail_first(event, ctx):
        nonlocal failed_id
        if (failed_id is None and event.role == "assistant"
                and event.kind == MemoryKind.CONVERSATION_TURN
                and event.scope == MemoryScope.TASK):
            failed_id = event.id
            raise OSError("assistant write failed after finished commit")
        return await real_ingest(event, ctx)

    monkeypatch.setattr(memory, "ingest", fail_first)
    runtime = make_runtime(agent_provider=provider, llm=MockLLMAdapter([
        MockResponse(text="first answer"),
        *[MockResponse(text="retry answer") for _ in range(8)],
    ], observer_verdict="success"))
    runtime.providers.register_memory(memory)
    handle = await runtime.start_session(SessionStartParams.create(
        template_id="agent:tpl_echo", user_prompt="hi"))
    await handle.wait_for_finish(timeout=10)
    assert failed_id is not None
    record, = await memory.read_records([failed_id], ProviderContext(session_id=handle.session_id))
    assert record.content == "first answer"
    rows = await runtime.event_store.read_range(
        handle.session_id, include_types=(EventType.LLM_RESPONSE_FINISHED,))
    assert any(row.event.payload.get("memory_id") == failed_id for row in rows)
