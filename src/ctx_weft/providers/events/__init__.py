"""event 体系的内置实现。

目录按**协议**分层，而不是把所有实现平铺：`events/` 下装着两个不同的协议——
`EventBus`（传输/扇出）与 `EventStore`（持久化）。不分层就会把 `in_process`
（一个 bus）与 `in_memory` / `sql`（两个 store）摆成平级三兄弟，读起来像同一个
东西的三个变体。

    bus/in_process/     ← InProcessEventBus
    store/in_memory/    ← InMemoryEventStore
    store/sql/          ← SqlEventStore（需 ctx-weft[sql]，故不在此 re-export）

本模块只 re-export 无可选依赖的名字。
"""

from ctx_weft.providers.events.bus import InProcessEventBus
from ctx_weft.providers.events.persister import (
    EventPersister,
    PersistenceHandle,
    attach_persistence,
)
from ctx_weft.providers.events.snapshot import SnapshotWriter
from ctx_weft.providers.events.store import InMemoryEventStore

__all__ = [
    "EventPersister",
    "InMemoryEventStore",
    "InProcessEventBus",
    "PersistenceHandle",
    "SnapshotWriter",
    "attach_persistence",
]
