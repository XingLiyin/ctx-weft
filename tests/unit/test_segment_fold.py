"""segment_fold（v2 P3c）：段作用域折叠的框架侧策展——移植自 provider apply_compact 语义。

固定政策（两调用点一致）：keep_last=0、protect = {role=user 回合, SUMMARY kind}、
段界 = 最后一条 role=user 的 CONVERSATION_TURN。锚点/段尾语义与 2026-07-21 排序契约一致。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ctx_weft.core.loop.steps.segment_fold import SegmentFoldResult, segment_fold
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider
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
    return MemoryEvent(kind=MemoryKind.CONVERSATION_TURN, layer=MemoryScope.TASK,
                       scope=_ADDR, content=content,
                       timestamp=_T0 + timedelta(minutes=minute), role=role)


def _summary(content: str, minute: int) -> MemoryEvent:
    return MemoryEvent(kind=MemoryKind.SUMMARY, layer=MemoryScope.TASK,
                       scope=_ADDR, content=content, role="assistant",
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


async def test_counts_reported() -> None:
    m = InMemoryMemoryProvider()
    await m.ingest(_turn("UP1", 0, "user"), _ctx())
    await m.ingest(_turn("A1", 1, "assistant"), _ctx())
    await m.ingest(_turn("T1", 2, "tool"), _ctx())

    result = await segment_fold(m, _ADDR, MemoryScope.TASK, "recap", _ctx())
    # before：UP+A+T = 3；after：UP + 摘要 = 2
    assert result.events_before == 3
    assert result.events_after == 2
