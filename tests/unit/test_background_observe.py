"""Tests for background_observe module (Task 4)."""

import asyncio
import pytest
from ctx_weft.core.loop.steps import background_observe as bo


@pytest.fixture(autouse=True)
def _clear_module_state():
    """Clear module-level dicts between tests to avoid cross-test lock/event-loop pollution."""
    bo._task_locks.clear()
    bo._task_pending.clear()
    bo._orphan_tasks.clear()
    yield
    bo._task_locks.clear()
    bo._task_pending.clear()
    bo._orphan_tasks.clear()


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
async def test_await_pending_waits_for_latest_when_two_launched(monkeypatch, fake_state_ctx):
    """Regression: when two observes launch for the same task_id, the first completing
    must NOT clear _task_pending — await_pending_background_observe must wait for the
    second (latest) observe to finish.

    Old behavior (unconditional pop): task1.done_callback pops _task_pending[tid],
    so await_pending finds None and returns immediately while task2 is still running.
    Fix (compare-and-clear): task1.done_callback sees _task_pending[tid] is task2, skips;
    await_pending correctly awaits task2.
    """
    state, ctx = fake_state_ctx
    call_count = 0
    second_done = False

    async def vary_speed(s, c):
        nonlocal call_count, second_done
        call_count += 1
        current = call_count
        if current == 1:
            # First call: completes quickly
            await asyncio.sleep(0)
            return "fast"
        else:
            # Second call: completes slowly
            await asyncio.sleep(0.05)
            second_done = True
            return "slow"

    monkeypatch.setattr(bo, "summarize_for_compact", vary_speed)

    t1 = bo.launch_background_observe(state, ctx)
    t2 = bo.launch_background_observe(state, ctx)  # overwrites _task_pending[tid]

    # Let t1 finish (it's fast); t2 is still queued behind the lock
    await asyncio.sleep(0.01)

    # At this point t1 may be done; second_done must still be False because t2
    # is serialized behind the lock and sleeps 0.05s
    # Now await_pending must block until t2 finishes
    await bo.await_pending_background_observe(state.task.id)

    assert second_done, (
        "await_pending_background_observe returned before the second (latest) observe "
        "finished — _task_pending was incorrectly cleared by the first task's callback"
    )
    await asyncio.gather(t1, t2)  # ensure both are cleaned up


@pytest.mark.asyncio
async def test_failure_swallowed(monkeypatch, fake_state_ctx):
    state, ctx = fake_state_ctx

    async def boom(s, c):
        raise RuntimeError("llm down")

    monkeypatch.setattr(bo, "summarize_for_compact", boom)
    t = bo.launch_background_observe(state, ctx)
    await t  # 不抛
    assert t.exception() is None
