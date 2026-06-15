"""interaction_mode 跨事件投影 / 快照 / 恢复保留（否则 resume 后 interactive 任务退化为 auto）。

链路：Task → _task_payload(TASK_CREATED) → reducer(TaskView) → snapshot 往返 → task_from_projection。
"""

from __future__ import annotations

from loomex_core.core.control.converters import task_from_projection
from loomex_core.core.control.reducers import (
    deserialize_view,
    reduce_events,
    serialize_view,
)
from loomex_core.core.control.types import TaskView
from loomex_core.core.events.types import Event, EventType
from loomex_core.core.orchestrator.task_manager import _task_payload
from loomex_core.core.state.models import Task
from loomex_core.core.utils import generate_id, now_utc


def _interactive_task() -> Task:
    return Task(
        id="t1", session_id="s1", status="ACTIVE",
        title="Chat", interaction_mode="interactive",
    )


def test_task_payload_carries_interaction_mode() -> None:
    payload = _task_payload(_interactive_task())
    assert payload["task"]["interaction_mode"] == "interactive"


def test_task_from_projection_preserves_interaction_mode() -> None:
    proj = TaskView(id="t1", session_id="s1", interaction_mode="interactive")
    assert task_from_projection(proj).interaction_mode == "interactive"


def test_default_interaction_mode_is_auto_through_projection() -> None:
    proj = TaskView(id="t2", session_id="s1")
    assert proj.interaction_mode == "auto"
    assert task_from_projection(proj).interaction_mode == "auto"


def test_interaction_mode_survives_reduce_and_snapshot() -> None:
    ev = Event(
        id=generate_id("evt"), run_id=None, sequence=1, session_id="s1",
        type=EventType.TASK_CREATED, timestamp=now_utc(), tenant_id="default",
        task_id="t1", payload=_task_payload(_interactive_task()),
    )
    view = reduce_events([ev], run_id="run1")
    assert view.tasks["t1"].interaction_mode == "interactive"

    # snapshot 往返
    restored = deserialize_view(serialize_view(view))
    assert restored.tasks["t1"].interaction_mode == "interactive"
