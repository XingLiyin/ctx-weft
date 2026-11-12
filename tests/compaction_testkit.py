"""Behavioral fixture adapters for the memory organize protocol.

Old count-returning test helpers delegate to the public memory operations. Pure
classifier aliases remain only for the existing boundary algorithm unit tests.
"""

import dataclasses
import inspect
from types import SimpleNamespace

from ctx_weft.core.assembler.builder import compile_history
from ctx_weft.core.assembler.legacy_task_memory import capsule_origin as capsule_origin
from ctx_weft.core.loop.steps import compact as compact_step
from ctx_weft.core.loop.steps.compact import CompactStep
from ctx_weft.protocols.events import EventOrigin as EventOrigin
from ctx_weft.protocols.events import EventType as EventType
from ctx_weft.providers.memory import organize as memory_ops

COLLAPSE_DELIM = memory_ops.COLLAPSE_DELIM
LEGACY_COLLAPSE_DELIMS = memory_ops.LEGACY_COLLAPSE_DELIMS
DIGEST_SECTIONS = memory_ops.DIGEST_SECTIONS
REQUIRED_DIGEST_SECTIONS = memory_ops.REQUIRED_DIGEST_SECTIONS
ParsedDigest = memory_ops._Digest
parse_structured_digest = memory_ops.parse_structured_digest
_TASK_VIEW_KINDS = memory_ops._VIEW_KINDS
_agent_half = lambda scope: dataclasses.replace(scope, task_id=None)
_original_section = memory_ops._original_section
_split_for_collapse = memory_ops._split_for_collapse
_dispatch_finish_sets = memory_ops._dispatch_finish_sets


def _pctx(state, ctx):
    return dataclasses.replace(
        ctx.provider_ctx, task_id=state.scope.task_id, agent_id=state.scope.agent_id
    )


def patch_summary(monkeypatch, callback):
    async def generate(state, ctx, *, scope="task", records):
        kwargs = {"scope": scope}
        if "record_ids" in inspect.signature(callback).parameters:
            kwargs["record_ids"] = tuple(r.id for r in records)
        result = await callback(state, ctx, **kwargs)
        return result.text if isinstance(result, ParsedDigest) else result

    monkeypatch.setattr(compact_step, "_summarize", generate)


async def summarize_for_compact(state, ctx, *, scope="task", record_ids=None):
    rows = (
        []
        if not hasattr(ctx, "memory")
        else list((await memory_ops._read_working(ctx.memory, _pctx(state, ctx))).records)
        if record_ids is None
        else await ctx.memory.read_records(record_ids, ctx.provider_ctx)
    )
    return parse_structured_digest(
        await compact_step._summarize(state, ctx, scope=scope, records=rows)
    )


async def _digest(value, selection=None):
    if callable(value):
        value = value(selection) if "plan" in inspect.signature(value).parameters else value()
    if inspect.isawaitable(value):
        value = await value
    value = value.text if isinstance(value, ParsedDigest) else value
    if not value.strip():
        from ctx_weft.protocols import LLMCallError

        raise LLMCallError("Invalid compaction summary; original records retained", retriable=False)
    return value


async def _history_view(state, ctx):
    history = await memory_ops._read_working(ctx.memory, _pctx(state, ctx))
    return list(history.agent), list(history.body)


async def _active_memory_tokens(state, ctx):
    history = await memory_ops._read_working(ctx.memory, _pctx(state, ctx))
    return compile_history(
        history.records, token_counter=ctx.llm.tokenizer.count, task_id=state.scope.task_id
    ).token_count


async def _kept_origin_ids(state, ctx, keep_last):
    history = await memory_ops._read_working(ctx.memory, _pctx(state, ctx))
    roots = list(memory_ops._task_graph(history.agent, state.scope.task_id, history.body).roots)
    return roots if keep_last is None else roots[-keep_last:] if keep_last > 0 else []


async def _count_root_residues(state, ctx):
    return len(await _kept_origin_ids(state, ctx, None))


async def _old_history_remaining(state, ctx):
    history = await memory_ops._read_working(ctx.memory, _pctx(state, ctx))
    return memory_ops._history_details_remain(history, state.scope.task_id)


async def collapse_task_layer(state, ctx, keep_last, summary_text):
    pctx = _pctx(state, ctx)
    rows = await ctx.memory.recall_for_organize(pctx, operation="fold_current", keep_last=keep_last)
    if not rows:
        return 0
    original_pctx = ctx.provider_ctx
    ctx.provider_ctx = pctx
    try:
        rows = await compact_step._prepare_summary_records(ctx, rows)
    finally:
        ctx.provider_ctx = original_pctx
    result = await ctx.memory.organize(
        pctx,
        operation="fold_current",
        record_ids=[r.id for r in rows],
        summary=await _digest(summary_text),
    )
    return len(result.superseded_ids)


async def fold_root_experience(state, ctx, keep_last, summary_text, *, lean_only=False):
    pctx = _pctx(state, ctx)
    history = await memory_ops._read_working(ctx.memory, pctx)
    selection = memory_ops._history_selection(
        history, state.scope.task_id, keep_last, lean_only=lean_only
    )
    rows = list(selection.records) if selection else []
    if not rows:
        return 0
    demoted = await ctx.memory.organize(
        pctx, operation="demote_images", record_ids=[r.id for r in rows], keep_recent_images=0
    )
    if demoted.changed:
        rows = [r for r in rows if r.id not in demoted.superseded_ids] + list(demoted.replacements)
        selection = dataclasses.replace(
            selection, records=tuple(rows), summary_ids=tuple(r.id for r in rows)
        )
    result = await ctx.memory.organize(
        pctx,
        operation="fold_history",
        record_ids=[r.id for r in rows],
        summary=await _digest(summary_text, selection),
    )
    return len(result.superseded_ids)


async def demote_kept_capsules(state, ctx, origin_ids, *, repair_summary=None):
    pctx = _pctx(state, ctx)
    history = await memory_ops._read_working(ctx.memory, pctx)
    total = 0
    for selection in memory_ops._capsules(history, state.scope.task_id):
        if selection.roots[0] not in origin_ids:
            continue
        if selection.action == "demote":
            result = await ctx.memory.organize(
                pctx, operation="reduce_history", record_ids=selection.ids
            )
        elif repair_summary is not None:
            result = await ctx.memory.organize(
                pctx,
                operation="fold_history",
                record_ids=selection.ids,
                summary=await _digest(repair_summary),
            )
        else:
            continue
        total += len(result.superseded_ids)
    return total


async def escalating_compact(state, ctx, *, token_estimate, trigger="compact", force=False):
    if not hasattr(ctx, "llm"):
        ctx.llm = SimpleNamespace(tokenizer=SimpleNamespace(count=len))
    if not hasattr(state.agent, "loop_guard"):
        state.agent.loop_guard = SimpleNamespace(context_tokens=token_estimate)
    if not hasattr(state.agent.loop_guard, "context_tokens"):
        state.agent.loop_guard.context_tokens = token_estimate
    previous = getattr(ctx, "execution", None)
    previous_pctx = ctx.provider_ctx
    ctx.provider_ctx = _pctx(state, ctx) if ctx.provider_ctx is not None else None
    ctx.execution = SimpleNamespace(
        options={"token_estimate": token_estimate, "trigger": trigger, "force": force}
    )
    try:
        return (await CompactStep().execute(state, ctx)).events
    finally:
        ctx.execution = previous
        ctx.provider_ctx = previous_pctx
