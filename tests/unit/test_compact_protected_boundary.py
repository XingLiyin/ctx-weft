"""Compaction retains the earliest protected record or pending tool call."""

from types import SimpleNamespace

import pytest

from ctx_weft.core.loop.steps.compact import _split_for_collapse
from ctx_weft.protocols import MemoryKind, MemoryScope


@pytest.mark.parametrize(("record_ids", "call_ids", "expected_cut"), [
    (set(), set(), 4),
    ({"missing"}, {"missing"}, 4),
    ({"r1"}, set(), 1),
    (set(), {"c2"}, 2),
    ({"r3"}, {"c1", "c2"}, 1),
    ({"r5"}, {"c5"}, 4),
])
def test_protected_records_only_move_the_collapse_boundary_earlier(record_ids, call_ids, expected_cut):
    records = [SimpleNamespace(
        id=f"r{i}", kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK,
        role="assistant", metadata={"tool_calls": [{"id": f"c{i}"}]},
    ) for i in range(6)]

    fold, keep = _split_for_collapse(
        records, 2, protected_record_ids=record_ids, protected_call_ids=call_ids,
    )

    assert fold == records[:expected_cut]
    assert keep == records[expected_cut:]
