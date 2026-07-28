"""方法面清点（v2 设计 §4/§7 · P4b-2）：MemoryProvider 协议 = 8 方法。

写(2) ingest/fold + 读(3) load_view/recall_topic/recall_semantic +
订阅(2) subscribe_topic/list_subscriptions + 能力(1) describe。
旧 5 方法（recall_recent/by_agent/count_recent/supersede/apply_compact）与
CompactResult 自协议删除；in-memory provider 的读 wrapper 仅为存量测试兼容
（非协议方法，见 P4a 范围决策）。
"""

from __future__ import annotations

from ctx_weft.protocols import MemoryProvider


V2_SURFACE = (
    "ingest", "fold",
    "load_view", "recall_topic", "recall_semantic",
    "subscribe_topic", "list_subscriptions",
    "describe",
)
SUNSET = (
    "recall_recent", "recall_recent_by_agent", "count_recent",
    "supersede", "apply_compact",
)


def test_protocol_has_v2_surface() -> None:
    for name in V2_SURFACE:
        assert hasattr(MemoryProvider, name), f"protocol missing {name}"


def test_protocol_dropped_legacy_methods() -> None:
    for name in SUNSET:
        assert not hasattr(MemoryProvider, name), f"protocol still carries {name}"


def test_compact_result_removed() -> None:
    import ctx_weft.protocols as protocols
    assert not hasattr(protocols, "CompactResult")
