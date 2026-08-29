"""MemoryProvider 的实现们：`in_memory`（纯内存）与 `sql`（SQLAlchemy async）。

**本模块只 re-export 无可选依赖的实现。** `sql` 子包需要 ``sqlalchemy>=2.0``
（SQLite 后端另需 ``aiosqlite``），装法 ``pip install ctx-weft[sql]``，
故**刻意不在这里 re-export、也不做 PEP 562 惰性 `__getattr__`**——让路径本身说明
依赖边界：``from ctx_weft.providers.memory.sql import SqlMemoryProvider`` 一眼看出
要 extras。惰性绑定会让 ``providers.memory.SqlMemoryProvider`` 这个名字看起来存在、
访问时才炸。

不变量：`import ctx_weft.providers.memory` 在缺 sqlalchemy 时照常工作。
"""

from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider

__all__ = ["InMemoryMemoryProvider"]
