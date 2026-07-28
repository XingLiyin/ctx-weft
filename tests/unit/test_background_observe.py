"""Tests for background_observe module (Task 4 + Task 6)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import ctx_weft.core.loop.steps.background_observe as bo
import ctx_weft.core.loop.steps.observe as _obs_mod
from ctx_weft.core.orchestrator.control_capability import (
    BACKGROUND_PROCESS_REPORT_NAME,
    ControlResult,
)
from ctx_weft.protocols import MemoryEventType


@pytest.fixture(autouse=True)
def _clear_module_state():
    """Clear module-level dicts between tests to avoid cross-test lock/event-loop pollution."""
    bo._task_locks.clear()
    bo._task_pending.clear()
    bo._orphan_tasks.clear()
    bo._close_report.clear()
    bo._close_synth.clear()
    yield
    bo._task_locks.clear()
    bo._task_pending.clear()
    bo._orphan_tasks.clear()
    bo._close_report.clear()
    bo._close_synth.clear()


# ── helpers shared by new ReAct-based tests ───────────────────────────────────


def _make_tool_call_chunk(name: str, call_id: str = "tc1"):
    return SimpleNamespace(
        kind="tool_call",
        tool_call=SimpleNamespace(id=call_id, name=name, arguments={"task_process_report": "段总结X"}),
        text="",
        usage=None,
    )


def _make_token_chunk(text: str):
    return SimpleNamespace(kind="token", text=text, tool_call=None, usage=None)


def _make_usage_chunk():
    from ctx_weft.protocols import LLMUsage
    return SimpleNamespace(
        kind="usage",
        usage=LLMUsage(prompt_tokens=10, completion_tokens=5),
        tool_call=None,
        text="",
    )


def _neutralize_refold_guard(monkeypatch, ctx) -> None:
    """让重折幂等护栏恒放行（等价旧 count_recent→1 mock）。

    新护栏走 load_view 数 assistant 回合；此处 mock 恒返回一条 assistant conversation
    turn。流式已 mock，prompt 内容无关紧要。
    """
    from datetime import datetime, timezone
    from ctx_weft.protocols import MemoryEventType as MT
    from ctx_weft.protocols import MemoryKind, MemoryScope, MemoryRecord

    async def fake_load_view(address, scope, pctx, kinds=None):
        return [MemoryRecord(
            id="guard", type=MT.LLM_RESPONSE, content="x",
            timestamp=datetime.now(timezone.utc), role="assistant",
            kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK,
        )]

    monkeypatch.setattr(ctx.memory, "load_view", fake_load_view)


class _FakeGateway:
    """Gateway that returns ControlResult(content=report_text) for collect_process_report."""

    def __init__(self, report_text: str = "段总结X"):
        self._report_text = report_text

    async def invoke(self, *, tool_name, arguments, state, ctx, tool_call_id):
        return ControlResult(content=self._report_text)


async def _fake_stream_collect_process_report(ctx, state, request):
    """Fake LLM that calls collect_process_report once."""
    yield _make_token_chunk("thinking...")
    yield _make_tool_call_chunk(BACKGROUND_PROCESS_REPORT_NAME)
    yield _make_usage_chunk()


def _async_const(value: str):
    """Return an async function that always returns value (legacy compat for old tests)."""
    async def _impl(s, c):
        return value
    return _impl


# ── legacy tests (rewritten to use ReAct-based mock) ─────────────────────────


@pytest.mark.asyncio
async def test_launch_produces_summary_and_folds(monkeypatch, fake_state_ctx):
    state, ctx = fake_state_ctx  # task 层有 [UP, llm, tool]
    # augment ctx with capability_gateway and max_turns_per_observe
    ctx.capability_gateway = _FakeGateway("段摘要文本")
    state.agent.loop_config = SimpleNamespace(compact_keep_last=2, max_turns_per_observe=3)
    state.session = SimpleNamespace(id="s1", tenant_id="default", token_used=0)
    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", _fake_stream_collect_process_report)

    t = bo.launch_background_observe(state, ctx, boundary="interrupt")
    await t
    # apply_compact 被调、产出 TASK_COMPACT_SUMMARY、UP 保留
    from ctx_weft.protocols import MemoryEventType as MT
    recs = await ctx.memory.recall_recent(
        state.scope, [MT.USER_PROMPT, MT.LLM_RESPONSE, MT.TOOL_RESULT, MT.TASK_COMPACT_SUMMARY],
        100, ctx.provider_ctx)
    types = {r.type for r in recs}
    assert MT.TASK_COMPACT_SUMMARY in types
    summary = next(r for r in recs if r.type == MT.TASK_COMPACT_SUMMARY)
    assert summary.role == "assistant", "后台 observe 真实 apply_compact 应产 assistant 段摘要"
    assert MT.USER_PROMPT in types
    assert MT.LLM_RESPONSE not in types


@pytest.mark.asyncio
async def test_serialized_per_task(monkeypatch, fake_state_ctx):
    state, ctx = fake_state_ctx
    ctx.capability_gateway = _FakeGateway("x")
    state.agent.loop_config = SimpleNamespace(compact_keep_last=2, max_turns_per_observe=3)
    state.session = SimpleNamespace(id="s1", tenant_id="default", token_used=0)

    order = []

    async def slow_stream(c, s, req):
        order.append("start")
        await asyncio.sleep(0.01)
        yield _make_tool_call_chunk(BACKGROUND_PROCESS_REPORT_NAME)
        yield _make_usage_chunk()
        order.append("end")

    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", slow_stream)
    _neutralize_refold_guard(monkeypatch, ctx)
    t1 = bo.launch_background_observe(state, ctx, boundary="interrupt")
    t2 = bo.launch_background_observe(state, ctx, boundary="interrupt")
    await asyncio.gather(t1, t2)
    assert order == ["start", "end", "start", "end"]  # 串行，不交错


@pytest.mark.asyncio
async def test_await_pending_waits_for_latest_when_two_launched(monkeypatch, fake_state_ctx):
    """Regression: when two observes launch for the same task_id, the first completing
    must NOT clear _task_pending — await_pending_background_observe must wait for the
    second (latest) observe to finish.
    """
    state, ctx = fake_state_ctx
    ctx.capability_gateway = _FakeGateway("x")
    state.agent.loop_config = SimpleNamespace(compact_keep_last=2, max_turns_per_observe=3)
    state.session = SimpleNamespace(id="s1", tenant_id="default", token_used=0)

    call_count = 0
    second_done = False

    async def vary_speed(c, s, req):
        nonlocal call_count, second_done
        call_count += 1
        current = call_count
        if current == 1:
            await asyncio.sleep(0)
        else:
            await asyncio.sleep(0.05)
            second_done = True
        yield _make_tool_call_chunk(BACKGROUND_PROCESS_REPORT_NAME)
        yield _make_usage_chunk()

    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", vary_speed)
    _neutralize_refold_guard(monkeypatch, ctx)

    t1 = bo.launch_background_observe(state, ctx, boundary="interrupt")
    t2 = bo.launch_background_observe(state, ctx, boundary="interrupt")

    await asyncio.sleep(0.01)

    await bo.await_pending_background_observe(state.task.id)

    assert second_done, (
        "await_pending_background_observe returned before the second (latest) observe "
        "finished — _task_pending was incorrectly cleared by the first task's callback"
    )
    await asyncio.gather(t1, t2)


@pytest.mark.asyncio
async def test_failure_swallowed(monkeypatch, fake_state_ctx):
    state, ctx = fake_state_ctx
    ctx.capability_gateway = _FakeGateway("x")
    state.agent.loop_config = SimpleNamespace(compact_keep_last=2, max_turns_per_observe=3)
    state.session = SimpleNamespace(id="s1", tenant_id="default", token_used=0)

    async def boom(c, s, req):
        raise RuntimeError("llm down")
        yield  # make it an async generator

    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", boom)
    t = bo.launch_background_observe(state, ctx, boundary="interrupt")
    await t  # 不抛
    assert t.exception() is None


# ── Task 6: new tests ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_background_interrupt_writes_segment_summary(monkeypatch, fake_state_ctx):
    """boundary="interrupt": run 后 task 层有一条 TASK_COMPACT_SUMMARY(role=assistant)，内容=工具产出"""
    state, ctx = fake_state_ctx
    ctx.capability_gateway = _FakeGateway("段总结X")
    state.agent.loop_config = SimpleNamespace(compact_keep_last=2, max_turns_per_observe=3)
    state.session = SimpleNamespace(id="s1", tenant_id="default", token_used=0)
    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", _fake_stream_collect_process_report)

    t = bo.launch_background_observe(state, ctx, boundary="interrupt")
    await t

    from ctx_weft.protocols import MemoryEventType as MT
    recs = await ctx.memory.recall_recent(
        state.scope, [MT.TASK_COMPACT_SUMMARY], 100, ctx.provider_ctx)
    assert len(recs) == 1 and recs[0].role == "assistant"


@pytest.mark.asyncio
async def test_background_close_no_memory_writes_slot(monkeypatch, fake_state_ctx):
    """boundary="finish": run 后无 TASK_COMPACT_SUMMARY；结果落 _close_report 槽"""
    state, ctx = fake_state_ctx
    ctx.capability_gateway = _FakeGateway("段总结X")
    state.agent.loop_config = SimpleNamespace(compact_keep_last=2, max_turns_per_observe=3)
    state.session = SimpleNamespace(id="s1", tenant_id="default", token_used=0)
    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", _fake_stream_collect_process_report)

    t = bo.launch_background_observe(state, ctx, boundary="finish")
    await t

    from ctx_weft.protocols import MemoryEventType as MT
    recs = await ctx.memory.recall_recent(
        state.scope, [MT.TASK_COMPACT_SUMMARY], 100, ctx.provider_ctx)
    assert recs == []
    result = bo.pop_close_report(state.task.id)
    assert result is not None


@pytest.mark.asyncio
async def test_two_plain_text_observes_accumulate_both_summaries(monkeypatch, fake_state_ctx):
    """Regression: two plain_text-boundary observes on the same task must each leave their
    OWN TASK_COMPACT_SUMMARY capsule, ordered [UP1, S1, UP2, S2].

    Bug: plain_text apply_compact protected only USER_PROMPT (not TASK_COMPACT_SUMMARY), so the
    2nd observe superseded the 1st's summary (S1 lost) and anchored S2 before UP2 (S2 landed in
    S1's slot between the two user prompts) — exactly the observed symptom.
    """
    from datetime import UTC, datetime

    from ctx_weft.protocols import MemoryEvent
    from ctx_weft.protocols import MemoryEventType as MT

    state, ctx = fake_state_ctx  # task 层预置 [UP1, LLM, TOOL]
    state.agent.loop_config = SimpleNamespace(compact_keep_last=2, max_turns_per_observe=3)
    state.session = SimpleNamespace(id="s1", tenant_id="default", token_used=0)
    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", _fake_stream_collect_process_report)

    class _CountingGateway:
        """Distinct report per observe so we can tell S1 from S2."""

        def __init__(self):
            self.n = 0

        async def invoke(self, *, tool_name, arguments, state, ctx, tool_call_id):
            self.n += 1
            return ControlResult(content=f"S{self.n}")

    ctx.capability_gateway = _CountingGateway()

    # ── observe1：折掉预置 [LLM, TOOL]，留 UP1，产 S1 ──
    await bo.launch_background_observe(state, ctx, boundary="plain_text")

    s1 = await ctx.memory.recall_recent(state.scope, [MT.TASK_COMPACT_SUMMARY], 100, ctx.provider_ctx)
    assert len(s1) == 1 and s1[0].content == "S1"

    # ── 用户回复(UP2) + 第二轮纯文本(LLM reply2)；用真实 now 保证时序单调 ──
    await asyncio.sleep(0.005)
    await ctx.memory.ingest(MemoryEvent(
        type=MT.USER_PROMPT, address=state.scope, content="user2",
        timestamp=datetime.now(UTC), role="user"), ctx.provider_ctx)
    await asyncio.sleep(0.005)
    await ctx.memory.ingest(MemoryEvent(
        type=MT.LLM_RESPONSE, address=state.scope, content="reply2",
        timestamp=datetime.now(UTC), role="assistant"), ctx.provider_ctx)
    await asyncio.sleep(0.005)

    # ── observe2：折掉 reply2，留 UP1/UP2/S1，产 S2 ──
    await bo.launch_background_observe(state, ctx, boundary="plain_text")

    recs = await ctx.memory.recall_recent(
        state.scope, [MT.USER_PROMPT, MT.LLM_RESPONSE, MT.TOOL_RESULT, MT.TASK_COMPACT_SUMMARY],
        100, ctx.provider_ctx)
    chrono = list(reversed(recs))  # recall 是 newest-first
    summaries = [r for r in chrono if r.type == MT.TASK_COMPACT_SUMMARY]

    assert len(summaries) == 2, f"两段 plain_text 胶囊都应存活；实得 {[s.content for s in summaries]}"
    assert [s.content for s in summaries] == ["S1", "S2"]
    assert [r.type for r in chrono] == [
        MT.USER_PROMPT, MT.TASK_COMPACT_SUMMARY, MT.USER_PROMPT, MT.TASK_COMPACT_SUMMARY,
    ], "顺序应为 [UP1, S1, UP2, S2]"


@pytest.mark.asyncio
async def test_plain_text_reply_falls_back_to_last_text(monkeypatch, fake_state_ctx):
    """observer 全程纯文本、始终不调 collect_process_report：不得丢弃其文本换占位符，
    应把最后一轮纯文本当作段摘要写入 TASK_COMPACT_SUMMARY。"""
    state, ctx = fake_state_ctx
    ctx.capability_gateway = _FakeGateway("unused")
    state.agent.loop_config = SimpleNamespace(compact_keep_last=2, max_turns_per_observe=2)
    state.session = SimpleNamespace(id="s1", tenant_id="default", token_used=0)

    async def _text_only(c, s, req):
        yield _make_token_chunk("纯文本复述")
        yield _make_usage_chunk()

    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", _text_only)
    await bo.launch_background_observe(state, ctx, boundary="plain_text")

    recs = await ctx.memory.recall_recent(
        state.scope, [MemoryEventType.TASK_COMPACT_SUMMARY], 100, ctx.provider_ctx)
    assert len(recs) == 1
    assert recs[0].content == "纯文本复述", \
        f"应采纳 observer 的纯文本复述，实得 {recs[0].content!r}"


@pytest.mark.asyncio
async def test_no_usable_report_keeps_raw(monkeypatch, fake_state_ctx):
    """observer 既没调工具也没产出任何文本：不写占位摘要、不折叠——段保 raw
    （与异常路径同语义）。"""
    state, ctx = fake_state_ctx  # task 层预置 [UP, LLM, TOOL]
    ctx.capability_gateway = _FakeGateway("unused")
    state.agent.loop_config = SimpleNamespace(compact_keep_last=2, max_turns_per_observe=2)
    state.session = SimpleNamespace(id="s1", tenant_id="default", token_used=0)

    async def _empty(c, s, req):
        yield _make_usage_chunk()

    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", _empty)
    await bo.launch_background_observe(state, ctx, boundary="plain_text")

    from ctx_weft.protocols import MemoryEventType as MT
    recs = await ctx.memory.recall_recent(
        state.scope, [MT.USER_PROMPT, MT.LLM_RESPONSE, MT.TOOL_RESULT, MT.TASK_COMPACT_SUMMARY],
        100, ctx.provider_ctx)
    types = [r.type for r in recs]
    assert MT.TASK_COMPACT_SUMMARY not in types, "无可用报告不得写占位摘要"
    assert MT.LLM_RESPONSE in types and MT.TOOL_RESULT in types, "raw 必须保留"


@pytest.mark.asyncio
async def test_no_usable_report_close_does_not_fill_slot(monkeypatch, fake_state_ctx):
    """close 边界、无 synth 登记、无可用报告：不得把占位符塞进 _close_report 槽
    （否则 finalize 会用它合成 finish 对）。"""
    state, ctx = fake_state_ctx
    ctx.capability_gateway = _FakeGateway("unused")
    state.agent.loop_config = SimpleNamespace(compact_keep_last=2, max_turns_per_observe=2)
    state.session = SimpleNamespace(id="s1", tenant_id="default", token_used=0)

    async def _empty(c, s, req):
        yield _make_usage_chunk()

    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", _empty)
    await bo.launch_background_observe(state, ctx, boundary="finish")

    assert bo.pop_close_report(state.task.id) is None, "无可用报告不得占用 close_report 槽"


@pytest.mark.asyncio
async def test_no_usable_report_close_preserves_existing_finish_pair(monkeypatch, fake_state_ctx):
    """close 边界、synth 已登记、无可用报告：finalize 合成的 finish 对须保持原样，
    不得被占位符重写；synth 登记要弹掉防泄漏。"""
    from datetime import UTC, datetime

    from ctx_weft.protocols import MemoryEvent
    from ctx_weft.protocols import MemoryEventType as MT

    state, ctx = fake_state_ctx
    ctx.capability_gateway = _FakeGateway("unused")
    state.agent.loop_config = SimpleNamespace(compact_keep_last=2, max_turns_per_observe=2)
    state.session = SimpleNamespace(id="s1", tenant_id="default", token_used=0)

    # 预置 finalize 合成的 finish 对（assistant + tool，tool_call_id=tc9）
    ts = datetime.now(UTC)
    await ctx.memory.ingest(MemoryEvent(
        type=MT.AGENT_CONVERSATION_TURN, address=state.scope, content="finalize recap",
        timestamp=ts, role="assistant",
        metadata={"origin_task_id": state.task.id,
                  "tool_calls": [{"id": "tc9", "name": "control__finish_task", "input": {}}]},
    ), ctx.provider_ctx)
    await ctx.memory.ingest(MemoryEvent(
        type=MT.AGENT_CONVERSATION_TURN, address=state.scope, content="finalize summary",
        timestamp=ts, role="tool",
        metadata={"origin_task_id": state.task.id, "tool_call_id": "tc9"},
    ), ctx.provider_ctx)

    async def _empty(c, s, req):
        yield _make_usage_chunk()

    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", _empty)
    bo.register_close_synth(state.task.id, "tc9", state.scope, "success")
    await bo.launch_background_observe(state, ctx, boundary="finish")

    turns = await ctx.memory.recall_recent(
        state.scope, [MT.AGENT_CONVERSATION_TURN], 100, ctx.provider_ctx)
    contents = sorted(r.content for r in turns)
    assert contents == ["finalize recap", "finalize summary"], \
        f"finish 对不得被占位符重写，实得 {contents}"
    assert bo._close_synth == {}, "close_synth 登记须被弹掉防泄漏"


@pytest.mark.asyncio
async def test_background_zero_state_pollution(monkeypatch, fake_state_ctx):
    """跑 background observe 前后，task.status/actor_done/observer_outcome 不变"""
    state, ctx = fake_state_ctx
    ctx.capability_gateway = _FakeGateway("段总结X")
    state.agent.loop_config = SimpleNamespace(compact_keep_last=2, max_turns_per_observe=3)
    state.session = SimpleNamespace(id="s1", tenant_id="default", token_used=0)
    # Add the fields if not present
    if not hasattr(state.task, "status"):
        state.task.status = "RUNNING"
    if not hasattr(state.task, "actor_done"):
        state.task.actor_done = False
    if not hasattr(state.task, "observer_outcome"):
        state.task.observer_outcome = None
    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", _fake_stream_collect_process_report)

    before = (state.task.status, state.task.actor_done, state.task.observer_outcome)
    t = bo.launch_background_observe(state, ctx, boundary="finish")
    await t
    assert (state.task.status, state.task.actor_done, state.task.observer_outcome) == before


# ── dispatch 边界（spec 2026-07-16）：非 close 分支契约特征测试 ────────────────


@pytest.mark.asyncio
async def test_dispatch_boundary_folds_segment(monkeypatch, fake_state_ctx):
    """boundary="dispatch"：走非 close 分支写段摘要、折派发前 raw、UP 保留；
    SuspendStep 的挂起摘要（OBSERVER_SUMMARY，AGENT 层半僵尸类型、不进装配）
    层级隔离——TASK 层折叠不动它（spec §1 事实修正）。"""
    from datetime import UTC, datetime

    from ctx_weft.protocols import MemoryEvent
    from ctx_weft.protocols import MemoryEventType as MT

    state, ctx = fake_state_ctx  # task 层预置 [UP, LLM, TOOL]
    ctx.capability_gateway = _FakeGateway("dispatch段摘要")
    state.agent.loop_config = SimpleNamespace(compact_keep_last=2, max_turns_per_observe=3)
    state.session = SimpleNamespace(id="s1", tenant_id="default", token_used=0)
    # 模拟 SuspendStep 已写的挂起摘要（AGENT 层）
    await ctx.memory.ingest(MemoryEvent(
        type=MT.OBSERVER_SUMMARY, address=state.scope,
        content="Delegated to sub-task(s): 'x'. Awaiting completion.",
        timestamp=datetime.now(UTC), role="assistant",
        metadata={"task_id": state.task.id, "outcome": "suspended"}), ctx.provider_ctx)
    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", _fake_stream_collect_process_report)

    await bo.launch_background_observe(state, ctx, boundary="dispatch")

    recs = await ctx.memory.recall_recent(
        state.scope,
        [MT.USER_PROMPT, MT.LLM_RESPONSE, MT.TOOL_RESULT, MT.TASK_COMPACT_SUMMARY],
        100, ctx.provider_ctx)
    types = {r.type for r in recs}
    assert MT.TASK_COMPACT_SUMMARY in types, "dispatch 边界必须写段摘要"
    assert MT.USER_PROMPT in types, "UP 受 protect_types 保护"
    assert MT.LLM_RESPONSE not in types, "派发前 raw 必须折掉"
    assert MT.TOOL_RESULT not in types, "TOOL_RESULT 同属段 raw，必须折掉"
    # 层级隔离：AGENT 层的挂起摘要不受 TASK 层折叠影响。单独召回——遵循
    # spec/06 §8 同层约定（layer_for_types 混层抛错；in-memory provider 过渡期宽容，
    # 但测试不依赖这种宽容）。
    obs = await ctx.memory.recall_recent(
        state.scope, [MT.OBSERVER_SUMMARY], 100, ctx.provider_ctx)
    assert len(obs) == 1, "OBSERVER_SUMMARY 层级隔离，折叠后应原样留存"


@pytest.mark.asyncio
async def test_dispatch_boundary_short_segment_kept_raw(monkeypatch, fake_state_ctx):
    """boundary="dispatch"：段 raw 低于 short_segment_token_threshold → 免折保 raw。"""
    state, ctx = fake_state_ctx
    ctx.capability_gateway = _FakeGateway("unused")
    state.agent.loop_config = SimpleNamespace(
        compact_keep_last=2, max_turns_per_observe=3,
        short_segment_token_threshold=100_000)  # 远超预置 raw → 门必命中
    state.session = SimpleNamespace(id="s1", tenant_id="default", token_used=0)
    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", _fake_stream_collect_process_report)

    await bo.launch_background_observe(state, ctx, boundary="dispatch")

    from ctx_weft.protocols import MemoryEventType as MT
    recs = await ctx.memory.recall_recent(
        state.scope, [MT.LLM_RESPONSE, MT.TASK_COMPACT_SUMMARY], 100, ctx.provider_ctx)
    types = [r.type for r in recs]
    assert MT.TASK_COMPACT_SUMMARY not in types, "短段必须免折"
    assert MT.LLM_RESPONSE in types, "raw 必须保留"


@pytest.mark.asyncio
async def test_dispatch_boundary_refold_guard_skips(monkeypatch, fake_state_ctx):
    """boundary="dispatch"：段内无 active LLM_RESPONSE（恢复重跑已折过）→ 幂等护栏跳过。"""
    state, ctx = fake_state_ctx
    ctx.capability_gateway = _FakeGateway("unused")
    state.agent.loop_config = SimpleNamespace(compact_keep_last=2, max_turns_per_observe=3)
    state.session = SimpleNamespace(id="s1", tenant_id="default", token_used=0)

    # 真实"已折过"状态：supersede 段内 raw（新护栏走 load_view 数 assistant 回合，
    # 不再可经 count_recent mock 控制）
    from ctx_weft.protocols import MemoryEventType as _MT
    _raws = await ctx.memory.recall_recent(
        state.scope, [_MT.LLM_RESPONSE], 100, ctx.provider_ctx)
    await ctx.memory.supersede([r.id for r in _raws], ctx.provider_ctx)
    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", _fake_stream_collect_process_report)

    await bo.launch_background_observe(state, ctx, boundary="dispatch")

    from ctx_weft.protocols import MemoryEventType as MT
    recs = await ctx.memory.recall_recent(
        state.scope, [MT.TASK_COMPACT_SUMMARY], 100, ctx.provider_ctx)
    assert recs == [], "护栏命中不得产冗余胶囊"


@pytest.mark.asyncio
async def test_dispatch_recap_completes_benignly_after_cancel(monkeypatch, fake_state_ctx):
    """SUSPENDED 期间被取消：在途 dispatch recap 事后完成——不抛错、照常折段、
    不回写 task 状态（spec 2026-07-16 §2 并发边界：接受，不加同步）。"""
    state, ctx = fake_state_ctx
    ctx.capability_gateway = _FakeGateway("段总结X")
    state.agent.loop_config = SimpleNamespace(compact_keep_last=2, max_turns_per_observe=3)
    state.session = SimpleNamespace(id="s1", tenant_id="default", token_used=0)

    async def slow_stream(c, s, req):
        await asyncio.sleep(0.02)  # 给取消留出交叉窗口
        yield _make_tool_call_chunk(BACKGROUND_PROCESS_REPORT_NAME)
        yield _make_usage_chunk()

    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", slow_stream)

    t = bo.launch_background_observe(state, ctx, boundary="dispatch")
    state.task.status = "CANCELED"  # recap 在跑时取消坐实
    await t

    assert t.exception() is None, "取消交叉不得让 recap 抛错"
    assert state.task.status == "CANCELED", "recap 不得回写 task 状态"
    from ctx_weft.protocols import MemoryEventType as MT
    recs = await ctx.memory.recall_recent(
        state.scope, [MT.TASK_COMPACT_SUMMARY], 100, ctx.provider_ctx)
    assert len(recs) == 1, "折的是取消前已存在的 raw——照常成段摘要（良性）"


@pytest.mark.asyncio
async def test_segment_fold_failure_keeps_raw_and_does_not_raise(
        monkeypatch, caplog, fake_state_ctx):
    """段折运行时故障（v2：segment_fold 抛异常）：段保 raw 降级、不抛（fire-and-forget）。

    旧的 apply_compact TypeError 协议错配特判随策展上移消亡——segment_fold 是框架内函数，
    签名错配不再是 provider 运行时风险；只保留通用故障降级契约。
    """
    import logging

    state, ctx = fake_state_ctx  # task 层预置 [UP, LLM, TOOL]
    ctx.capability_gateway = _FakeGateway("段总结X")
    state.agent.loop_config = SimpleNamespace(compact_keep_last=2, max_turns_per_observe=3)
    state.session = SimpleNamespace(id="s1", tenant_id="default", token_used=0)
    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", _fake_stream_collect_process_report)

    async def boom(*args, **kwargs):
        raise RuntimeError("fold backend down")

    monkeypatch.setattr("ctx_weft.core.loop.steps.segment_fold.segment_fold", boom)

    with caplog.at_level(logging.ERROR, logger="ctx_weft.core.loop.steps.background_observe"):
        t = bo.launch_background_observe(state, ctx, boundary="plain_text")
        await t

    assert t.exception() is None, "运行时故障不得抛出（fire-and-forget 降级）"
    from ctx_weft.protocols import MemoryEventType as MT
    recs = await ctx.memory.recall_recent(
        state.scope, [MT.TASK_COMPACT_SUMMARY], 100, ctx.provider_ctx)
    assert recs == [], "故障不得写摘要"
    raw = await ctx.memory.recall_recent(
        state.scope, [MT.LLM_RESPONSE], 100, ctx.provider_ctx)
    assert raw, "段必须保 raw"
