"""Compaction stops using the same message costs as prepare and the gateway."""

from datetime import timedelta
from types import SimpleNamespace as NS

import pytest

from ctx_weft.core.loop.llm_gateway import _estimate_message_tokens
from ctx_weft.core.utils.estimate import estimate_assembled_tokens, estimate_message_tokens
from ctx_weft.protocols import (
    ImagePart,
    LLMMessage,
    MemoryAddress,
    MemoryEvent,
    MemoryKind,
    MemoryScope,
    TextPart,
    load_records,
)
from ctx_weft.protocols.events import EventType
from tests.unit.test_compact_history_priority import BASE, capsule, cm, setup


def test_message_costs_agree_for_tools_images_framing_and_emitted_reasoning():
    message = LLMMessage(
        role="assistant", content=[TextPart(text="look"), ImagePart(data="eA==", media_type="image/png")],
        tool_calls=[{"name": "example__work", "arguments": {"payload": "x" * 1000}}],
        reasoning_content="reasoning actually sent",
    )
    cost = estimate_message_tokens(message, len)
    assert cost > 2600
    assert cost == _estimate_message_tokens(message, len)
    assert cost == estimate_assembled_tokens(NS(system="", messages=[message], tools=[]), len)


@pytest.mark.asyncio
async def test_l1_removing_large_tool_arguments_stops_before_summarization(monkeypatch):
    state, ctx, _ = await setup(monkeypatch, target=0.58)
    await capsule(state, ctx, "old", 0)
    await ctx.memory.ingest(MemoryEvent(
        kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK,
        address=MemoryAddress(session_id=state.scope.session_id, agent_id=state.scope.agent_id,
                              task_id="old"),
        role="assistant", content="", timestamp=BASE + timedelta(seconds=1),
        metadata={"tool_calls": [{"id": "big-call", "name": "example__work",
                                  "arguments": {"payload": "x" * 120_000}}]},
    ), ctx.provider_ctx)

    async def unexpected_summary(*args, **kwargs):
        pytest.fail("Removing the large call already meets the budget")

    cm.patch_summary(monkeypatch, unexpected_summary)
    events = await cm.escalating_compact(state, ctx, token_estimate=80_000)
    levels = [e.payload for e in events if e.type == EventType.MEMORY_COMPACTED]
    assert [p["source"] for p in levels] == ["demote_lean"]
    assert levels[0]["freed_tokens"] > 70_000
    current = await load_records(ctx.memory, state.scope, MemoryScope.TASK, ctx.provider_ctx)
    assert not any(r.metadata.get("collapsed") for r in current)


@pytest.mark.asyncio
async def test_growth_during_compaction_is_not_discarded_from_budget(monkeypatch):
    state,ctx,_=await setup(monkeypatch,target=0.01)
    for i in range(8):await capsule(state,ctx,f"old{i}",i)
    before=await cm._active_memory_tokens(state,ctx)
    calls=[]
    async def summary(*args,scope="task",**kwargs):
        calls.append(scope)
        return "growth"*1000 if len(calls)==1 else "digest"
    cm.patch_summary(monkeypatch,summary)
    events=await cm.escalating_compact(state,ctx,token_estimate=90_000)
    after=await cm._active_memory_tokens(state,ctx)
    assert calls==["agent","agent","task"]
    assert events[-1].payload["est_after"]==90_000+after-before
