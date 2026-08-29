from datetime import UTC, datetime, timedelta

import pytest
from ctx_weft.protocols import (
    BLOB_REF_PREFIX, EventBlobStore, MemoryBlobStore, ProviderContext,
)
from ctx_weft.providers.blob.fs import FsBlobStore


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


# ── review 追加：collect 覆盖 + get 对损坏 sidecar 的容错 ──────────────────────


@pytest.mark.asyncio
async def test_get_survives_corrupted_meta_sidecar(tmp_path):
    """.meta 被截断/篡改成非 UTF-8 字节时，get() 必须回落而不是抛 UnicodeDecodeError。

    真实触发场景：磁盘故障、进程崩溃写到一半、外部篡改——这些都属于契约里
    「形态不对」的范畴，不该让 get() 违反「绝不 raise」。
    """
    store = FsBlobStore(tmp_path)
    ref = await store.put(b"payload", "image/png", _ctx())
    sha = ref[len(BLOB_REF_PREFIX):]
    meta_path = tmp_path / sha[:2] / f"{sha}.meta"
    meta_path.write_bytes(b"\xff\xfe\x00not-utf8")
    assert await store.get(ref, _ctx()) == (b"payload", "application/octet-stream")


@pytest.mark.asyncio
async def test_collect_accepts_naive_now(tmp_path):
    """naive datetime（无 tzinfo）传给 now 不应 raise TypeError——不能要求调用方记得带时区。

    naive 值按 UTC 面值归一（而不是按本机时区转换），所以这里用
    ``datetime.now(UTC)`` 剥掉 tzinfo 构造，而不是 ``datetime.now()``（本机时区
    wall time，若本机不在 UTC 会让断言依赖本机时区偏移，是构造用例的错误，不是
    实现的错误）。
    """
    store = FsBlobStore(tmp_path, grace_period=timedelta(seconds=0))
    await store.put(b"stale", "image/png", _ctx())
    naive_future = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=1)
    assert await store.collect(set(), now=naive_future) == 1


@pytest.mark.asyncio
async def test_collect_keeps_blob_within_grace_period(tmp_path):
    """宽限期未到：即便不在 live_refs 中也不删——保护 put→ingest 之间的窗口。"""
    store = FsBlobStore(tmp_path, grace_period=timedelta(hours=24))
    ref = await store.put(b"fresh", "image/png", _ctx())
    assert await store.collect(set()) == 0
    assert await store.get(ref, _ctx()) == (b"fresh", "image/png")


@pytest.mark.asyncio
async def test_collect_deletes_expired_unreferenced_blob(tmp_path):
    """宽限期已过且不在 live_refs 中：删除，并返回删除计数。"""
    store = FsBlobStore(tmp_path, grace_period=timedelta(seconds=0))
    ref = await store.put(b"orphan", "image/png", _ctx())
    now = datetime.now(UTC) + timedelta(seconds=1)
    assert await store.collect(set(), now=now) == 1
    assert await store.get(ref, _ctx()) is None


@pytest.mark.asyncio
async def test_collect_keeps_expired_but_referenced_blob(tmp_path):
    """宽限期已过但在 live_refs 中：不删。"""
    store = FsBlobStore(tmp_path, grace_period=timedelta(seconds=0))
    ref = await store.put(b"still-used", "image/png", _ctx())
    now = datetime.now(UTC) + timedelta(seconds=1)
    assert await store.collect({ref}, now=now) == 0
    assert await store.get(ref, _ctx()) == (b"still-used", "image/png")
