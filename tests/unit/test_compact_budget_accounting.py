"""Compaction stops using the same message costs as prepare and the gateway."""

from datetime import timedelta
from types import SimpleNamespace as NS

import pytest

from ctx_weft.core.loop.llm_gateway import _estimate_message_tokens
from ctx_weft.core.loop.steps.prepare import _estimate_assembled_tokens
from ctx_weft.core.utils.estimate import estimate_message_tokens
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
from tests.unit.test_escalating_compact import _const, _state


def test_message_costs_agree_for_tools_images_framing_and_emitted_reasoning():
    message = LLMMessage(
        role="assistant", content=[TextPart(text="look"), ImagePart(data="eA==", media_type="image/png")],
        tool_calls=[{"name": "example__work", "arguments": {"payload": "x" * 1000}}],
        reasoning_content="reasoning actually sent",
    )
    cost = estimate_message_tokens(message, len)
    assert cost > 2600
    assert cost == _estimate_message_tokens(message, len)
    assert cost == _estimate_assembled_tokens(NS(system="", messages=[message], tools=[]), len)


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

    monkeypatch.setattr(cm, "summarize_for_compact", unexpected_summary)
    events = await cm.escalating_compact(state, ctx, token_estimate=80_000)
    levels = [e.payload for e in events if e.type == EventType.MEMORY_COMPACTED]
    assert [p["source"] for p in levels] == ["demote_lean"]
    assert levels[0]["freed_tokens"] > 70_000
    current = await load_records(ctx.memory, state.scope, MemoryScope.TASK, ctx.provider_ctx)
    assert not any(r.metadata.get("collapsed") for r in current)


@pytest.mark.asyncio
async def test_growth_during_compaction_is_not_discarded_from_budget(monkeypatch):
    costs = iter([900, 1100, 800, 500])

    async def measure(*args):
        return next(costs)

    monkeypatch.setattr(cm, "_active_memory_tokens", measure)
    monkeypatch.setattr(cm, "_kept_origin_ids", lambda *args: _const(["old"]))
    monkeypatch.setattr(cm, "demote_kept_capsules", lambda *args, **kwargs: 1)
    monkeypatch.setattr(cm, "_old_history_remaining", lambda *args: _const(False))
    monkeypatch.setattr(cm, "fold_root_experience", lambda *args, **kwargs: 1)
    collapsed = []
    monkeypatch.setattr(cm, "collapse_task_layer", lambda *args: collapsed.append(True) or 1)
    state = _state(ratio_target=0.65)
    state.agent.loop_config.compact_keep_last = 0
    memory = NS(load_view_ids=lambda *args, **kwargs: _const(list(range(10))),
                read_records=lambda ids, ctx: _const([object()] * len(ids)))
    events = await cm.escalating_compact(state, NS(memory=memory, provider_ctx=None), token_estimate=900)
    assert collapsed == [True]  # 800 after L2 is still above 650.
    assert events[-1].payload["est_after"] == 500
