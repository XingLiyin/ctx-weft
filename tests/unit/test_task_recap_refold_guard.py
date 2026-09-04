import asyncio
from datetime import UTC, datetime

import pytest
from types import SimpleNamespace
import ctx_weft.core.loop.steps.background_observe as bo
from ctx_weft.protocols.events import EventType
from ctx_weft.protocols import MemoryEvent, MemoryEventType


@pytest.mark.asyncio
async def test_compact_boundary_skips_when_no_active_raw(fake_state_ctx, monkeypatch):
    state, ctx = fake_state_ctx
    # Augment agent with max_turns_per_observe
    state.agent.loop_config = SimpleNamespace(compact_keep_last=2, max_turns_per_observe=3)
    # 该段已折叠：视图内无 active assistant 回合（新护栏走 load_view，不再经 count_recent）
    async def _empty_view(address, scope, pctx, kinds=None):
        return []
    monkeypatch.setattr(ctx.memory, "load_view", _empty_view)
    called = {"react": False}
    async def _react(*a, **k):
        called["react"] = True
        return None, ""
    monkeypatch.setattr(bo, "run_observe_react", _react)

    await bo._run_background_observe(state, ctx, boundary="interrupt")

    assert called["react"] is False  # 护栏跳过，未跑 LLM observe


@pytest.mark.asyncio
async def test_close_boundary_not_guarded(fake_state_ctx, monkeypatch):
    state, ctx = fake_state_ctx
    # Augment agent with max_turns_per_observe
    state.agent.loop_config = SimpleNamespace(compact_keep_last=2, max_turns_per_observe=3)
    async def _count(scope, types, pctx):
        return 0  # 即便为 0，close 边界也不受护栏影响
    monkeypatch.setattr(ctx.memory, "count_recent", _count)
    called = {"react": False}
    async def _react(*a, **k):
        called["react"] = True
        from ctx_weft.core.capabilities.control_tools import ControlResult
        return ControlResult(content="r", metadata={}), ""
    monkeypatch.setattr(bo, "run_observe_react", _react)

    await bo._run_background_observe(state, ctx, boundary="finish")

    assert called["react"] is True  # close 边界照常跑


@pytest.mark.asyncio
async def test_close_boundary_exception_pops_close_synth(fake_state_ctx, monkeypatch):
    """close 边界 bg observe 抛异常时须弹掉 finalize 已登记的 _close_synth 槽
    ——登记此后永远无人消费（task_id 唯一 + close 单入口），不弹即模块级 dict 泄漏。
    异常被吞（不外抛），finally 仍发 TASK_RECAP_DONE。"""
    state, ctx = fake_state_ctx
    state.agent.loop_config = SimpleNamespace(compact_keep_last=2, max_turns_per_observe=3)
    bo.register_close_synth(state.task.id, "tcall_x", state.scope, "success")

    async def _boom(*a, **k):
        raise RuntimeError("llm outage")
    monkeypatch.setattr(bo, "run_observe_react", _boom)

    await bo._run_background_observe(state, ctx, boundary="finish")  # 不应外抛

    assert bo.pop_close_synth(state.task.id) is None, "异常路径应弹掉登记（防泄漏）"
    done_events = [e for e in ctx.event_bus.emitted if e.type == EventType.TASK_RECAP_DONE]
    assert len(done_events) == 1


@pytest.mark.asyncio
async def test_short_segment_kept_raw_no_llm_call(fake_state_ctx, monkeypatch):
    """短段免折：本段 active raw token ≤ short_segment_token_threshold → 不跑后台 LLM、
    不折叠（raw 保持 active、无 TASK_COMPACT_SUMMARY），finally 仍发 TASK_RECAP_DONE。"""
    state, ctx = fake_state_ctx
    state.agent.loop_config = SimpleNamespace(
        compact_keep_last=2, max_turns_per_observe=3,
        short_segment_token_threshold=400,  # 种子 raw（hello llm / tool result）远低于此
    )
    called = {"react": False}
    async def _react(*a, **k):
        called["react"] = True
        return None, ""
    monkeypatch.setattr(bo, "run_observe_react", _react)

    await bo._run_background_observe(state, ctx, boundary="plain_text")

    assert called["react"] is False  # 短段直接跳过，连 LLM 都不跑
    n_raw = await ctx.memory.count_recent(
        state.scope, [MemoryEventType.LLM_RESPONSE], ctx.provider_ctx,
    )
    assert n_raw == 1, "短段的 raw 应保持 active（未被 supersede）"
    n_summary = await ctx.memory.count_recent(
        state.scope, [MemoryEventType.TASK_COMPACT_SUMMARY], ctx.provider_ctx,
    )
    assert n_summary == 0, "短段不应产 TASK_COMPACT_SUMMARY"
    done_events = [e for e in ctx.event_bus.emitted if e.type == EventType.TASK_RECAP_DONE]
    assert len(done_events) == 1  # finally 无条件发 DONE，跳过不影响收尾


@pytest.mark.asyncio
async def test_segment_over_threshold_folds_as_before(fake_state_ctx, monkeypatch):
    """超过阈值的段照常折叠（门只放行短段）。

    段内需 ≥2 条 LLM 回复：单回复段无条件免折（is_short_segment 的单回复门）。"""
    state, ctx = fake_state_ctx
    state.agent.loop_config = SimpleNamespace(
        compact_keep_last=2, max_turns_per_observe=3,
        short_segment_token_threshold=1,  # 种子 raw 必然超过
    )
    await ctx.memory.ingest(MemoryEvent(
        type=MemoryEventType.LLM_RESPONSE, address=state.scope,
        content="second llm", role="assistant",
        timestamp=datetime(2024, 1, 1, 12, 0, 0, 4, tzinfo=UTC)), ctx.provider_ctx)
    from ctx_weft.core.capabilities.control_tools import ControlResult
    called = {"react": False}
    async def _react(*a, **k):
        called["react"] = True
        return ControlResult(content="segment recap", metadata={}), ""
    monkeypatch.setattr(bo, "run_observe_react", _react)

    await bo._run_background_observe(state, ctx, boundary="plain_text")

    assert called["react"] is True
    n_raw = await ctx.memory.count_recent(
        state.scope, [MemoryEventType.LLM_RESPONSE], ctx.provider_ctx,
    )
    assert n_raw == 0, "超阈值段应照常折叠（raw 被 supersede）"


@pytest.mark.asyncio
async def test_close_boundary_ignores_short_segment_gate(fake_state_ctx, monkeypatch):
    """close 边界（finish/normal）不受短段门影响：finish 对的 Process Report 与段大小无关。"""
    state, ctx = fake_state_ctx
    state.agent.loop_config = SimpleNamespace(
        compact_keep_last=2, max_turns_per_observe=3,
        short_segment_token_threshold=10**9,  # 巨大阈值也拦不住 close 路径
    )
    from ctx_weft.core.capabilities.control_tools import ControlResult
    called = {"react": False}
    async def _react(*a, **k):
        called["react"] = True
        return ControlResult(content="r", metadata={}), ""
    monkeypatch.setattr(bo, "run_observe_react", _react)

    await bo._run_background_observe(state, ctx, boundary="finish")

    assert called["react"] is True


@pytest.mark.asyncio
async def test_second_launch_over_already_folded_segment_is_skipped_by_real_guard(
    fake_state_ctx, monkeypatch,
):
    """端到端（真实 ctx.memory，不 mock count_recent）：同一 task 背靠背两次 interrupt
    边界 observe——第一次真实折叠该段（LLM_RESPONSE 被 supersede），第二次落到该段
    count_recent(LLM_RESPONSE)==0，护栏应跳过其 LLM/observe 工作，但 finally 仍须为
    两次启动各发一条 TASK_RECAP_DONE，且 await_pending_background_observe 对被跳过的
    第二个任务也要正常收尾（不挂起、不报错）。

    若把 _run_background_observe 里的护栏摘掉，第二次也会真的调 run_observe_react，
    本测试的 call_count 断言会失败（变成 2）。
    """
    state, ctx = fake_state_ctx
    state.agent.loop_config = SimpleNamespace(compact_keep_last=2, max_turns_per_observe=3)
    state.session = SimpleNamespace(id="s1", tenant_id="default", token_used=0)

    from ctx_weft.core.capabilities.control_tools import ControlResult

    calls: list[int] = []

    async def _react(*a, **k):
        calls.append(1)
        return ControlResult(content=f"S{len(calls)}", metadata={}), ""

    monkeypatch.setattr(bo, "run_observe_react", _react)

    # 背靠背发起两次同 task interrupt observe（不 await 第一个就发第二个），
    # 同 task 锁下第一次先真正折叠，第二次落到已折叠的段上。
    t1 = bo.launch_background_observe(state, ctx, boundary="interrupt")
    t2 = bo.launch_background_observe(state, ctx, boundary="interrupt")
    await asyncio.gather(t1, t2)

    # 1) 第一次真的跑了、折了段：该段 active LLM_RESPONSE 计数归零
    n_raw = await ctx.memory.count_recent(
        state.scope, [MemoryEventType.LLM_RESPONSE], ctx.provider_ctx,
    )
    assert n_raw == 0, "第一次 observe 应已折叠该段（LLM_RESPONSE 被 supersede）"

    # 2) 第二次被护栏跳过：run_observe_react 只跑了一次
    assert len(calls) == 1, (
        f"护栏应跳过对已折叠段的第二次 observe（run_observe_react 只应跑 1 次），实得 {len(calls)} 次"
    )

    # 3) 两次启动都发了 TASK_RECAP_DONE（finally 无条件发，护栏只跳过 try 里的工作体）
    done_events = [e for e in ctx.event_bus.emitted if e.type == EventType.TASK_RECAP_DONE]
    assert len(done_events) == 2, f"两次启动都应发 TASK_RECAP_DONE，实得 {len(done_events)} 条"

    # 4) await_pending_background_observe 对被跳过的第二个任务也能正常收尾（不挂起/不报错）
    await asyncio.wait_for(
        bo.await_pending_background_observe(state.task.id), timeout=1.0,
    )
