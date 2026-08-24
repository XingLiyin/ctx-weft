import pytest

from ctx_weft.core.runtime import ProviderRegistry
from ctx_weft.protocols import BLOB_REF_PREFIX, BlobStore, NullBlobStore, ProviderContext


def _ctx():
    return ProviderContext(session_id="s", tenant_id="tn")


def test_null_store_satisfies_protocol():
    """NullBlobStore 必须是 BlobStore 的具体实现（抽象方法全部实现，可实例化）。"""
    assert isinstance(NullBlobStore(), BlobStore)


@pytest.mark.asyncio
async def test_null_store_get_is_none():
    """get 对不存在的 ref 返回 None 且不抛——这是 spec §5.1 的硬性要求。"""
    assert await NullBlobStore().get("blob:whatever", _ctx()) is None


@pytest.mark.asyncio
async def test_null_store_put_raises():
    """Phase 1 无调用方；抛错可在 Phase 3 接线错误时立刻暴露。"""
    with pytest.raises(NotImplementedError):
        await NullBlobStore().put(b"x", "image/png", _ctx())


def test_registry_defaults_to_null_store():
    reg = ProviderRegistry()
    assert isinstance(reg.get_blob_store(), NullBlobStore)


def test_registry_returns_registered_store():
    class _Fake(BlobStore):
        async def put(self, data, media_type, ctx):
            return f"{BLOB_REF_PREFIX}fake"

        async def get(self, ref, ctx):
            return (b"x", "image/png")

    reg = ProviderRegistry()
    store = _Fake()
    reg.register_blob_store(store)
    assert reg.get_blob_store() is store


def test_blob_ref_prefix_value():
    assert BLOB_REF_PREFIX == "blob:"
