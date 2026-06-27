"""Tests for background_observe module (Task 4)."""

import asyncio
import pytest
from ctx_weft.core.loop.steps import background_observe as bo


def _async_const(value: str):
    """Return an async function that always returns value."""
    async def _impl(s, c):
        return value
    return _impl


@pytest.mark.asyncio
async def test_launch_produces_summary_and_folds(monkeypatch, fake_state_ctx):
    state, ctx = fake_state_ctx  # task 层有 [UP, llm, tool]
    monkeypatch.setattr(bo, "summarize_for_compact", _async_const("段摘要文本"))
    t = bo.launch_background_observe(state, ctx)
    await t
    # apply_compact 被调、产出 TASK_COMPACT_SUMMARY、UP 保留
    from ctx_weft.protocols import MemoryEventType as MT
    recs = await ctx.memory.recall_recent(
        state.scope, [MT.USER_PROMPT, MT.LLM_RESPONSE, MT.TOOL_RESULT, MT.TASK_COMPACT_SUMMARY],
        100, ctx.provider_ctx)
    types = {r.type for r in recs}
    assert MT.TASK_COMPACT_SUMMARY in types
    assert MT.USER_PROMPT in types
    assert MT.LLM_RESPONSE not in types


@pytest.mark.asyncio
async def test_serialized_per_task(monkeypatch, fake_state_ctx):
    state, ctx = fake_state_ctx
    order = []

    async def slow(s, c):
        order.append("start")
        await asyncio.sleep(0.01)
        order.append("end")
        return "x"

    monkeypatch.setattr(bo, "summarize_for_compact", slow)
    t1 = bo.launch_background_observe(state, ctx)
    t2 = bo.launch_background_observe(state, ctx)
    await asyncio.gather(t1, t2)
    assert order == ["start", "end", "start", "end"]  # 串行，不交错


@pytest.mark.asyncio
async def test_failure_swallowed(monkeypatch, fake_state_ctx):
    state, ctx = fake_state_ctx

    async def boom(s, c):
        raise RuntimeError("llm down")

    monkeypatch.setattr(bo, "summarize_for_compact", boom)
    t = bo.launch_background_observe(state, ctx)
    await t  # 不抛
    assert t.exception() is None
