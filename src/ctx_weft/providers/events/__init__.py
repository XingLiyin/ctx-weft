"""event 体系的内置实现（bus + store）。

与 `providers/llm/` 同构：领域一个目录，内部按变体分文件。将来要加 Redis Streams
的 bus 或 Postgres 的 store，加文件即可，不必再动 core。
"""

from ctx_weft.providers.events.bus import InProcessEventBus
from ctx_weft.providers.events.store import InMemoryEventStore

__all__ = ["InProcessEventBus", "InMemoryEventStore"]
