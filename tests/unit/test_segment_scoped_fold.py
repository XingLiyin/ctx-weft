"""段作用域折叠（方案 A，2026-07-21）：段 = 最后一条 active USER_PROMPT 之后。

Bug：短段免折（is_short_segment）让上一段 raw 以 active 状态跨过下一条 USER_PROMPT；
下一次段边界折叠时 apply_compact 无段概念，把两段 raw 合折成**一条**摘要，且锚点算法
把它插到 UP2 之前 → 渲染序 [UP1][合并摘要][UP2]，UP2 永远读不到回答、时序倒置。

修复契约（三层收口）：
1. apply_compact 新增 since_last：归档池限定在「最后一条 active since_last 类型记录之后」；
2. is_short_segment 只统计当前段（末条 UP 之后）的 raw；
3. 两个折叠调用方（_run_background_observe 非 close 分支、_fold_retry_segment）传
   since_last=USER_PROMPT——更早的短段 raw 永久保 raw（「短 → 原文成胶囊」）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import ctx_weft.core.loop.steps.background_observe as bo
import ctx_weft.core.loop.steps.observe as _obs_mod
from ctx_weft.core.loop.steps.observe import ObserveStep, Verdict
from ctx_weft.core.orchestrator.control_capability import (
    BACKGROUND_PROCESS_REPORT_NAME,
    ControlResult,
)
from ctx_weft.protocols import (
    ImagePart,
    MemoryEvent,
    MemoryEventType,
    MemoryScope,
    MemoryAddress,
    ProviderContext,
    TextPart,
)
from ctx_weft.providers.llm.tokenizer import HeuristicTokenizer
from ctx_weft.core.loop.steps.segment_fold import segment_fold
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider

MT = MemoryEventType

_PCTX = ProviderContext(session_id="s1", tenant_id="default", task_id="t1", agent_id="a1")
_SCOPE = MemoryAddress(session_id="s1", task_id="t1", agent_id="a1")
_BASE = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)


def _ts(us: int) -> datetime:
    return _BASE + timedelta(microseconds=us)


async def _seed_two_segments(mem: InMemoryMemoryProvider, a1_content: str = "A1 段一回答") -> None:
    """时间线：UP1 → A1（段一 raw，模拟短段免折后残留）→ UP2 → A2a/A2b（当前段 raw）。"""
    events = [
        MemoryEvent(type=MT.USER_PROMPT,  address=_SCOPE, content="UP1 第一问", timestamp=_ts(10), role="user"),
        MemoryEvent(type=MT.LLM_RESPONSE, address=_SCOPE, content=a1_content,   timestamp=_ts(20), role="assistant"),
        MemoryEvent(type=MT.USER_PROMPT,  address=_SCOPE, content="UP2 第二问", timestamp=_ts(30), role="user"),
        MemoryEvent(type=MT.LLM_RESPONSE, address=_SCOPE, content="A2a 当前段", timestamp=_ts(40), role="assistant"),
        MemoryEvent(type=MT.TOOL_RESULT,  address=_SCOPE, content="A2b 工具",   timestamp=_ts(50), role="tool"),
    ]
    for ev in events:
        await mem.ingest(ev, _PCTX)


async def _chrono(mem: InMemoryMemoryProvider, scope=_SCOPE, pctx=_PCTX) -> list:
    """active 记录按时间正序（recall 是 newest-first，反转即渲染序）。"""
    recs = await mem.recall_recent(
        scope, [MT.USER_PROMPT, MT.LLM_RESPONSE, MT.TOOL_RESULT, MT.TASK_COMPACT_SUMMARY],
        100, pctx)
    return list(reversed(recs))


# ─────────────────────────────────────────────────────────────────────────────
# 1) provider 层：apply_compact(since_last=USER_PROMPT) 只折当前段
# ─────────────────────────────────────────────────────────────────────────────


async def test_apply_compact_since_last_folds_only_current_segment():
    """since_last=USER_PROMPT：只折 UP2 之后的 raw；段一 A1 保 raw；摘要锚在 UP2 之后。"""
    mem = InMemoryMemoryProvider()
    await _seed_two_segments(mem)

    await segment_fold(mem, _SCOPE, MemoryScope.TASK, "S2", _PCTX)

    chrono = await _chrono(mem)
    kinds = [(r.type, r.content) for r in chrono]
    assert (MT.LLM_RESPONSE, "A1 段一回答") in kinds, f"段一 raw 不得被跨段折掉: {kinds}"
    assert not any(c in ("A2a 当前段", "A2b 工具") for _, c in kinds), f"当前段 raw 应被折: {kinds}"
    # 渲染序：[UP1, A1, UP2, S2]——摘要必须在 UP2 之后，UP2 的回答位置不空
    assert [c for _, c in kinds] == ["UP1 第一问", "A1 段一回答", "UP2 第二问", "S2"], kinds


async def test_apply_compact_since_last_without_up_folds_whole_scope():
    """scope 内没有 since_last 类型记录 → 回退旧行为：全量归档池（不因参数存在而漏折）。"""
    mem = InMemoryMemoryProvider()
    for i, (typ, content, role) in enumerate([
        (MT.LLM_RESPONSE, "a", "assistant"),
        (MT.TOOL_RESULT, "b", "tool"),
    ]):
        await mem.ingest(MemoryEvent(type=typ, address=_SCOPE, content=content,
                                     timestamp=_ts(10 + i), role=role), _PCTX)

    await segment_fold(mem, _SCOPE, MemoryScope.TASK, "S", _PCTX)

    chrono = await _chrono(mem)
    assert [r.content for r in chrono] == ["S"], "无 UP 时应整 scope 照折"


# ─────────────────────────────────────────────────────────────────────────────
# 2) is_short_segment：只统计当前段（末条 active UP 之后）的 raw
# ─────────────────────────────────────────────────────────────────────────────


def _short_seg_state_ctx(mem: InMemoryMemoryProvider, threshold: int):
    state = SimpleNamespace(
        scope=_SCOPE,
        agent=SimpleNamespace(loop_config=SimpleNamespace(short_segment_token_threshold=threshold)),
    )
    ctx = SimpleNamespace(
        memory=mem,
        provider_ctx=_PCTX,
        llm=SimpleNamespace(tokenizer=HeuristicTokenizer()),
    )
    return state, ctx


async def test_is_short_segment_counts_only_current_segment():
    """段一 raw 超阈值、当前段 raw 极短 → 仍判 short（旧实现把段一也计入 → False）。"""
    mem = InMemoryMemoryProvider()
    await _seed_two_segments(mem, a1_content="长" * 4000)  # 段一 raw 远超阈值
    # 当前段凑两条 LLM 回复：绕开单回复门，让 token 口径成为判别项
    await mem.ingest(MemoryEvent(
        type=MT.LLM_RESPONSE, address=_SCOPE, content="A2c 短",
        timestamp=_ts(55), role="assistant"), _PCTX)
    state, ctx = _short_seg_state_ctx(mem, threshold=400)

    assert await bo.is_short_segment(state, ctx) is True, \
        "免折门只该看当前段（末条 UP 之后）的 raw"


async def test_is_short_segment_long_current_segment_not_short():
    """当前段 raw 超阈值 → 不 short（照常折叠）。"""
    mem = InMemoryMemoryProvider()
    await _seed_two_segments(mem)
    await mem.ingest(MemoryEvent(
        type=MT.LLM_RESPONSE, address=_SCOPE, content="长" * 4000,
        timestamp=_ts(60), role="assistant"), _PCTX)
    state, ctx = _short_seg_state_ctx(mem, threshold=400)

    assert await bo.is_short_segment(state, ctx) is False


async def test_is_short_segment_single_llm_reply_is_short_regardless_of_tokens():
    """当前段只有一条 LLM 回复 → 判 short，哪怕它远超 token 阈值。

    一条回复折成摘要是净亏：recap 常比原文还长，原文对下一轮信息更全。"""
    mem = InMemoryMemoryProvider()
    await _seed_two_segments(mem)
    await mem.ingest(MemoryEvent(
        type=MT.USER_PROMPT, address=_SCOPE, content="UP3 第三问",
        timestamp=_ts(60), role="user"), _PCTX)
    await mem.ingest(MemoryEvent(
        type=MT.LLM_RESPONSE, address=_SCOPE, content="长" * 4000,
        timestamp=_ts(70), role="assistant"), _PCTX)
    await mem.ingest(MemoryEvent(
        type=MT.TOOL_RESULT, address=_SCOPE, content="工具" * 2000,
        timestamp=_ts(80), role="tool"), _PCTX)
    state, ctx = _short_seg_state_ctx(mem, threshold=400)

    assert await bo.is_short_segment(state, ctx) is True,         "单条 LLM 回复的段不该折，无论多长"


async def test_is_short_segment_counts_image_parts():
    """当前段文本极短但带图 → 图片 token 使其超阈值，必须判非 short。

    _seed_two_segments 的当前段是 A2a(assistant) + A2b(tool)，只有 1 条 assistant
    回合会命中单回复门直接 True；故再补一条带图的 assistant 回合凑够 2 条。
    """
    mem = InMemoryMemoryProvider()
    await _seed_two_segments(mem)
    await mem.ingest(MemoryEvent(
        type=MT.LLM_RESPONSE, address=_SCOPE,
        content=[TextPart(text="ok"), ImagePart(data="ZGF0YQ==", media_type="image/png")],
        timestamp=_ts(55), role="assistant"), _PCTX)
    state, ctx = _short_seg_state_ctx(mem, threshold=400)

    assert await bo.is_short_segment(state, ctx) is False, \
        "一张图 1600 token 已超阈值 400，不得因图算 0 而误判短段免折"


# ─────────────────────────────────────────────────────────────────────────────
# 3) 调用方收口：bg 段折 / retry 段折都只折当前段
# ─────────────────────────────────────────────────────────────────────────────


def _make_tool_call_chunk(name: str, call_id: str = "tc1"):
    return SimpleNamespace(
        kind="tool_call",
        tool_call=SimpleNamespace(id=call_id, name=name, arguments={"task_process_report": "S2"}),
        text="", usage=None,
    )


def _make_usage_chunk():
    from ctx_weft.protocols import LLMUsage
    return SimpleNamespace(
        kind="usage", usage=LLMUsage(prompt_tokens=10, completion_tokens=5),
        tool_call=None, text="")


async def _fake_stream(ctx, state, request):
    yield _make_tool_call_chunk(BACKGROUND_PROCESS_REPORT_NAME)
    yield _make_usage_chunk()


class _FakeGateway:
    async def invoke(self, *, tool_name, arguments, state, ctx, tool_call_id):
        return ControlResult(content="S2")


@pytest.mark.asyncio
async def test_background_fold_after_short_skip_keeps_previous_segment_raw(
        monkeypatch, fake_state_ctx):
    """免折残留场景端到端：fixture 预置段一 [UP1, LLM, TOOL]（视为短段免折残留），
    注入 UP2 + 当前段 raw 后触发 plain_text 段折——段一 raw 必须原样保留，
    摘要必须锚在 UP2 之后。旧行为：段一被跨段折掉、合并摘要落在 UP2 之前。"""
    state, ctx = fake_state_ctx  # 预置 [UP1(1us), LLM(2us), TOOL(3us)]
    ctx.capability_gateway = _FakeGateway()
    state.agent.loop_config = SimpleNamespace(compact_keep_last=2, max_turns_per_observe=3)
    state.session = SimpleNamespace(id="s1", tenant_id="default", token_used=0)
    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", _fake_stream)

    # 第二条用户消息 + 当前段 raw（真实 now 保证时序在预置事件之后）
    await ctx.memory.ingest(MemoryEvent(
        type=MT.USER_PROMPT, address=state.scope, content="UP2 第二问",
        timestamp=datetime.now(UTC), role="user"), ctx.provider_ctx)
    await asyncio.sleep(0.002)
    await ctx.memory.ingest(MemoryEvent(
        type=MT.LLM_RESPONSE, address=state.scope, content="A2 当前段",
        timestamp=datetime.now(UTC), role="assistant"), ctx.provider_ctx)

    await bo.launch_background_observe(state, ctx, boundary="plain_text")

    chrono = await _chrono(ctx.memory, state.scope, ctx.provider_ctx)
    contents = [r.content for r in chrono]
    assert "hello llm" in contents and "tool result" in contents, \
        f"段一 raw 不得被跨段折掉: {contents}"
    assert "A2 当前段" not in contents, f"当前段 raw 应被折: {contents}"
    up2_idx = contents.index("UP2 第二问")
    summary_idx = next(i for i, r in enumerate(chrono) if r.type == MT.TASK_COMPACT_SUMMARY)
    assert summary_idx > up2_idx, \
        f"摘要必须锚在 UP2 之后（UP2 的回答位），实际渲染序: {contents}"


async def test_fold_retry_segment_keeps_previous_segment_raw():
    """retry 段折同门收口：段一 raw 保留，摘要锚在 UP2 之后。"""
    mem = InMemoryMemoryProvider()
    await _seed_two_segments(mem)

    state = SimpleNamespace(
        run_id="r1", sequence_counter=0,
        agent=SimpleNamespace(id="a1", loop_config=SimpleNamespace(compact_keep_last=2)),
        session=SimpleNamespace(id="s1", tenant_id="default"),
        task=SimpleNamespace(id="t1", process_report=""),
        scope=_SCOPE, transcript=[],
        extra={"template": None}, act_exit_reason="max_turns",
    )
    ctx = SimpleNamespace(
        memory=mem, provider_ctx=_PCTX,
        llm=SimpleNamespace(tokenizer=HeuristicTokenizer()),
        task_manager=None,
    )
    verdict = Verdict(task_outcome="retry", act_recap="S2", reported=True)

    await ObserveStep()._fold_retry_segment(state, ctx, verdict, [])

    chrono = await _chrono(mem)
    contents = [r.content for r in chrono]
    assert "A1 段一回答" in contents, f"段一 raw 不得被跨段折掉: {contents}"
    assert contents == ["UP1 第一问", "A1 段一回答", "UP2 第二问", "S2"], contents


# ─────────────────────────────────────────────────────────────────────────────
# 4) close 收尾：_supersede_final_raw_segment 同样只删当前段（末条 UP 之后）
# ─────────────────────────────────────────────────────────────────────────────


async def test_supersede_final_raw_segment_keeps_previous_segment_raw():
    """长任务 close 删「末 raw 段」：短段免折残留的前段 raw 无胶囊代表，删了即信息丢失
    （前段 UP 失去回答位）——必须只删末条 UP 之后的当前段 raw。"""
    from ctx_weft.core.loop.steps.finalize import _supersede_final_raw_segment

    mem = InMemoryMemoryProvider()
    await _seed_two_segments(mem)

    await _supersede_final_raw_segment(mem, _SCOPE, _PCTX)

    chrono = await _chrono(mem)
    contents = [r.content for r in chrono]
    assert "A1 段一回答" in contents, f"前段免折残留 raw 不得在 close 时被删: {contents}"
    assert not any(c in ("A2a 当前段", "A2b 工具") for c in contents), \
        f"末段 raw 应被删（finish 对已承载其 recap）: {contents}"


async def test_apply_compact_since_last_after_collapsed_up_still_folds():
    """L3 坍缩 UP 是「timestamp 回填（保留区之前）、seq 最高（后 ingest）」的记录。
    since_last 段界若按 seq 找最后一条 UP，会把段界推到所有 raw 之后 → 归档池空 →
    摘要照写、raw 一条不折。段界必须按渲染序（timestamp, seq_no）判定。"""
    from ctx_weft.core.loop.steps.compact import COLLAPSE_DELIM, collapse_task_layer

    mem = InMemoryMemoryProvider()
    seed = [
        (MT.USER_PROMPT,  "UP1 原始问题", 10, "user"),
        (MT.LLM_RESPONSE, "A1 早期回合", 20, "assistant"),
        (MT.LLM_RESPONSE, "A2 保留raw",  30, "assistant"),
        (MT.LLM_RESPONSE, "A3 保留raw",  40, "assistant"),
    ]
    for typ, content, off, role in seed:
        await mem.ingest(MemoryEvent(type=typ, address=_SCOPE, content=content,
                                     timestamp=_ts(off), role=role), _PCTX)

    # 真实 L3 坍缩：折 [UP1, A1]，坍缩 UP 锚在保留区之前（ts 回填）、seq 最高
    state = SimpleNamespace(scope=_SCOPE)
    ctx = SimpleNamespace(memory=mem, provider_ctx=_PCTX)
    folded = await collapse_task_layer(state, ctx, keep_last=2, summary_text="坍缩摘要")
    assert folded == 2

    # 段边界折叠：坍缩 UP 之后的 raw（A2/A3）是当前段，必须被折
    await segment_fold(mem, _SCOPE, MemoryScope.TASK, "S", _PCTX)

    chrono = await _chrono(mem)
    contents = [r.content for r in chrono]
    assert not any(c in ("A2 保留raw", "A3 保留raw") for c in contents), \
        f"坍缩 UP 在场时段折叠失效——raw 未被替换: {contents}"
    assert len(chrono) == 2 and chrono[0].type is MT.USER_PROMPT \
        and COLLAPSE_DELIM in chrono[0].content and chrono[1].content == "S", \
        f"期望 [坍缩UP, S]，实得: {[(r.type.value, r.content[:20]) for r in chrono]}"


async def test_supersede_final_raw_segment_without_up_supersedes_all():
    """scope 内无 UP（防御路径）→ 回退旧行为：全部 active raw 照删。"""
    from ctx_weft.core.loop.steps.finalize import _supersede_final_raw_segment

    mem = InMemoryMemoryProvider()
    await mem.ingest(MemoryEvent(
        type=MT.LLM_RESPONSE, address=_SCOPE, content="a",
        timestamp=_ts(10), role="assistant"), _PCTX)

    await _supersede_final_raw_segment(mem, _SCOPE, _PCTX)

    chrono = await _chrono(mem)
    assert chrono == [], "无 UP 时应保持旧行为全删"
