"""纯内存 MemoryProvider——测试 / 单进程 demo 用。

**不实现 `MemoryBlobStore`**（裁定 D6）：不接 blob 的宿主行为与改造前逐字节一致。
"""

from ctx_weft.providers.memory.in_memory.provider import InMemoryMemoryProvider

__all__ = ["InMemoryMemoryProvider"]
