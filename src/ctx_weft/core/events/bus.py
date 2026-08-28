"""EventBus 的 re-export 兼容层。

协议已搬到 `ctx_weft.protocols.events`、实现已搬到 `ctx_weft.providers.events`
（spec 2026-08-27 三层划界）。本模块保留只为让既有 import 不断裂；新代码请直接从
protocols / providers 导入。
"""

from __future__ import annotations

from ctx_weft.protocols.events import Event, EventBus, EventFilter, SubscriptionHandle
from ctx_weft.providers.events.bus import InProcessEventBus

__all__ = ["Event", "EventBus", "EventFilter", "SubscriptionHandle", "InProcessEventBus"]
