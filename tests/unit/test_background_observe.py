"""Tests for background_observe module (Task 4 + Task 6)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ctx_weft.core.loop.background import boundaries
from ctx_weft.core.loop.background import recap as recap_mod
from ctx_weft.core.loop.background import runner
from ctx_weft.core.loop import finish_pair as fp
from ctx_weft.core.capabilities.control_tools import ControlMetaKey as K

import pytest

import ctx_weft.core.loop.observing as _obs_mod
from ctx_weft.core.capabilities.control_tools import (
    COLLECT_PROCESS_REPORT_NAME,
    REPORT_TASK_OUTCOME_NAME,
    ControlResult,
)
from ctx_weft.protocols import MemoryEventType


@pytest.fixture(autouse=True)
def _clear_module_state():
    """Clear module-level dicts between tests to avoid cross-test lock/event-loop pollution."""
    recap_mod._task_locks.clear()
    runner._task_pending.clear()
    runner._orphan_tasks.clear()
    fp._close_report.clear()
    fp._close_synth.clear()
    yield
    recap_mod._task_locks.clear()
    runner._task_pending.clear()
    runner._orphan_tasks.clear()
    fp._close_report.clear()
    fp._close_synth.clear()


# ── helpers shared by new ReAct-based tests ───────────────────────────────────


def _make_tool_call_chunk(name: str, call_id: str = "tc1"):
    return SimpleNamespace(
        kind="tool_call",
        tool_call=SimpleNamespace(id=call_id, name=name, arguments={"act_recap": "段总结X"}),
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
        # 返回形态对齐生产 InvocationResult（含 is_error）——run_observe_react
        # 读该字段判定 terminal 失败，缺字段会 AttributeError。
        # 对齐 `report_task_outcome` 的真实返回：content 是给 LLM 看的话术，干净的
        # recap 在 metadata 里（2026-09-22 起后台与前台同一个 terminal tool）。
        return SimpleNamespace(
            content=f"Assessment recorded: outcome=success. {self._report_text}",
            is_error=False,
            metadata={K.OBSERVER_OUTCOME: "success",
                      K.OBSERVER_ACT_RECAP: self._report_text,
                      K.OBSERVER_TASK_SUMMARY: ""},
        )


async def _fake_stream_collect_process_report(ctx, state, request):
    """Fake LLM that calls collect_process_report once —— **只摘要那一档**的 terminal tool。"""
    yield _make_token_chunk("thinking...")
    yield _make_tool_call_chunk(COLLECT_PROCESS_REPORT_NAME)
    yield _make_usage_chunk()


async def _fake_stream_report_task_outcome(ctx, state, request):
    """判定那一档的 terminal tool（2026-09-28 起两档是两个工具）。

    名字必须对得上 `run_observe_react` 的 `terminal_tool_name`，否则那个循环不会终止——
    它会一直跑到 max_rounds，terminal_result 恒为 None，报告只能从 last_text 兜底取。
    """
    yield _make_token_chunk("judging...")
    yield _make_tool_call_chunk(REPORT_TASK_OUTCOME_NAME)
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

    t = runner.launch_recap(state, ctx, boundary="interrupt")
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
        yield _make_tool_call_chunk(COLLECT_PROCESS_REPORT_NAME)
        yield _make_usage_chunk()
        order.append("end")

    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", slow_stream)
    _neutralize_refold_guard(monkeypatch, ctx)
    t1 = runner.launch_recap(state, ctx, boundary="interrupt")
    t2 = runner.launch_recap(state, ctx, boundary="interrupt")
    await asyncio.gather(t1, t2)
    assert order == ["start", "end", "start", "end"]  # 串行，不交错


@pytest.mark.asyncio
async def test_await_pending_waits_for_latest_when_two_launched(monkeypatch, fake_state_ctx):
    """Regression: when two observes launch for the same task_id, the first completing
    must NOT clear _task_pending — await_pending_recap must wait for the
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
        yield _make_tool_call_chunk(COLLECT_PROCESS_REPORT_NAME)
        yield _make_usage_chunk()

    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", vary_speed)
    _neutralize_refold_guard(monkeypatch, ctx)

    t1 = runner.launch_recap(state, ctx, boundary="interrupt")
    t2 = runner.launch_recap(state, ctx, boundary="interrupt")

    await asyncio.sleep(0.01)

    await runner.await_pending_recap(state.task.id)

    assert second_done, (
        "await_pending_recap returned before the second (latest) observe "
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
    t = runner.launch_recap(state, ctx, boundary="interrupt")
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

    t = runner.launch_recap(state, ctx, boundary="interrupt")
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

    t = runner.launch_recap(state, ctx, boundary="finish")
    await t

    from ctx_weft.protocols import MemoryEventType as MT
    recs = await ctx.memory.recall_recent(
        state.scope, [MT.TASK_COMPACT_SUMMARY], 100, ctx.provider_ctx)
    assert recs == []
    result = fp.pop_close_report(state.task.id)
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
    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", _fake_stream_report_task_outcome)

    class _CountingGateway:
        """Distinct report per observe so we can tell S1 from S2."""

        def __init__(self):
            self.n = 0

        async def invoke(self, *, tool_name, arguments, state, ctx, tool_call_id):
            self.n += 1
            # 返回形态对齐生产 InvocationResult（含 is_error）——run_observe_react
            # 读该字段判定 terminal 失败，缺字段会 AttributeError。
            return SimpleNamespace(
                content=f"Assessment recorded: outcome=success. S{self.n}",
                is_error=False,
                metadata={K.OBSERVER_OUTCOME: "success",
                          K.OBSERVER_ACT_RECAP: f"S{self.n}",
                          K.OBSERVER_TASK_SUMMARY: ""},
            )

    ctx.capability_gateway = _CountingGateway()

    # ── observe1：折掉预置 [LLM, TOOL]，留 UP1，产 S1 ──
    await runner.launch_recap(state, ctx, boundary="plain_text")

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
    await runner.launch_recap(state, ctx, boundary="plain_text")

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
    await runner.launch_recap(state, ctx, boundary="plain_text")

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
    await runner.launch_recap(state, ctx, boundary="plain_text")

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
    await runner.launch_recap(state, ctx, boundary="finish")

    assert fp.pop_close_report(state.task.id) is None, "无可用报告不得占用 close_report 槽"


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
    fp.register_close_synth(state.task.id, "tc9", state.scope, "success")
    await runner.launch_recap(state, ctx, boundary="finish")

    turns = await ctx.memory.recall_recent(
        state.scope, [MT.AGENT_CONVERSATION_TURN], 100, ctx.provider_ctx)
    contents = sorted(r.content for r in turns)
    assert contents == ["finalize recap", "finalize summary"], \
        f"finish 对不得被占位符重写，实得 {contents}"
    assert fp._close_synth == {}, "close_synth 登记须被弹掉防泄漏"


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
    t = runner.launch_recap(state, ctx, boundary="finish")
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

    await runner.launch_recap(state, ctx, boundary="dispatch")

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

    await runner.launch_recap(state, ctx, boundary="dispatch")

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

    await runner.launch_recap(state, ctx, boundary="dispatch")

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
        yield _make_tool_call_chunk(COLLECT_PROCESS_REPORT_NAME)
        yield _make_usage_chunk()

    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", slow_stream)

    t = runner.launch_recap(state, ctx, boundary="dispatch")
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
    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", _fake_stream_report_task_outcome)

    async def boom(*args, **kwargs):
        raise RuntimeError("fold backend down")

    monkeypatch.setattr("ctx_weft.core.loop.fold.segment_fold", boom)

    with caplog.at_level(logging.ERROR, logger="ctx_weft.core.loop.background"):
        t = runner.launch_recap(state, ctx, boundary="plain_text")
        await t

    assert t.exception() is None, "运行时故障不得抛出（fire-and-forget 降级）"
    from ctx_weft.protocols import MemoryEventType as MT
    recs = await ctx.memory.recall_recent(
        state.scope, [MT.TASK_COMPACT_SUMMARY], 100, ctx.provider_ctx)
    assert recs == [], "故障不得写摘要"
    raw = await ctx.memory.recall_recent(
        state.scope, [MT.LLM_RESPONSE], 100, ctx.provider_ctx)
    assert raw, "段必须保 raw"


# ── build_finish_slots：折叠产出必须声明幸存占位的 ref（GC mark 判据）───────────


@pytest.mark.asyncio
async def test_replace_finish_report_declares_surviving_placeholder_refs():
    """`_replace_finish_report` 用 `memory.fold` 把旧 finish 对 supersede 掉、换上新槽。
    若新槽正文（act_recap / task_summary）逐字带着 L0.5 占位向前走，旧的供体记录正被
    这次 fold 干掉——占位的活引用只剩新槽记录能扛，`build_finish_slots` 必须替两个调用方
    （这里的 fold 路径、以及 finalize._synthesize_dispatch_pair 的裸 ingest 路径）都声明。
    """
    from datetime import datetime, timezone

    from ctx_weft.core.utils.content import collect_blob_refs
    from ctx_weft.core.media.refs import encode_image_placeholder
    from ctx_weft.protocols import (
        MemoryAddress, MemoryEvent, MemoryKind, MemoryScope, ProviderContext,
    )
    from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider

    ref = "blob:" + "e" * 64
    placeholder = encode_image_placeholder(ref, "image/png")

    mem = InMemoryMemoryProvider()
    pctx = ProviderContext(session_id="s1", tenant_id="default", task_id="t1", agent_id="a1")
    scope = MemoryAddress(session_id="s1", task_id="t1", agent_id="a1")
    ts = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    tool_call_id = "tc1"

    # 占位 finish 对（两槽）：assistant 挂 tool_calls，tool 是 process report——都带着
    # 供体占位，即将被本次 fold 全部 supersede。
    await mem.ingest(MemoryEvent(
        kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.AGENT, address=scope,
        content=f"placeholder recap {placeholder}", timestamp=ts, role="assistant",
        metadata={"origin_task_id": "t1", "parent_task_id": None,
                  "tool_calls": [{"id": tool_call_id, "name": "control:finish_task", "input": {}}]},
    ), pctx)
    await mem.ingest(MemoryEvent(
        kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.AGENT, address=scope,
        content="placeholder report", timestamp=ts, role="tool",
        metadata={"origin_task_id": "t1", "tool_call_id": tool_call_id},
    ), pctx)

    await fp.replace_finish_report(
        mem, pctx, scope, "t1", tool_call_id,
        act_recap=f"did the thing {placeholder}", task_summary="summary body",
        outcome="done", title="task title",
    )

    agent_addr = MemoryAddress(session_id="s1", agent_id="a1")
    view = await mem.load_view(agent_addr, MemoryScope.AGENT, pctx, kinds=[MemoryKind.CONVERSATION_TURN])
    recap_rec = next(r for r in view if r.role == "assistant" and "did the thing" in r.content)
    assert ref in collect_blob_refs(recap_rec), (
        "折叠重写的 finish 对没有声明幸存占位的 ref，GC 会在宽限期后误删"
    )


# ── 判决与折叠的顺序（2026-09-27）──────────────────────────────────────────────
#
# 判 success 会经带外 finalize 走 close（写 finish 对 + 折末段 raw），而那条路**刻意不产段
# 摘要**——`finalize._supersede_final_raw_segment` 的不变量是「末段 raw 与真实 Process Report
# 至少存其一」，段摘要与 finish 对同时存在就是把同一段记了两遍。
#
# 所以「这一段归谁记账」只有拿到判决才知道，不能先折了再判。此前（2280761 起）plain_text
# 判 success 就是先折后判，两份都写。


class _VerdictTM:
    """带外入口的最小替身：只回「接不接受」。

    **刻意不调 `finalize`**：本组用例钉的是「折不折」这条分支，判据只是带外入口的返回值。
    「判 success 时 finalize 必须被调、且排在仲裁之后 / `_settle` 之前」那条契约由真
    `TaskManager` 的用例钉（`tests/unit/test_out_of_band_verdict.py`），不在这里用替身自证
    ——替身自证等于让测试去核对自己的假设。
    """

    def __init__(self, *, accepted: bool = True) -> None:
        self._accepted = accepted
        self.submitted: list[str] = []

    async def apply_out_of_band_verdict(self, task_id, outcome, **kw) -> bool:
        self.submitted.append(outcome.verdict)
        return self._accepted


def _verdict_gateway(verdict: str, recap: str = "段摘要文本"):
    class _G:
        async def invoke(self, *, tool_name, arguments, state, ctx, tool_call_id):
            return SimpleNamespace(
                content=f"Assessment recorded: outcome={verdict}.",
                is_error=False,
                metadata={K.OBSERVER_OUTCOME: verdict,
                          K.OBSERVER_ACT_RECAP: recap,
                          K.OBSERVER_TASK_SUMMARY: ""},
            )

    return _G()


async def _run_judging(monkeypatch, state, ctx, *, boundary, verdict, accepted=True,
                       judging=None):
    # 不带 short_segment_token_threshold → 阈值取 0 → 短段门关闭，照常折叠（与本文件其他
    # 用例同一手法）。预置段只有一条 assistant，带上真实默认阈值会被判成短段而免折。
    state.agent.loop_config = SimpleNamespace(compact_keep_last=2, max_turns_per_observe=3)
    state.session = SimpleNamespace(id="s1", tenant_id="default", token_used=0)
    ctx.capability_gateway = _verdict_gateway(verdict)
    tm = _VerdictTM(accepted=accepted)
    ctx.task_manager = tm
    # 判定档与摘要档的 terminal tool 不同名，替身要跟着分：`_run_judging` 也被
    # `boundary="interrupt"`（不判）那条用例复用，故按 `_judges` 选，不写死。
    # `judging=` 显式覆盖留给「边界该判、但这个 agent 没有 observer」那一档（见下）。
    if judging is None:
        judging = boundaries.judges(boundary)
    stream = (_fake_stream_report_task_outcome if judging
              else _fake_stream_collect_process_report)
    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", stream)
    await runner.launch_recap(state, ctx, boundary=boundary)
    return tm


async def _summaries(state, ctx) -> list:
    return await ctx.memory.recall_recent(
        state.scope, [MemoryEventType.TASK_COMPACT_SUMMARY], 100, ctx.provider_ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["plain_text", "finish_park"])
async def test_a_closing_verdict_leaves_the_segment_to_the_finish_pair(
        monkeypatch, fake_state_ctx, boundary):
    """判 success 且仲裁接受 → 这一段归 close 的 finish 对，**不产段摘要**。"""
    state, ctx = fake_state_ctx
    tm = await _run_judging(monkeypatch, state, ctx, boundary=boundary, verdict="success")

    assert tm.submitted == ["success"], "前提不成立：判决压根没提交"
    assert await _summaries(state, ctx) == [], (
        "判 success 之后还产了段摘要——同一段会被记两遍（段摘要 + close 的 finish 对）")


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["plain_text", "finish_park"])
async def test_a_retry_verdict_still_folds_the_segment(monkeypatch, fake_state_ctx, boundary):
    """判 retry → 维持 park，没有 close，这一段仍归段摘要（下一轮 act 要靠它看见进度）。"""
    state, ctx = fake_state_ctx
    tm = await _run_judging(monkeypatch, state, ctx, boundary=boundary, verdict="continue")

    assert tm.submitted == ["continue"]
    assert len(await _summaries(state, ctx)) == 1


@pytest.mark.asyncio
async def test_a_rejected_verdict_still_folds_the_segment(monkeypatch, fake_state_ctx):
    """仲裁拒（人先开口）→ task 一个字段都没动，这一段同样要有人记。

    水位线保证只折这次 launch 之前的记录，人刚说的那句话不会被卷进来。
    """
    state, ctx = fake_state_ctx
    tm = await _run_judging(
        monkeypatch, state, ctx, boundary="plain_text", verdict="success", accepted=False)

    assert tm.submitted == ["success"]
    assert len(await _summaries(state, ctx)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["plain_text", "finish_park"])
async def test_no_observe_role_downgrades_the_judging_tier_to_recap(
        monkeypatch, fake_state_ctx, boundary):
    """模板没有 ROLE → 让位边界也不判，只产段摘要（2026-09-28）。

    判定档的条件是 `_judges(boundary)` **与** `has_observe_role(state)` 相与。没有 observer
    还去判，等于让 actor 顶着自己的 SOUL 判自己——装配层此前正是这么回退的，现在 observe
    家族缺 facet 不再回退 act。

    与之配套：`act.py` 里 `finish_park` 那条让位判据也认同一个 `has_observe_role`，所以这两个
    边界在无 ROLE 的模板上其实只剩 `plain_text` 到得了（让位给用户不需要 observer），
    `finish_park` 压根不会发生。这里两个都测，钉的是本函数自己的分档不看边界名。
    """
    import dataclasses

    state, ctx = fake_state_ctx
    tpl = state.extra["template"]
    state.extra["template"] = dataclasses.replace(
        tpl, identity={k: v for k, v in tpl.identity.items() if k != "observe"})

    tm = await _run_judging(monkeypatch, state, ctx, boundary=boundary,
                            verdict="success", judging=False)

    assert tm.submitted == [], "没有 observer 还是提交了判决"
    assert len(await _summaries(state, ctx)) == 1, "降成只摘要档之后段摘要仍要产"


@pytest.mark.asyncio
async def test_a_non_judging_boundary_never_asks_the_entry(monkeypatch, fake_state_ctx):
    """不判的边界连带外入口都不碰——那份判决已由别处给出，覆盖它是纯破坏。"""
    state, ctx = fake_state_ctx
    tm = await _run_judging(monkeypatch, state, ctx, boundary="interrupt", verdict="success")

    assert tm.submitted == []
    assert len(await _summaries(state, ctx)) == 1

