import pytest

from ctx_weft.core.runtime import ProviderRegistry
from ctx_weft.protocols import BLOB_REF_PREFIX, MemoryBlobStore, NullMemoryBlobStore, ProviderContext


def _ctx():
    return ProviderContext(session_id="s", tenant_id="tn")


def test_null_store_satisfies_protocol():
    """NullMemoryBlobStore 必须是 MemoryBlobStore 的具体实现（抽象方法全部实现，可实例化）。"""
    assert isinstance(NullMemoryBlobStore(), MemoryBlobStore)


@pytest.mark.asyncio
async def test_null_store_get_is_none():
    """get 对不存在的 ref 返回 None 且不抛——这是 spec §5.1 的硬性要求。"""
    assert await NullMemoryBlobStore().get("blob:whatever", _ctx()) is None


@pytest.mark.asyncio
async def test_null_store_put_raises():
    """Phase 1 无调用方；抛错可在 Phase 3 接线错误时立刻暴露。"""
    with pytest.raises(NotImplementedError):
        await NullMemoryBlobStore().put(b"x", "image/png", _ctx())


def test_registry_defaults_to_null_store():
    reg = ProviderRegistry()
    assert isinstance(reg.get_memory_blob_store(), NullMemoryBlobStore)


def test_registry_returns_registered_store():
    class _Fake(MemoryBlobStore):
        async def put(self, data, media_type, ctx):
            return f"{BLOB_REF_PREFIX}fake"

        async def get(self, ref, ctx):
            return (b"x", "image/png")

    reg = ProviderRegistry()
    store = _Fake()
    reg.register_memory_blob_store(store)
    assert reg.get_memory_blob_store() is store


def test_blob_ref_prefix_value():
    assert BLOB_REF_PREFIX == "blob:"


async def test_memory_blob_store_does_not_auto_resolve_from_memory_provider():
    """两个 blob store 的解析规则对称：都只有「显式注册 > Null」两级（spec §5.3）。

    曾经 memory 侧有第三级——memory provider 若自己实现了 MemoryBlobStore 就直接用它。
    那一级的唯一服务对象是 SqlMemoryProvider 的字节存储，字节移出 RDBMS 后无对象可服务。
    """
    from ctx_weft.protocols import MemoryBlobStore, NullMemoryBlobStore
    from ctx_weft.core.runtime import ProviderRegistry

    class _MemoryThatIsAlsoBlobStore(MemoryBlobStore):
        name = "fake"

        async def put(self, data, media_type, ctx):
            return "blob:" + "0" * 64

        async def get(self, ref, ctx):
            return None

    reg = ProviderRegistry()
    reg.register_memory(_MemoryThatIsAlsoBlobStore())
    assert isinstance(reg.get_memory_blob_store(), NullMemoryBlobStore)


async def test_memory_blob_store_returns_explicit_registration():
    from ctx_weft.core.runtime import ProviderRegistry
    from ctx_weft.providers.blob.fs import FsBlobStore
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        store = FsBlobStore(Path(d))
        reg = ProviderRegistry()
        reg.register_memory_blob_store(store)
        assert reg.get_memory_blob_store() is store
