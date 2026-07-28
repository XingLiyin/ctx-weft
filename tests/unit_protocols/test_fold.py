"""fold 原子原语（v2 设计 §4 · P2d）：遗忘 + 补偿单事务完成。

- 纯遗忘（replacements=[]）
- 遗忘 + 摘要 / 多 replacement 保序
- 已 superseded / 不存在 id 幂等跳过
- replacement 带预生成 id → record-id 契约（重放幂等）
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider
from ctx_weft.protocols import (
    MemoryAddress,
    MemoryEvent,
    MemoryEventType,
    MemoryLayer,
    ProviderContext,
)
from ctx_weft.protocols.memory_compat import MemoryKind

_T0 = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
_ADDR = MemoryAddress(session_id="s1", task_id="t1", agent_id="a1")


def _ctx() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default")


def _turn(content: str, minute: int, role: str = "assistant") -> MemoryEvent:
    return MemoryEvent(
        kind=MemoryKind.CONVERSATION_TURN, layer=MemoryLayer.TASK,
        scope=_ADDR, content=content, timestamp=_T0 + timedelta(minutes=minute), role=role,
    )


def _summary(content: str, minute: int, id: str | None = None) -> MemoryEvent:
    return MemoryEvent(
        kind=MemoryKind.SUMMARY, layer=MemoryLayer.TASK,
        scope=_ADDR, content=content, timestamp=_T0 + timedelta(minutes=minute),
        role="assistant", id=id,
    )


async def _view(m: InMemoryMemoryProvider) -> list[str]:
    view = await m.load_view(_ADDR, MemoryLayer.TASK, _ctx())
    return [r.content for r in view]


async def test_fold_pure_forget() -> None:
    m = InMemoryMemoryProvider()
    ids = [await m.ingest(_turn(f"c{i}", i), _ctx()) for i in range(3)]
    new_ids = await m.fold(ids, [], _ctx())
    assert new_ids == []
    assert await _view(m) == []


async def test_fold_forget_plus_summary() -> None:
    m = InMemoryMemoryProvider()
    ids = [await m.ingest(_turn(f"c{i}", i), _ctx()) for i in range(3)]
    new_ids = await m.fold(ids, [_summary("recap", 3)], _ctx())
    assert len(new_ids) == 1
    assert await _view(m) == ["recap"]


async def test_fold_multiple_replacements_keep_order() -> None:
    m = InMemoryMemoryProvider()
    ids = [await m.ingest(_turn("old", 0), _ctx())]
    new_ids = await m.fold(
        ids,
        [_turn("pair-asst", 1), _turn("pair-tool", 2, role="tool")],
        _ctx(),
    )
    assert len(new_ids) == 2
    assert await _view(m) == ["pair-asst", "pair-tool"]


async def test_fold_skips_missing_and_already_superseded() -> None:
    m = InMemoryMemoryProvider()
    rid = await m.ingest(_turn("keep-gone", 0), _ctx())
    await m.fold([rid], [], _ctx())
    # 再折同 id + 不存在 id：不炸、no-op
    new_ids = await m.fold([rid, "mev_nonexistent"], [_summary("s", 1)], _ctx())
    assert len(new_ids) == 1
    assert await _view(m) == ["s"]


async def test_fold_replacement_with_pregenerated_id_is_idempotent() -> None:
    """重放同一 fold（同 supersede_ids + 同 replacement id）→ 单条，不重复写入。"""
    m = InMemoryMemoryProvider()
    rid = await m.ingest(_turn("raw", 0), _ctx())
    first = await m.fold([rid], [_summary("recap", 1, id="mem_fold_001")], _ctx())
    second = await m.fold([rid], [_summary("recap", 1, id="mem_fold_001")], _ctx())
    assert first == second == ["mem_fold_001"]
    assert await _view(m) == ["recap"]
