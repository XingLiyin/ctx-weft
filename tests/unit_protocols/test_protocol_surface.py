"""MemoryProvider selects IDs and loads immutable records; load_view is retired."""

from __future__ import annotations

from ctx_weft.protocols import MemoryProvider


V2_SURFACE = (
    "ingest", "fold",
    "load_view_ids", "read_records", "recall_topic", "recall_semantic",
    "subscribe_topic", "list_subscriptions",
    "describe",
)
SUNSET = (
    "recall_recent", "recall_recent_by_agent", "count_recent",
    "supersede", "apply_compact", "load_view",
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
