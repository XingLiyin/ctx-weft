"""Control tools expose qualified names; docstrings reference qualified names."""

from __future__ import annotations

from ctx_weft.core.orchestrator.control_capability import (
    DELEGATE_PLAN_NAME,
    DELEGATE_TASK_NAME,
    FINISH_TASK_NAME,
    delegate_task,
)


def test_qualified_name_constants() -> None:
    assert FINISH_TASK_NAME == "control__finish_task"
    assert DELEGATE_TASK_NAME == "control__delegate_task"
    assert DELEGATE_PLAN_NAME == "control__delegate_plan"


def test_delegate_task_docstring_uses_qualified_finish_name() -> None:
    assert "control__finish_task" in (delegate_task.__doc__ or "")
    # no bare-name reference left to mislead the LLM
    assert "use finish_task" not in (delegate_task.__doc__ or "")
