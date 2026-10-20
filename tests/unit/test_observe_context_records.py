"""Pinned observe records remain addressable after fold and preserve isolation."""
from ctx_weft.core.utils.clock import now_utc
from ctx_weft.protocols import MemoryAddress, MemoryEvent, MemoryKind, MemoryScope, ProviderContext
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from ctx_weft.providers.memory.sql.provider import open_sqlite_memory


async def exercise(memory):
    address = MemoryAddress(session_id="session", agent_id="agent", task_id="task")
    ctx = ProviderContext(session_id="session", tenant_id="tenant")
    raw = MemoryEvent(id="original", kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK,
        address=address, content="original user input", role="user", timestamp=now_utc())
    await memory.ingest(raw, ctx)
    await memory.fold([raw.id], [MemoryEvent(id="replacement", kind=MemoryKind.SUMMARY,
        scope=MemoryScope.TASK, address=address, content="later summary", timestamp=now_utc())], ctx)
    assert [r.id for r in await memory.load_view(address, MemoryScope.TASK, ctx)] == ["replacement"]
    record, = await memory.read_records([raw.id], ctx)
    assert record.content == "original user input" and record.role == "user"
    assert await memory.read_records([raw.id], ProviderContext(session_id="session", tenant_id="other")) == []
    assert await memory.read_records([raw.id], ProviderContext(session_id="other", tenant_id="tenant")) == []
    assert await memory.read_records([], ctx) == []


async def test_memory_input_survives_fold():
    await exercise(InMemoryMemoryProvider())


async def test_sqlite_input_survives_fold_and_reopen(tmp_path):
    path = tmp_path / "memory.db"
    async with open_sqlite_memory(path) as memory:
        await exercise(memory)
    async with open_sqlite_memory(path) as memory:
        record, = await memory.read_records(["original"], ProviderContext(session_id="session", tenant_id="tenant"))
        assert record.content == "original user input"


async def test_old_recap_cannot_replace_a_later_fold_owner():
    from ctx_weft.core.loop.task_memory_fold import segment_fold
    memory = InMemoryMemoryProvider()
    address = MemoryAddress(session_id="session", agent_id="agent", task_id="task")
    ctx = ProviderContext(session_id="session")
    raw = MemoryEvent(id="original", kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK,
        address=address, content="original answer", role="assistant", timestamp=now_utc())
    await memory.ingest(raw, ctx)
    await memory.fold([raw.id], [MemoryEvent(id="new-owner", kind=MemoryKind.SUMMARY,
        scope=MemoryScope.TASK, address=address, content="accepted later report", timestamp=now_utc())], ctx)
    await segment_fold(memory, address, MemoryScope.TASK, "obsolete recap", ctx,
                       operation_id="old-observe", context_record_ids=[raw.id])
    current = await memory.load_view(address, MemoryScope.TASK, ctx)
    assert [(r.id, r.content) for r in current] == [("new-owner", "accepted later report")]


async def test_pinned_audits_count_for_short_segment_without_entering_prompt():
    from types import SimpleNamespace as NS
    from ctx_weft.core.assembler.sources.agent_recall import AgentRecallSource
    from ctx_weft.core.loop.steps.observe import ObserveStep, capture_context, context_records
    from ctx_weft.providers.llm.tokenizer import HeuristicTokenizer
    memory = InMemoryMemoryProvider()
    address = MemoryAddress(session_id="session", agent_id="agent", task_id="task")
    ctx = NS(memory=memory, provider_ctx=ProviderContext(session_id="session"),
             llm=NS(tokenizer=HeuristicTokenizer()))
    for i, (kind, content) in enumerate([(MemoryKind.CONVERSATION_TURN, "one"),
            (MemoryKind.CONVERSATION_TURN, "two"), (MemoryKind.TOOL_AUDIT, "audit " * 1000)]):
        await memory.ingest(MemoryEvent(id=str(i), kind=kind, scope=MemoryScope.TASK,
            address=address, content=content, role="assistant", timestamp=now_utc()), ctx.provider_ctx)
    state = NS(session=NS(id="session"), task=NS(id="task"), extra={}, scope=address,
               agent=NS(id="agent", loop_config=NS(short_segment_token_threshold=100)))
    options = {"segment_end_at": now_utc().isoformat()}
    await capture_context(state, ctx, options)
    assert set(options["context_record_ids"]) == {"0", "1", "2"}
    ctx.execution = NS(options=options)
    assert not await ObserveStep()._is_short_segment(state, ctx)
    records = await context_records(state, ctx)
    blocks = [b async for b in AgentRecallSource().fetch(NS(extra={"observe_records": records}), None)]
    assert len(blocks) == 2 and all("audit" not in str(b) for b in blocks)
