"""`unattended` 跨事件 / 投影 / 序列化 / 反序列化 / 重建全链保值。

丢了它，一个后台自治任务在 resume 之后就变回「有人看顾」，随后第一次 HITL 会把它
park 到死——正是本字段要堵的那个洞。链路与 `test_interaction_mode_persistence.py`
同构：Task → task_payload(TASK_CREATED) → reducer(TaskView) → snapshot 往返 →
task_from_projection。
"""

from __future__ import annotations

from ctx_weft.core.control.converters import task_from_projection
from ctx_weft.core.control.reducers import (
    deserialize_view,
    reduce_events,
    serialize_view,
)
from ctx_weft.core.control.types import TaskView
from ctx_weft.core.models.task import Task
from ctx_weft.core.orchestrator.task.manager import task_payload
from ctx_weft.core.utils.clock import now_utc
from ctx_weft.core.utils.ids import generate_id
from ctx_weft.protocols.events import Event, EventType


def _unattended_task() -> Task:
    return Task(
        id="t1", session_id="s1", status="ACTIVE", title="Nightly",
        unattended=True, interaction_mode="auto",
    )


def _created_event(payload: dict) -> Event:
    return Event(
        id=generate_id("evt"), run_id=None, sequence=1, session_id="s1",
        type=EventType.TASK_CREATED, timestamp=now_utc(), tenant_id="default",
        task_id="t1", payload=payload,
    )


def test_task_payload_carries_unattended() -> None:
    payload = task_payload(_unattended_task(), user_prompt_jsonable=None)
    assert payload["task"]["unattended"] is True


def test_default_is_false_everywhere() -> None:
    assert Task(id="t", session_id="s", status="PENDING").unattended is False
    assert TaskView(id="t", session_id="s").unattended is False
    assert task_from_projection(TaskView(id="t", session_id="s")).unattended is False


def test_task_from_projection_preserves_unattended() -> None:
    proj = TaskView(id="t1", session_id="s1", unattended=True)
    assert task_from_projection(proj).unattended is True


def test_unattended_survives_reduce_and_snapshot_and_rebuild() -> None:
    ev = _created_event(task_payload(_unattended_task(), user_prompt_jsonable=None))
    view = reduce_events([ev], run_id="run1")
    assert view.tasks["t1"].unattended is True

    restored = deserialize_view(serialize_view(view))
    assert restored.tasks["t1"].unattended is True
    assert task_from_projection(restored.tasks["t1"]).unattended is True


def test_legacy_event_without_the_key_reduces_to_false() -> None:
    """存量事件流：payload 里压根没有 `unattended` 键 → False，且不炸。"""
    payload = task_payload(_unattended_task(), user_prompt_jsonable=None)
    payload["task"].pop("unattended")
    view = reduce_events([_created_event(payload)], run_id="run1")
    assert view.tasks["t1"].unattended is False


def test_legacy_snapshot_without_the_key_deserialises_to_false() -> None:
    """存量快照同理：deserialize_view 用 `.get(..., False)` 兼容。"""
    ev = _created_event(task_payload(_unattended_task(), user_prompt_jsonable=None))
    data = serialize_view(reduce_events([ev], run_id="run1"))
    data["tasks"]["t1"].pop("unattended")
    assert deserialize_view(data).tasks["t1"].unattended is False
