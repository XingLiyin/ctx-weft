"""Invalid model summaries are retried before replacement memory is created."""

import pytest

from ctx_weft.protocols import LLMCallError, LLMChunk, LLMUsage, MemoryScope, load_records
from ctx_weft.protocols.events import EventType
from tests.unit.test_compact_history_priority import capsule, cm, setup
from tests.unit.test_compact_step_transitions import ScriptedLLM


def empty_response():
    return [LLMChunk(kind="usage", usage=LLMUsage(prompt_tokens=20, completion_tokens=0))]


@pytest.mark.asyncio
async def test_empty_then_valid_summary_retries_with_paired_request_events(monkeypatch):
    llm = ScriptedLLM([empty_response(), [LLMChunk(kind="token", text="   ")],
                       [LLMChunk(kind="token", text="usable prose digest")]])
    state, ctx, bus = await setup(monkeypatch, llm=llm)
    state.origin = cm.EventOrigin.LOOP_COMPACT
    await capsule(state, ctx, "old", 0)
    await cm.demote_kept_capsules(state, ctx, {"old"})
    n = await cm.fold_root_experience(state, ctx, 0,
        lambda: cm.summarize_for_compact(state, ctx, scope="agent"), lean_only=True)
    assert n == 1 and len(llm.requests) == 3
    recs = await load_records(ctx.memory, cm._agent_half(state.scope), MemoryScope.AGENT, ctx.provider_ctx)
    assert [r.content for r in recs] == ["usable prose digest"]
    started = [e.payload["request_id"] for e in bus.events if e.type == EventType.LLM_REQUEST_STARTED]
    finished = [e.payload["request_id"] for e in bus.events if e.type == EventType.LLM_RESPONSE_FINISHED]
    assert len(started) == len(set(started)) == 3
    assert started == finished
    assert len([e for e in bus.events if e.type == EventType.LLM_RETRY_TRIGGERED]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("layer", ["agent", "task"])
async def test_exhausted_empty_summaries_preserve_selected_records(monkeypatch, layer):
    llm = ScriptedLLM([empty_response(), empty_response(), empty_response()])
    state, ctx, _ = await setup(monkeypatch, llm=llm)
    if layer == "agent":
        await capsule(state, ctx, "old", 0)
        await cm.demote_kept_capsules(state, ctx, {"old"})
        scope, address = MemoryScope.AGENT, cm._agent_half(state.scope)
        operation = cm.fold_root_experience(state, ctx, 0,
            lambda: cm.summarize_for_compact(state, ctx, scope="agent"), lean_only=True)
    else:
        scope, address = MemoryScope.TASK, state.scope
        operation = cm.collapse_task_layer(state, ctx, 1, lambda: cm.summarize_for_compact(state, ctx))
    before = await load_records(ctx.memory, address, scope, ctx.provider_ctx)
    with pytest.raises(LLMCallError, match="original records retained"):
        await operation
    after = await load_records(ctx.memory, address, scope, ctx.provider_ctx)
    assert [r.id for r in after] == [r.id for r in before]
    assert len(llm.requests) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["", " \n\t"])
async def test_direct_empty_digest_cannot_bypass_event_validation(monkeypatch, invalid):
    state, ctx, _ = await setup(monkeypatch)
    await capsule(state, ctx, "old", 0)
    with pytest.raises(LLMCallError, match="original records retained"):
        await cm.fold_root_experience(state, ctx, 0, invalid)
    assert await cm._old_history_remaining(state, ctx)
