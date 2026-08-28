"""EventBlobStore：事件流侧独立的 blob 协议。

spec: docs/superpowers/specs/2026-08-27-dual-blob-store-design.md

与 MemoryBlobStore 同形但**类型无关**——两侧语义会各自演进（最明显的是回收锚点不同：
memory 侧是记录 is_superseded，event 侧是事件保留策略）。host 要共用就一个类同时实现两者。
"""

from __future__ import annotations

import hashlib

import pytest

from ctx_weft.core.runtime import ProviderRegistry
from ctx_weft.protocols import ProviderContext
from ctx_weft.protocols.events import EventBlobStore, NullEventBlobStore
from ctx_weft.protocols.memory import MemoryBlobStore


def _ctx() -> ProviderContext:
    return ProviderContext(session_id="ses_1", tenant_id="default")


class _Stub(EventBlobStore):
    """最小实现，验协议可被继承。"""

    def __init__(self) -> None:
        self.blobs: dict[str, tuple[bytes, str]] = {}

    async def put(self, data: bytes, media_type: str, ctx: ProviderContext) -> str:
        ref = f"blob:{hashlib.sha256(data).hexdigest()}"
        self.blobs[ref] = (data, media_type)
        return ref

    async def get(self, ref: str, ctx: ProviderContext):
        return self.blobs.get(ref)


def test_null_store_cannot_externalize() -> None:
    assert NullEventBlobStore().can_externalize is False


def test_stub_can_externalize_by_default() -> None:
    """基类默认 True——既有实现无需改动就是「能存」。"""
    assert _Stub().can_externalize is True


async def test_null_store_get_returns_none_never_raises() -> None:
    """get 对不存在的 ref 恒返 None 不抛：取图失败绝不能中断 loop。"""
    assert await NullEventBlobStore().get("blob:nope", _ctx()) is None


async def test_null_store_put_raises_loudly() -> None:
    """put 刻意抛错：调用方应先探询 can_externalize，而不是调用后捕异常。

    把「响亮失败」降级成控制流，会让真正的接线错误也被静默吞掉。
    """
    with pytest.raises(NotImplementedError):
        await NullEventBlobStore().put(b"x", "image/png", _ctx())


def test_is_independent_of_memory_blob_store() -> None:
    """同形但**类型无关**——不是子类型，也不共用一个 ABC（spec §3）。"""
    assert not issubclass(EventBlobStore, MemoryBlobStore)
    assert not issubclass(MemoryBlobStore, EventBlobStore)


def test_one_class_can_implement_both() -> None:
    """host 要共用就一个类同时继承两者——这是「可分可合」的合的那一半。"""

    class Both(MemoryBlobStore, EventBlobStore):
        async def put(self, data: bytes, media_type: str, ctx: ProviderContext) -> str:
            return "blob:x"

        async def get(self, ref: str, ctx: ProviderContext):
            return None

    both = Both()
    assert isinstance(both, MemoryBlobStore)
    assert isinstance(both, EventBlobStore)


def test_registry_explicit_registration() -> None:
    reg = ProviderRegistry()
    assert reg.get_event_blob_store().can_externalize is False  # 默认 Null
    stub = _Stub()
    reg.register_event_blob_store(stub)
    assert reg.get_event_blob_store() is stub


def test_registry_does_not_fall_back_to_memory_provider() -> None:
    """**刻意不自动回落**（spec §4）：自动解析会让「共用」成为隐式默认，
    而本设计的出发点正是让两者可分。host 要共用就把同一个实例注册两次。
    """

    class BothProvider(MemoryBlobStore, EventBlobStore):
        name = "both"

        async def put(self, data: bytes, media_type: str, ctx: ProviderContext) -> str:
            return "blob:x"

        async def get(self, ref: str, ctx: ProviderContext):
            return None

    reg = ProviderRegistry()
    reg.register_memory_blob_store(BothProvider())
    # memory 侧拿得到，event 侧仍是 Null——不串门
    assert reg.get_memory_blob_store().can_externalize is True
    assert reg.get_event_blob_store().can_externalize is False
