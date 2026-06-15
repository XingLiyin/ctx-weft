"""finish_task：actor 主动提交 task 结果并结束 act，路由到 observe。

锁定：
- 写 task.outputs = result，置 actor_done=True
- 不置 SUSPENDED（区别于 delegate_task/delegate_plan 的委派挂起）→ act 退出后走 observe
- 是 act-purpose 的控制工具
"""

from __future__ import annotations

from ctx_weft.core.orchestrator.control_capability import (
    ControlContext,
    finish_task,
    _CONTROL_TOOLS,
)
from ctx_weft.core.state.models import Task


def _ctx(task: Task) -> ControlContext:
    return ControlContext(
        session_id="s1", task_id=task.id, agent_id="a1", task=task,
        task_manager=None, session=None, tool_call_id="tc_1",
    )


def test_finish_task_sets_outputs_and_actor_done() -> None:
    task = Task(id="t1", session_id="s1", status="ACTIVE", title="T")
    finish_task(result="the final answer", ctx=_ctx(task))
    assert task.outputs == "the final answer"
    assert task.actor_done is True
    assert task.status != "SUSPENDED"  # 走 observe，不走 suspend


def test_finish_task_is_act_purpose_tool() -> None:
    cap, _ = _CONTROL_TOOLS["finish_task"]
    assert "act" in cap.purposes
    assert cap.name == "finish_task"
