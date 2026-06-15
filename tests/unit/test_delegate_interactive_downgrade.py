"""interactive 只能沿用户面向链路下传：auto 父任务派生的子任务即使请求 interactive
也静默降级为 auto；interactive 父任务则保留子任务的 interactive。

覆盖 delegate_task / delegate_plan / replan 三处委派入口。
"""

from __future__ import annotations

from ctx_weft.core.orchestrator.control_capability import (
    ControlContext,
    delegate_plan,
    delegate_task,
    replan,
)
from ctx_weft.core.state.models import Task


class _FakeTM:
    def __init__(self) -> None:
        self.staged: list[Task] = []

    def stage_task(self, child: Task, **kwargs) -> None:
        self.staged.append(child)

    def get_task(self, tid: str):
        return None


def _ctx(tm: _FakeTM, parent_mode: str) -> ControlContext:
    parent = Task(
        id="p1", session_id="s1", status="ACTIVE", title="P",
        interaction_mode=parent_mode,
    )
    return ControlContext(
        session_id="s1", task_id="p1", agent_id="a1", task=parent,
        task_manager=tm, session=None, tool_call_id="tc",
    )


def test_auto_parent_downgrades_delegate_task() -> None:
    tm = _FakeTM()
    delegate_task(title="c", task_prompt="p", interactive=True, ctx=_ctx(tm, "auto"))
    assert tm.staged[0].interaction_mode == "auto"


def test_interactive_parent_keeps_delegate_task() -> None:
    tm = _FakeTM()
    delegate_task(title="c", task_prompt="p", interactive=True, ctx=_ctx(tm, "interactive"))
    assert tm.staged[0].interaction_mode == "interactive"


def test_auto_parent_downgrades_delegate_plan() -> None:
    tm = _FakeTM()
    delegate_plan(
        tasks=[{"title": "a", "interactive": True}, {"title": "b", "interactive": False}],
        ctx=_ctx(tm, "auto"),
    )
    assert [c.interaction_mode for c in tm.staged] == ["auto", "auto"]


def test_interactive_parent_keeps_delegate_plan() -> None:
    tm = _FakeTM()
    delegate_plan(
        tasks=[{"title": "a", "interactive": True}, {"title": "b", "interactive": False}],
        ctx=_ctx(tm, "interactive"),
    )
    assert [c.interaction_mode for c in tm.staged] == ["interactive", "auto"]


def test_auto_parent_downgrades_replan() -> None:
    tm = _FakeTM()
    replan(reason="changed", tasks=[{"title": "a", "interactive": True}], ctx=_ctx(tm, "auto"))
    assert tm.staged[0].interaction_mode == "auto"
