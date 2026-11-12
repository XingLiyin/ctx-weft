"""Collapse boundaries follow durable tool facts, without external ID lists."""

from types import SimpleNamespace
import pytest
from ctx_weft.protocols import MemoryKind, MemoryScope
from tests.compaction_testkit import _split_for_collapse


@pytest.mark.parametrize(
    ("pending_index", "expected_cut"),
    [
        (None, 4),
        (0, 0),
        (1, 1),
        (2, 2),
        (5, 4),
    ],
)
def test_unfinished_call_moves_boundary_from_record_facts(pending_index, expected_cut):
    records = [
        SimpleNamespace(
            id=f"r{i}",
            kind=MemoryKind.CONVERSATION_TURN,
            scope=MemoryScope.TASK,
            role="assistant",
            metadata={},
        )
        for i in range(6)
    ]
    if pending_index is not None:
        records[pending_index].metadata = {"execution_call_ids": ["pending"]}
    fold, kept = _split_for_collapse(records, 2)
    assert fold == records[:expected_cut]
    assert kept == records[expected_cut:]


def test_completed_tool_round_stays_together_across_recent_window():
    records = [
        SimpleNamespace(
            id=f"r{i}",
            kind=MemoryKind.CONVERSATION_TURN,
            scope=MemoryScope.TASK,
            role="assistant",
            metadata={},
        )
        for i in range(4)
    ]
    records[1].metadata = {"tool_calls": [{"id": "done"}]}
    records[2].role = "tool"
    records[2].metadata = {"tool_call_id": "done"}
    fold, kept = _split_for_collapse(records, 2)
    assert fold == records[:1]
    assert kept == records[1:]
