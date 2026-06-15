"""update_task_metadata must NOT flip actor_done (it now runs against the shared root task)."""

from __future__ import annotations

from types import SimpleNamespace

from loomex_core.core.orchestrator.control_capability import update_task_metadata, ControlContext
from loomex_core.core.state.models import NormalTaskSettings, Task


def _root_task():
    return Task(
        id="root1",
        session_id="s1",
        status="ACTIVE",
        tenant_id="default",
        title="",
        user_prompt="do the thing",
        settings=NormalTaskSettings(),
    )


def test_update_task_metadata_sets_title_but_not_actor_done():
    task = _root_task()
    assert task.actor_done is False
    ctx = ControlContext(
        session_id="s1", task_id="root1", agent_id="a1",
        task=task, task_manager=None, session=SimpleNamespace(goal=""),
    )
    result = update_task_metadata(title="My Title", description="My Desc", session_goal="", ctx=ctx)
    assert task.title == "My Title"
    assert task.description == "My Desc"
    assert task.actor_done is False  # MUST NOT be flipped — would prematurely finish the root task
