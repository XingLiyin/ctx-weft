import pytest
from ctx_weft.protocols import (
    BLOB_REF_PREFIX, EventBlobStore, MemoryBlobStore, ProviderContext,
)
from ctx_weft.providers.blob_fs import FsBlobStore


def _ctx():
    return ProviderContext(session_id="s1")


def test_one_class_satisfies_both_contracts(tmp_path):
    """两个契约独立定义、形状相似 → 实现时可以偷懒，一个类同时满足、注册两次。"""
    store = FsBlobStore(tmp_path)
    assert isinstance(store, MemoryBlobStore)
    assert isinstance(store, EventBlobStore)
    assert store.can_externalize is True


@pytest.mark.asyncio
async def test_put_is_content_addressed_and_idempotent(tmp_path):
    store = FsBlobStore(tmp_path)
    ref1 = await store.put(b"same-bytes", "image/png", _ctx())
    ref2 = await store.put(b"same-bytes", "image/png", _ctx())
    assert ref1 == ref2
    assert ref1.startswith(BLOB_REF_PREFIX)
    assert await store.get(ref1, _ctx()) == (b"same-bytes", "image/png")


@pytest.mark.asyncio
async def test_get_returns_none_and_never_raises(tmp_path):
    store = FsBlobStore(tmp_path)
    assert await store.get(f"{BLOB_REF_PREFIX}deadbeef", _ctx()) is None
    assert await store.get("http://example.com/x.png", _ctx()) is None   # 非 blob: 前缀
    assert await store.get(BLOB_REF_PREFIX, _ctx()) is None              # 空 sha
    assert await store.get(f"{BLOB_REF_PREFIX}../../etc/passwd", _ctx()) is None


@pytest.mark.asyncio
async def test_separate_instances_are_truly_independent(tmp_path):
    """分开部署时两个实例互不可见——core 不得依赖任何一侧解得开对方的 ref。"""
    mem = FsBlobStore(tmp_path / "mem")
    evt = FsBlobStore(tmp_path / "evt")
    ref = await mem.put(b"only-in-memory", "image/png", _ctx())
    assert await evt.get(ref, _ctx()) is None
