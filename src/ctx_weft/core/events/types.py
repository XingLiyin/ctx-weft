"""事件类型的 re-export + core 侧的投影映射。

定义已搬到 `ctx_weft.protocols.events`（spec 2026-08-27 协议层划界）。本模块保留
是为了让 130+ 个既有 import 一个都不用改；新代码请直接从 protocols 导入。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ctx_weft.protocols.events import (
    EVENT_TYPES,
    TRANSIENT_EVENT_TYPES,
    Event,
    EventFilter,
    EventType,
)

if TYPE_CHECKING:
    from ctx_weft.core.state.models import TaskStatus

__all__ = [
    "EVENT_TYPES",
    "TRANSIENT_EVENT_TYPES",
    "Event",
    "EventFilter",
    "EventType",
    "TASK_STATUS_BY_EVENT",
]


# 留在 core 的理由：只被 `core/control/reducers.py` 消费，且值类型 `TaskStatus` 来自
# `core/state/models.py`——它是 core 的投影逻辑，不是 host 要按之编程的契约。
TASK_STATUS_BY_EVENT: dict[EventType, "TaskStatus"] = {
    EventType.TASK_STARTED: "ACTIVE",
    EventType.TASK_SUSPENDED: "SUSPENDED",
    EventType.TASK_FINISHED: "FINISHED",
    EventType.TASK_FAILED: "FAILED",
    EventType.TASK_CANCELED: "CANCELED",
    EventType.TASK_RESUMED: "ACTIVE",
    EventType.TASK_REQUEUED: "PENDING",
}
