"""SqlMemoryProvider 的 blob **引用边**（字节已不在 RDBMS，见
docs/superpowers/specs/2026-08-29-providers-layout-and-sql-event-store-design.md §5）。

本 provider 只负责两件事：ingest 时把引用边与事件行写进同一个事务；把活引用吐出来
（live_blob_refs）供宿主喂给 FsBlobStore.collect() 做 mark-sweep 的 mark 输入。
字节的存取与回收都归 blob store，本文件不测那些。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from ctx_weft.protocols import (
    EventBlobStore,
    MemoryAddress,
    MemoryBlobStore,
    MemoryEvent,
    MemoryKind,
    MemoryScope,
    ProviderContext,
)
from ctx_weft.protocols.context import ImagePart, TextPart
from ctx_weft.providers.memory.sql import SqlMemoryProvider, open_sqlite_memory

_SHA_A = "a" * 64
_SHA_B = "b" * 64
_REF_A = f"blob:{_SHA_A}"
_REF_B = f"blob:{_SHA_B}"


@asynccontextmanager
async def _provider(tmp_path):
    async with open_sqlite_memory(tmp_path / "mem.db") as m:
        yield m


def _ctx(tenant: str = "default") -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id=tenant)


def _event(ref: str | None = None, *, blob_refs: list[str] | None = None) -> MemoryEvent:
    content = [TextPart(text="hi")]
    if ref is not None:
        content.append(ImagePart(data=ref, media_type="image/png", source_type="ref"))
    return MemoryEvent(
        kind=MemoryKind.CONVERSATION_TURN,
        scope=MemoryScope.TASK,
        address=MemoryAddress(session_id="s1", task_id="t1", agent_id="a1"),
        content=content,
        timestamp=datetime.now(UTC),
        role="user",
        blob_refs=list(blob_refs or []),
    )


async def test_provider_is_no_longer_a_blob_store(tmp_path):
    """字节离开 RDBMS：本 provider 不再实现任何 blob 协议（spec §5.2）。"""
    async with _provider(tmp_path) as m:
        assert not isinstance(m, MemoryBlobStore)
        assert not isinstance(m, EventBlobStore)
        assert not hasattr(m, "put")
        assert not hasattr(m, "collect_blobs")


async def test_live_blob_refs_collects_structured_ref_parts(tmp_path):
    async with _provider(tmp_path) as m:
        await m.ingest(_event(_REF_A), _ctx())
        assert await m.live_blob_refs() == {_REF_A}


async def test_live_blob_refs_collects_declared_refs(tmp_path):
    """L0.5 降级把 ImagePart(ref) 换成文本占位，ref 只能靠 blob_refs 声明传递。"""
    async with _provider(tmp_path) as m:
        await m.ingest(_event(None, blob_refs=[_REF_B]), _ctx())
        assert await m.live_blob_refs() == {_REF_B}


async def test_live_blob_refs_drops_superseded(tmp_path):
    """被 fold 掉的记录不再构成活引用——这正是 GC 的 mark 判据。"""
    async with _provider(tmp_path) as m:
        rid = await m.ingest(_event(_REF_A), _ctx())
        assert await m.live_blob_refs() == {_REF_A}
        await m.fold([rid], [], _ctx())
        assert await m.live_blob_refs() == set()


async def test_live_blob_refs_spans_all_tenants(tmp_path):
    """内容寻址天然跨租户共享一份字节；只看单租户会删掉别人还在用的图。"""
    async with _provider(tmp_path) as m:
        await m.ingest(_event(_REF_A), _ctx("tenant-a"))
        await m.ingest(_event(_REF_B), _ctx("tenant-b"))
        assert await m.live_blob_refs() == {_REF_A, _REF_B}


async def test_live_blob_refs_survives_partial_supersede(tmp_path):
    """同一 sha 被两条记录引用，fold 掉一条 → 仍是活引用。"""
    async with _provider(tmp_path) as m:
        rid1 = await m.ingest(_event(_REF_A), _ctx())
        await m.ingest(_event(_REF_A), _ctx())
        await m.fold([rid1], [], _ctx())
        assert await m.live_blob_refs() == {_REF_A}


async def test_live_blob_refs_empty_on_pure_text(tmp_path):
    async with _provider(tmp_path) as m:
        await m.ingest(_event(None), _ctx())
        assert await m.live_blob_refs() == set()
