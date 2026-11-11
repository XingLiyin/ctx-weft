"""Per-run caching preserves rendered costs, commit checks and bounded row I/O."""

import asyncio
from contextlib import asynccontextmanager

import pytest

from ctx_weft.core.assembler.builder import compile_history
from ctx_weft.core.loop.steps import _compact_snapshot as snapshots
from ctx_weft.core.loop.task_memory_fold import replace_finish_report
from ctx_weft.protocols import MemoryScope, load_records
from ctx_weft.protocols.events import EventType
from tests.unit.test_compact_history_priority import capsule, cm, setup

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("nested", [False, True])
async def test_incremental_costs_equal_full_render_after_each_capsule(monkeypatch, nested):
    state, ctx, _ = await setup(monkeypatch)
    parent = state.scope.task_id if nested else None
    for index in range(5):
        await capsule(state, ctx, f"old{index}", index, parent=parent)
    await capsule(state, ctx, "child", 6, parent="old0")
    with snapshots.compact_snapshot(state, ctx):
        for oid in await cm._kept_origin_ids(state, ctx, None):
            await cm._active_memory_tokens(state, ctx)
            await cm.demote_kept_capsules(state, ctx, {oid})
            incremental = await cm._active_memory_tokens(state, ctx)
            recs, body = await cm._history_view(state, ctx)
            full = compile_history([*recs, *body], token_counter=ctx.llm.tokenizer.count,
                                   task_id=state.scope.task_id).token_count
            assert incremental == full
        assert not state.extra.get(cm._DEFERRED_HISTORY_KEY)
    assert snapshots.current_snapshot(ctx) is None


async def test_observer_replacement_between_selection_and_commit_refreshes_plan(monkeypatch):
    state, ctx, _ = await setup(monkeypatch, target=0.01)
    await capsule(state, ctx, "old", 0)
    checkpoint = cm._history_commit
    replaced = False

    @asynccontextmanager
    async def observer_then_commit(current, local_ctx, origins):
        nonlocal replaced
        if not replaced:
            replaced = True
            await replace_finish_report(ctx.memory, ctx.provider_ctx, cm._agent_half(state.scope),
                "old", "close-old", "LATEST RECAP", "LATEST RESULT", "success", "old")
        async with checkpoint(current, local_ctx, origins):
            yield

    monkeypatch.setattr(cm, "_history_commit", observer_then_commit)
    events = await cm.escalating_compact(state, ctx, token_estimate=90_000)
    recs = await load_records(ctx.memory, cm._agent_half(state.scope), MemoryScope.AGENT, ctx.provider_ctx)
    assert len(recs) == 1 and "LATEST RESULT" in recs[0].content
    assert state.extra[cm._DEFERRED_HISTORY_KEY]
    assert [e.payload["source"] for e in events if e.type == EventType.MEMORY_COMPACTED] == ["demote_lean"]
    assert snapshots.current_snapshot(ctx) is None


async def test_snapshot_is_not_shared_with_background_task_and_resets_on_failure(monkeypatch):
    state, ctx, _ = await setup(monkeypatch)
    with pytest.raises(RuntimeError, match="stop"):
        with snapshots.compact_snapshot(state, ctx):
            snapshot = snapshots.current_snapshot(ctx)
            assert snapshot is not None
            await cm._history_view(state, ctx)
            assert snapshot.records

            async def background():
                assert snapshots.current_snapshot(ctx) is None

            await asyncio.create_task(background())
            raise RuntimeError("stop")
    assert snapshots.current_snapshot(ctx) is None
    assert not snapshot.records and not snapshot.ids


@pytest.mark.parametrize("history_count", [10, 40, 100])
@pytest.mark.parametrize("live_parent", [False, True])
async def test_history_payload_reads_and_render_work_stay_bounded(monkeypatch, history_count, live_parent):
    state, ctx, _ = await setup(monkeypatch, target=0.01)
    for index in range(history_count):
        await capsule(state, ctx, f"old{index}", index,
                      parent=state.scope.task_id if live_parent else None)
    stats = {"read_rows": 0, "render_rows": 0, "full_renders": 0}
    read = ctx.memory.read_records
    compile_original = snapshots.compile_history

    async def measured_read(ids, provider_ctx):
        rows = await read(ids, provider_ctx)
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
    monkeypatch.setattr(snapshots, "compile_history", measured_compile)
    monkeypatch.setattr(cm, "summarize_for_compact", summary)
    events = await cm.escalating_compact(state, ctx, token_estimate=10**9)
    assert stats["read_rows"] <= 6 * history_count + 40, stats
    assert stats["render_rows"] <= 16 * history_count + 100, stats
    assert stats["full_renders"] <= 4, stats
    assert [e.payload["source"] for e in events if e.type == EventType.MEMORY_COMPACTED] == [
        "demote_lean", "root_experience", "collapse"]
