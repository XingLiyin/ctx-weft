"""Completion is observer-owned; no callable finish tool remains."""
from ctx_weft.core.capabilities.control_tools import _CONTROL_TOOLS
from ctx_weft.core.loop.steps.act import TurnRecord, _compose_final_outputs


def test_finish_task_is_not_a_registered_capability():
    assert "control:finish_task" not in _CONTROL_TOOLS
    assert all(cap.name != "finish_task" for cap, _ in _CONTROL_TOOLS.values())


def test_normal_reply_is_the_deliverable_without_a_finish_call():
    record = TurnRecord(turn=1, messages_sent=[], assistant_text="Actual final answer")
    assert _compose_final_outputs([record]) == ("Actual final answer", "")
