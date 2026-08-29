"""SQL-backed MemoryProvider（SQLAlchemy async；默认 SQLite，postgres 同源）。

**可选依赖**：本子包需要 ``sqlalchemy>=2.0``（SQLite 后端另需 ``aiosqlite``），
装法 ``pip install ctx-weft[sql]``。它不在 ctx-weft 的基础依赖里，
也**没有**任何上层包 eager import 它——``import ctx_weft`` /
``import ctx_weft.providers`` 在缺 sqlalchemy 时照常工作，只有显式
``import ctx_weft.providers.memory.sql`` 才会（如实地）报 ImportError。
"""

from ctx_weft.providers.memory.sql.models import (
    Base,
    MemoryBlobModel,
    MemoryBlobRefModel,
    MemoryEventModel,
    MemorySubscriptionModel,
)
from ctx_weft.providers.memory.sql.provider import (
    SqlMemoryProvider,
    make_session_factory,
    normalize_tenant,
    open_sqlite_memory,
)

__all__ = [
    "Base",
    "MemoryBlobModel",
    "MemoryBlobRefModel",
    "MemoryEventModel",
    "MemorySubscriptionModel",
    "SqlMemoryProvider",
    "make_session_factory",
    "normalize_tenant",
    "open_sqlite_memory",
]
