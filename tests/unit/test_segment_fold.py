"""segment_fold（v2 P3c）：段作用域折叠的框架侧策展——移植自 provider apply_compact 语义。

固定政策（两调用点一致）：keep_last=0、protect = {role=user 回合, SUMMARY kind}、
段界 = 最后一条 role=user 的 CONVERSATION_TURN。锚点/段尾语义与 2026-07-21 排序契约一致。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ctx_weft.core.utils.content import collect_blob_refs
from ctx_weft.core.loop.steps.segment_fold import SegmentFoldResult, segment_fold
from ctx_weft.core.media.refs import encode_image_placeholder
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from ctx_weft.protocols import (
    MemoryAddress,
    MemoryEvent,
    MemoryKind,
    MemoryScope,
    ProviderContext,
)

_T0 = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
_ADDR = MemoryAddress(session_id="s1", task_id="t1", agent_id="a1")


def _ctx() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default")


def _turn(content: str, minute: int, role: str) -> MemoryEvent:
    return MemoryEvent(kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK,
                       address=_ADDR, content=content,
                       timestamp=_T0 + timedelta(minutes=minute), role=role)


def _summary(content: str, minute: int) -> MemoryEvent:
    return MemoryEvent(kind=MemoryKind.SUMMARY, scope=MemoryScope.TASK,
                       address=_ADDR, content=content, role="assistant",
                       timestamp=_T0 + timedelta(minutes=minute))


async def _view(m: InMemoryMemoryProvider) -> list[tuple[str, str]]:
    view = await m.load_view(_ADDR, MemoryScope.TASK, _ctx())
    return [(str(r.kind), r.content) for r in view]


async def test_folds_only_current_segment_after_last_user_turn() -> None:
    m = InMemoryMemoryProvider()
    await m.ingest(_turn("UP1", 0, "user"), _ctx())
    await m.ingest(_turn("A1-prev-seg", 1, "assistant"), _ctx())   # 前段免折残留
    await m.ingest(_turn("UP2", 2, "user"), _ctx())
    await m.ingest(_turn("A2", 3, "assistant"), _ctx())
    await m.ingest(_turn("T2", 4, "tool"), _ctx())

    result = await segment_fold(m, _ADDR, MemoryScope.TASK, "recap", _ctx())

    assert isinstance(result, SegmentFoldResult)
    assert result.summary_event_id
    contents = [c for _, c in await _view(m)]
    assert "A1-prev-seg" in contents, "前段 raw 不得跨段折入"
    assert "A2" not in contents and "T2" not in contents
    assert "recap" in contents
    # 段摘要 role=assistant（TASK 层自述体）
    view = await m.load_view(_ADDR, MemoryScope.TASK, _ctx())
    recap = next(r for r in view if r.content == "recap")
    assert recap.role == "assistant" and recap.kind is MemoryKind.SUMMARY


async def test_protects_user_turns_and_summaries() -> None:
    m = InMemoryMemoryProvider()
    await m.ingest(_turn("UP1", 0, "user"), _ctx())
    await m.ingest(_summary("S-old", 1), _ctx())   # 段内旧摘要（多段累积）不得被吞
    await m.ingest(_turn("A1", 2, "assistant"), _ctx())

    await segment_fold(m, _ADDR, MemoryScope.TASK, "S-new", _ctx())

    contents = [c for _, c in await _view(m)]
    assert "UP1" in contents and "S-old" in contents
    assert "A1" not in contents and "S-new" in contents


async def test_anchor_lands_before_following_survivor() -> None:
    """被折区起点之后有幸存者 → 摘要锚 survivor.ts − 1µs，排在其前。"""
    m = InMemoryMemoryProvider()
    await m.ingest(_turn("UP1", 0, "user"), _ctx())
    await m.ingest(_turn("A1", 1, "assistant"), _ctx())
    await m.ingest(_summary("S-mid", 2), _ctx())   # 折区中幸存的摘要
    await m.ingest(_turn("A2", 3, "assistant"), _ctx())

    await segment_fold(m, _ADDR, MemoryScope.TASK, "recap", _ctx())

    contents = [c for _, c in await _view(m)]
    assert contents == ["UP1", "recap", "S-mid"], f"锚点应在幸存摘要之前: {contents}"


async def test_segment_tail_anchor_does_not_use_now() -> None:
    """段尾无幸存者 → 摘要锚被折段末条 ts（不用 now）；后到的 UP 天然排其后。"""
    m = InMemoryMemoryProvider()
    await m.ingest(_turn("UP1", 0, "user"), _ctx())
    await m.ingest(_turn("A1", 1, "assistant"), _ctx())

    await segment_fold(m, _ADDR, MemoryScope.TASK, "recap", _ctx())
    # 模拟迟到收尾后新一轮消息（更晚墙钟）
    await m.ingest(_turn("UP-new", 90, "user"), _ctx())

    contents = [c for _, c in await _view(m)]
    assert contents == ["UP1", "recap", "UP-new"], f"摘要不得越过新消息: {contents}"


async def test_summary_declares_surviving_placeholder_refs() -> None:
    """`_protected` 只护住 user 回合与 SUMMARY，role=tool 记录会被折进 to_archive。
    若 LLM 摘要逐字带着其中一条的 L0.5 占位向前走，供体记录被本次 fold supersede，
    占位的活引用只剩新摘要记录能扛——GC 的 mark 判据必须能在它身上看见这条 ref。"""
    ref = "blob:" + "d" * 64
    placeholder = encode_image_placeholder(ref, "image/png")
    m = InMemoryMemoryProvider()
    await m.ingest(_turn("UP1", 0, "user"), _ctx())
    await m.ingest(_turn(placeholder, 1, "tool"), _ctx())   # 供体：折区内、role=tool

    summary_text = f"did the thing; {placeholder}"
    await segment_fold(m, _ADDR, MemoryScope.TASK, summary_text, _ctx())

    view = await m.load_view(_ADDR, MemoryScope.TASK, _ctx())
    summary_rec = next(r for r in view if r.kind is MemoryKind.SUMMARY)
    assert ref in collect_blob_refs(summary_rec), (
        "折叠产出的摘要没有声明幸存占位的 ref，GC 会在宽限期后误删"
    )


async def test_counts_reported() -> None:
    m = InMemoryMemoryProvider()
    await m.ingest(_turn("UP1", 0, "user"), _ctx())
    await m.ingest(_turn("A1", 1, "assistant"), _ctx())
    await m.ingest(_turn("T1", 2, "tool"), _ctx())

    result = await segment_fold(m, _ADDR, MemoryScope.TASK, "recap", _ctx())
    # before：UP+A+T = 3；after：UP + 摘要 = 2
    assert result.events_before == 3
    assert result.events_after == 2


# ── 段界水位线（2026-09-22）───────────────────────────────────────────────────
#
# 后台 observe 是 fire-and-forget，它判完时人可能已经开口、新的 USER_PROMPT 已落库。
# 段界若仍动态查找，那条新消息就成了「最后一条 user 回合」，折叠池随之变空——要折的
# 那一段一条都没折，摘要还落到 `now_utc()` 分支排到末尾。这两条用例正是那个 bug 与
# 它的修法。

async def test_without_watermark_a_late_user_turn_steals_the_boundary() -> None:
    """不设水位线时的既有行为：新 UP 抢走段界，上一段一条都没折。"""
    m = InMemoryMemoryProvider()
    await m.ingest(_turn("UP1", 0, "user"), _ctx())
    await m.ingest(_turn("A1", 1, "assistant"), _ctx())
    await m.ingest(_turn("A2", 2, "assistant"), _ctx())
    await m.ingest(_turn("UP2-人中途开口", 3, "user"), _ctx())

    res = await segment_fold(m, _ADDR, MemoryScope.TASK, "段摘要", _ctx())

    contents = [c for _k, c in await _view(m)]
    # A1/A2 一条没折，摘要还排到了所有记录的末尾——正是水位线要治的那个形态。
    assert contents == ["UP1", "A1", "A2", "UP2-人中途开口", "段摘要"]
    assert res.events_after == res.events_before + 1           # 只多了一条摘要


async def test_watermark_keeps_the_boundary_at_launch_time() -> None:
    """钉住水位线：要折的仍是原来那段，摘要落回新 UP 之前。"""
    m = InMemoryMemoryProvider()
    await m.ingest(_turn("UP1", 0, "user"), _ctx())
    await m.ingest(_turn("A1", 1, "assistant"), _ctx())
    await m.ingest(_turn("A2", 2, "assistant"), _ctx())
    watermark = _T0 + timedelta(minutes=2)                     # 后台 observe 开跑的时刻
    await m.ingest(_turn("UP2-人中途开口", 3, "user"), _ctx())

    await segment_fold(m, _ADDR, MemoryScope.TASK, "段摘要", _ctx(), watermark)

    contents = [c for _k, c in await _view(m)]
    assert "A1" not in contents and "A2" not in contents       # 折掉了
    assert contents == ["UP1", "段摘要", "UP2-人中途开口"]      # 摘要锚在新 UP 之前


async def test_watermark_does_not_swallow_the_new_user_turn() -> None:
    """水位线之后的记录既不进折叠池，也不被 supersede——它是下一段的开头。"""
    m = InMemoryMemoryProvider()
    await m.ingest(_turn("UP1", 0, "user"), _ctx())
    await m.ingest(_turn("A1", 1, "assistant"), _ctx())
    await m.ingest(_turn("A2", 2, "assistant"), _ctx())
    watermark = _T0 + timedelta(minutes=2)
    await m.ingest(_turn("UP2", 3, "user"), _ctx())
    await m.ingest(_turn("A3", 4, "assistant"), _ctx())        # 下一段已经开跑

    await segment_fold(m, _ADDR, MemoryScope.TASK, "段摘要", _ctx(), watermark)

    contents = [c for _k, c in await _view(m)]
    assert contents == ["UP1", "段摘要", "UP2", "A3"]
