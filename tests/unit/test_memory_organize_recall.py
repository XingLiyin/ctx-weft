"""Fixed material, conditional commits and bounded immutable record reuse."""

import pytest

from ctx_weft.core.assembler import builder
from ctx_weft.core.loop.steps import compact as compact_step
from ctx_weft.core.loop.task_memory_fold import replace_finish_report
from ctx_weft.protocols.events import EventType
from tests.unit.test_compact_history_priority import capsule, cm, setup


@pytest.mark.parametrize("nested", [False, True])
async def test_incremental_costs_equal_full_render_after_each_capsule(monkeypatch, nested):
    state, ctx, _ = await setup(monkeypatch)
    for i in range(5):
        await capsule(state, ctx, f"old{i}", i, parent=state.scope.task_id if nested else None)
    await capsule(state, ctx, "child", 6, parent="old0")
    costs = {}
    while True:
        result = await ctx.memory.organize(ctx.provider_ctx, operation="reduce_history")
        if not result.changed:
            break
        rows = await compact_step._read_memory(state, ctx)
        partial, costs = builder.history_token_costs(
            rows.values(),
            token_counter=ctx.llm.tokenizer.count,
            task_id=state.scope.task_id,
            known=costs,
        )
        full = builder.compile_history(
            rows.values(), token_counter=ctx.llm.tokenizer.count, task_id=state.scope.task_id
        ).token_count
        assert partial == full
        assert result.status != "deferred"


async def test_observer_replacement_between_selection_and_commit_defers_without_deleting(
    monkeypatch,
):
    state, ctx, _ = await setup(monkeypatch)
    await capsule(state, ctx, "old", 0)
    commit = ctx.memory._commit_organize

    async def raced(ids, events, pctx):
        await replace_finish_report(
            ctx.memory,
            pctx,
            cm._agent_half(state.scope),
            "old",
            "close-old",
            "LATEST RECAP",
            "LATEST RESULT",
            "success",
            "old",
        )
        return await commit(ids, events, pctx)

    monkeypatch.setattr(ctx.memory, "_commit_organize", raced)
    result = await ctx.memory.organize(ctx.provider_ctx, operation="reduce_history")
    assert result.status == "deferred" and not result.changed
    rows = await compact_step._read_memory(state, ctx)
    assert "old-body" in rows
    assert any("LATEST RESULT" in str(r.content) for r in rows.values())


async def test_agent_reuses_records_when_only_membership_is_refreshed(monkeypatch):
    state, ctx, _ = await setup(monkeypatch)
    known = await compact_step._read_memory(state, ctx)

    async def unexpected(*args):
        pytest.fail("immutable bodies already present must not be read again")

    monkeypatch.setattr(ctx.memory, "read_records", unexpected)
    assert await compact_step._read_memory(state, ctx, known) == known


@pytest.mark.parametrize("history_count", [10, 40, 100])
@pytest.mark.parametrize("live_parent", [False, True])
async def test_history_payload_reads_and_render_work_stay_bounded(
    monkeypatch, history_count, live_parent
):
    state, ctx, _ = await setup(monkeypatch, target=0.01)
    for i in range(history_count):
        await capsule(state, ctx, f"old{i}", i, parent=state.scope.task_id if live_parent else None)
    stats = {"read_rows": 0, "render_rows": 0, "full_renders": 0}
    read = ctx.memory.read_records
    compile_original = builder.compile_history

    async def measured_read(ids, pctx):
        rows = await read(ids, pctx)
        stats["read_rows"] += len(rows)
        return rows

    def measured_compile(rows, **kwargs):
        stats["render_rows"] += len(rows)
        if len(rows) >= history_count:
            stats["full_renders"] += 1
        return compile_original(rows, **kwargs)

    async def summary(*args, **kwargs):
        return "digest"

    monkeypatch.setattr(ctx.memory, "read_records", measured_read)
    monkeypatch.setattr(builder, "compile_history", measured_compile)
    monkeypatch.setattr(compact_step, "compile_history", measured_compile)
    cm.patch_summary(monkeypatch, summary)
    events = await cm.escalating_compact(state, ctx, token_estimate=10**9)
    assert stats["read_rows"] <= 6 * history_count + 40, stats
    assert stats["render_rows"] <= 16 * history_count + 100, stats
    assert stats["full_renders"] <= 4, stats
    assert [e.payload["source"] for e in events if e.type == EventType.MEMORY_COMPACTED] == [
        "demote_lean",
        "root_experience",
        "collapse",
    ]


async def test_summary_material_reaches_llm_without_retained_current_input():
    from datetime import timedelta

    from ctx_weft.protocols import MemoryAddress, MemoryEvent, MemoryKind, MemoryScope
    from tests.unit.test_compact_fidelity import _CaptureLLM, _state_ctx
    from tests.unit.test_compact_history_priority import BASE

    llm = _CaptureLLM("repaired history")
    state, ctx, _, _ = _state_ctx(llm)
    await capsule(state, ctx, "old", 0)
    await ctx.memory.organize(ctx.provider_ctx, operation="reduce_history")
    await ctx.memory.ingest(
        MemoryEvent(
            id="late-evidence",
            kind=MemoryKind.SUMMARY,
            scope=MemoryScope.TASK,
            address=MemoryAddress(
                session_id=state.scope.session_id, agent_id=state.scope.agent_id, task_id="old"
            ),
            role="assistant",
            timestamp=BASE + timedelta(seconds=50),
            content="LATE_EVIDENCE_MUST_BE_SEEN",
        ),
        ctx.provider_ctx,
    )
    await ctx.memory.ingest(
        MemoryEvent(
            id="unrelated",
            kind=MemoryKind.CONVERSATION_TURN,
            scope=MemoryScope.TASK,
            address=state.scope,
            role="user",
            timestamp=BASE,
            content="UNRELATED_CURRENT_MATERIAL",
        ),
        ctx.provider_ctx,
    )
    rows = await ctx.memory.recall_for_organize(ctx.provider_ctx, operation="fold_history")
    digest = await compact_step._summarize(state, ctx, scope="agent", records=rows)
    sent = "\n".join(str(m.content) for m in llm.last_request.messages)
    assert "LATE_EVIDENCE_MUST_BE_SEEN" in sent and "UNRELATED_CURRENT_MATERIAL" not in sent
    result = await ctx.memory.organize(
        ctx.provider_ctx, operation="fold_history", record_ids=[r.id for r in rows], summary=digest
    )
    assert result.changed
    remaining = await compact_step._read_memory(state, ctx)
    assert "late-evidence" not in remaining and "unrelated" in remaining


async def test_oversized_summary_inputs_preserve_originals():
    from ctx_weft.core.models.errors import ContextOverflowError
    from ctx_weft.protocols import MemoryAddress, MemoryEvent, MemoryKind, MemoryScope
    from tests.model_testkit import model_snapshot
    from tests.unit.test_compact_fidelity import _CaptureLLM, _state_ctx
    from tests.unit.test_compact_history_priority import BASE

    llm = _CaptureLLM("must not be called")
    state, ctx, _, _ = _state_ctx(llm)
    state.resolved_model = model_snapshot(context_limit=3000, output_reserve=0)
    await capsule(state, ctx, "old", 0, summary=False)
    await ctx.memory.ingest(
        MemoryEvent(
            id="huge",
            kind=MemoryKind.CONVERSATION_TURN,
            scope=MemoryScope.TASK,
            address=MemoryAddress(
                session_id=state.scope.session_id, agent_id=state.scope.agent_id, task_id="old"
            ),
            role="assistant",
            timestamp=BASE,
            content="x" * 30000,
        ),
        ctx.provider_ctx,
    )
    before = set(await compact_step._read_memory(state, ctx))
    material = await ctx.memory.recall_for_organize(ctx.provider_ctx, operation="fold_history")
    with pytest.raises(ContextOverflowError):
        await compact_step._summarize(state, ctx, scope="agent", records=material)
    assert llm.last_request is None
    assert set(await compact_step._read_memory(state, ctx)) == before
