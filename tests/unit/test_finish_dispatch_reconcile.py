"""Delegation remains blocking and cannot be detached by a completion tool."""
from types import SimpleNamespace
from ctx_weft.core.capabilities.control_tools import ControlContext, delegate_task, _CONTROL_TOOLS
from ctx_weft.core.models.task import Task


def test_delegation_keeps_the_child_attached_and_requests_suspension():
    staged = []
    manager = SimpleNamespace(stage_task=lambda task, **kwargs: staged.append((task, kwargs)))
    task = Task(id="parent", session_id="s", status="ACTIVE", assigned_agent_id="a", creator_agent_id="a")
    ctx = ControlContext(session_id="s", task_id=task.id, agent_id="a", task=task,
                         task_manager=manager, session=None, tool_call_id="delegate")
    delegate_task(title="Child", task_prompt="Do the child work", ctx=ctx)
    child, options = staged[0]
    assert child.parent_task_id == task.id
    assert options["parent_task_id"] == task.id
    assert task.suspend_requested and task.actor_done
    assert "control:finish_task" not in _CONTROL_TOOLS
