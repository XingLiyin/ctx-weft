"""EventStore 协议的实现们：`in_memory`（开发/测试）与 `sql`（持久化）。

**本模块只 re-export 无可选依赖的实现**——`sql` 子包需要 ``ctx-weft[sql]``，
理由与 `providers/memory/__init__.py` 同：让路径本身说明依赖边界。
"""

from ctx_weft.providers.events.store.in_memory import InMemoryEventStore

__all__ = ["InMemoryEventStore"]
