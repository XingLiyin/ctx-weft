"""`port_key` 跨事件 / 投影 / 序列化 / 反序列化 / 重建全链保值，外加存量数据的迁移映射。

丢了它，resume 之后一条旁支交互线就并回主口、与主线互斥——两边都在等各自的对端，
却只有一个能被派发。链路与 `test_task_unattended_persistence.py` 同构：
Task → task_payload(TASK_CREATED) → reducer(TaskView) → snapshot 往返 →
task_from_projection。

**迁移映射**（`default_port_for`）是本文件的另一半：存量数据只有 `unattended`，
没有 `port_key`。映射是 ``unattended=True → PORT_NONE`` / ``False → PORT_MAIN``，
双向无损——如果这两个概念不是同一根轴的两段，这里必然会丢信息。
"""

from __future__ import annotations

import pytest

from ctx_weft.core.control.converters import task_from_projection
from ctx_weft.core.control.reducers import (
    deserialize_view,
    reduce_events,
    serialize_view,
)
from ctx_weft.core.control.types import TaskView
from ctx_weft.core.models.task import PORT_MAIN, PORT_NONE, Task, default_port_for
from ctx_weft.core.orchestrator.task.manager import task_payload
from ctx_weft.core.utils.clock import now_utc
from ctx_weft.core.utils.ids import generate_id
from ctx_weft.protocols.events import Event, EventType


def _task(**kw) -> Task:
    return Task(id="t1", session_id="s1", status="ACTIVE", title="Ask aside", **kw)


def _created_event(payload: dict) -> Event:
    return Event(
        id=generate_id("evt"), run_id=None, sequence=1, session_id="s1",
        type=EventType.TASK_CREATED, timestamp=now_utc(), tenant_id="default",
        task_id="t1", payload=payload,
    )


def test_task_payload_carries_port_key() -> None:
    payload = task_payload(_task(port_key="btw"), user_prompt_jsonable=None)
    assert payload["task"]["port_key"] == "btw"


def test_default_is_main_everywhere() -> None:
    assert Task(id="t", session_id="s", status="PENDING").port_key == PORT_MAIN
    assert TaskView(id="t", session_id="s").port_key == PORT_MAIN
    assert task_from_projection(TaskView(id="t", session_id="s")).port_key == PORT_MAIN


def test_task_from_projection_preserves_port_key() -> None:
    proj = TaskView(id="t1", session_id="s1", port_key="btw")
    assert task_from_projection(proj).port_key == "btw"


def test_port_key_survives_reduce_and_snapshot_and_rebuild() -> None:
    ev = _created_event(task_payload(_task(port_key="btw"), user_prompt_jsonable=None))
    view = reduce_events([ev], run_id="run1")
    assert view.tasks["t1"].port_key == "btw"

    restored = deserialize_view(serialize_view(view))
    assert restored.tasks["t1"].port_key == "btw"
    assert task_from_projection(restored.tasks["t1"]).port_key == "btw"


def test_attached_but_autonomous_survives_the_whole_chain() -> None:
    """「接口但自治」那一格必须能过全链——它是两个字段共存的全部理由。

    合并成单字段的话，这一格在序列化那一步就会塌成「完全自治」。
    """
    ev = _created_event(task_payload(
        _task(port_key=PORT_MAIN, unattended=True), user_prompt_jsonable=None,
    ))
    restored = deserialize_view(serialize_view(reduce_events([ev], run_id="run1")))
    t = task_from_projection(restored.tasks["t1"])
    assert (t.port_key, t.unattended) == (PORT_MAIN, True)


# ── 存量数据迁移 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("unattended,expected", [(True, PORT_NONE), (False, PORT_MAIN)])
def test_default_port_for_is_the_single_migration_formula(unattended, expected) -> None:
    assert default_port_for(unattended) == expected


@pytest.mark.parametrize("unattended,expected", [(True, PORT_NONE), (False, PORT_MAIN)])
def test_legacy_event_without_port_key_falls_back_by_unattended(unattended, expected) -> None:
    """存量事件流：payload 里压根没有 `port_key` 键 → 按 `unattended` 回落，且不炸。"""
    payload = task_payload(_task(unattended=unattended), user_prompt_jsonable=None)
    payload["task"].pop("port_key")
    view = reduce_events([_created_event(payload)], run_id="run1")
    assert view.tasks["t1"].port_key == expected
    # 双射的另一半：回落之后 `unattended` 与老字段逐字相等，没有信息丢失。
    assert view.tasks["t1"].unattended is unattended


@pytest.mark.parametrize("unattended,expected", [(True, PORT_NONE), (False, PORT_MAIN)])
def test_legacy_snapshot_without_port_key_falls_back_by_unattended(unattended, expected) -> None:
    """存量快照同理：`deserialize_view` 与事件流重建共用同一条公式。"""
    ev = _created_event(task_payload(_task(unattended=unattended), user_prompt_jsonable=None))
    data = serialize_view(reduce_events([ev], run_id="run1"))
    data["tasks"]["t1"].pop("port_key")
    restored = deserialize_view(data)
    assert restored.tasks["t1"].port_key == expected
    assert restored.tasks["t1"].unattended is unattended
