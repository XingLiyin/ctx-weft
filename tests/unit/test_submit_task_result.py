"""finish_task：actor 主动结束 act，路由到 observe（反转契约 spec 2026-07-01）。

锁定：
- 只置 actor_done=True，**不**写 task.outputs（outputs 由 ActStep 收尾时合成：正文 + 可选
  deliverables_summary）
- 不置 SUSPENDED（区别于 delegate_task/delegate_plan 的委派挂起）→ act 退出后走 observe
- 是 act-purpose 的控制工具
"""

from __future__ import annotations

from ctx_weft.core.capabilities.control_tools import (
    ControlContext,
    finish_task,
    _CONTROL_TOOLS,
)
from ctx_weft.core.models.task import Task


def _ctx(task: Task) -> ControlContext:
    return ControlContext(
        session_id="s1", task_id=task.id, agent_id="a1", task=task,
        task_manager=None, session=None, tool_call_id="tc_1",
    )


def test_finish_task_sets_actor_done_not_outputs() -> None:
    task = Task(id="t1", session_id="s1", status="ACTIVE", title="T")
    finish_task(deliverables_summary="改了 a.py、b.py", ctx=_ctx(task))
    # 反转契约：finish_task 不再写 outputs（由 ActStep 合成正文+小结）
    assert task.outputs is None
    assert task.actor_done is True
    assert task.status != "SUSPENDED"  # 走 observe，不走 suspend


def test_finish_task_is_act_purpose_tool() -> None:
    cap, _ = _CONTROL_TOOLS["finish_task"]
    assert "act" in cap.purposes
    assert cap.name == "finish_task"
