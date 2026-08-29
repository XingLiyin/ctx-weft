"""L0.5 降级 → blob 回收 → 取回：把这条链串起来跑。

缺陷记录：`docs/superpowers/specs/2026-08-27-l05-demotion-drops-blob-reference.md`

L0.5 把真图换成含 ref 的**文本**占位，而引用边（`memory_blob_refs`）只认结构化的
ref part（`extract_blob_refs` 判据 `_is_ref_part`）。于是补偿记录写入零条引用边、
原记录被 fold 标 superseded，该 sha 活引用归零 → 过宽限期被回收 →
`media:get_image` 再也取不回来。

L0.5 排在 L1/L2/L3 之前的第三条理由就是「可逆」，这条测试钉的正是那个可逆性。

本文件钉住的缺陷已于 2026-08-27 修复（补偿记录经 MemoryEvent.blob_refs 显式声明
降级掉的 ref，provider 的 mark 判据 collect_blob_refs 据此建引用边）。用例保留为
回归防线：任何让 ref 重新只存在于占位文本里的改动，都会让它转红。

**跨组件改写（2026-08-29）**：`SqlMemoryProvider` 不再兼当 blob store——引用边
（`memory_blob_refs`）留在 SQL，与 `ingest` 同事务；字节挪去了文件系统
（`FsBlobStore`）。这条测试原先测的是「SQL 自己存自己收」的单机简化路径，现在
测的是两个组件靠 ref 串起来的真实生产组合：mark（`mem.live_blob_refs()`）在 SQL
侧算，sweep（`blobs.collect(live_refs, now=)`）在 FS 侧执行，谁都不知道对方的
存在。
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

from ctx_weft.core.media import demote_for_budget
from ctx_weft.protocols import (
    ImagePart,
    MemoryAddress,
    MemoryEvent,
    MemoryKind,
    MemoryScope,
    ProviderContext,
    TextPart,
)
from ctx_weft.providers.blob.fs import FsBlobStore
from ctx_weft.providers.memory.sql import open_sqlite_memory

_RAW = b"\x89PNG\r\n\x1a\n" + b"payload" * 100
_KINDS = [MemoryKind.CONVERSATION_TURN]


def _addr() -> MemoryAddress:
    return MemoryAddress(session_id="ses_1", agent_id="agt_1", task_id="tsk_1")


async def _seed(blobs, mem, ctx, addr) -> str:
    """put 一张图（FS）+ ingest 一条引用它的记录（SQL）。返回 ref。"""
    ref = await blobs.put(_RAW, "image/png", ctx)
    await mem.ingest(
        MemoryEvent(
            kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK, address=addr,
            role="user", timestamp=datetime.now(timezone.utc),
            content=[
                TextPart(text="看这张图"),
                ImagePart(data=ref, media_type="image/png",
                          source_type="ref", byte_size=len(_RAW)),
            ],
        ),
        ctx,
    )
    return ref


# 宽限期设 0：把「时间是否已过」从实验里去掉，只考察引用判据。
# 真实部署是 24h，结论相同——只是延后一天发生。
def _open_blobs(tmp: str) -> FsBlobStore:
    return FsBlobStore(os.path.join(tmp, "blobs"), grace_period=timedelta(0))


async def _open_mem(tmp: str):
    return open_sqlite_memory(os.path.join(tmp, "m.db"))


async def test_live_reference_protects_blob_before_demotion() -> None:
    """对照组：没降级时活引用在，回收不动它。这条现在就该绿。"""
    with tempfile.TemporaryDirectory() as tmp:
        blobs = _open_blobs(tmp)
        async with await _open_mem(tmp) as mem:
            ctx = ProviderContext(session_id="ses_1", tenant_id="default")
            ref = await _seed(blobs, mem, ctx, _addr())

            assert await blobs.collect(await mem.live_blob_refs()) == 0, "有活引用，不该回收"
            assert await blobs.get(ref, ctx) is not None


async def test_demotion_leaves_placeholder_that_still_carries_the_ref() -> None:
    """对照组：降级本身是对的——占位落了库、ref 还在文本里。这条现在就该绿。

    与下面那条一起，把失败精确定位在「引用边」而不是「降级」或「占位格式」。
    """
    with tempfile.TemporaryDirectory() as tmp:
        blobs = _open_blobs(tmp)
        async with await _open_mem(tmp) as mem:
            ctx = ProviderContext(session_id="ses_1", tenant_id="default")
            addr = _addr()
            ref = await _seed(blobs, mem, ctx, addr)

            n = await demote_for_budget(mem, addr, ctx, keep_recent=0,
                                        scope=MemoryScope.TASK, kinds=_KINDS)
            assert n == 1

            recs = await mem.load_view(addr, MemoryScope.TASK, ctx, kinds=_KINDS)
            texts = [p.text for r in recs for p in (r.content or []) if hasattr(p, "text")]
            assert any(ref in t for t in texts), "占位里必须留着 ref，否则 get_image 无从定位"


async def test_demoted_image_survives_blob_collection() -> None:
    """L0.5 的可逆性：降级过的图，回收之后仍取得回来。

    这是 L0.5 排在所有折叠之前的第三条理由（子设计 §6「可逆」）所要求的性质。
    """
    with tempfile.TemporaryDirectory() as tmp:
        blobs = _open_blobs(tmp)
        async with await _open_mem(tmp) as mem:
            ctx = ProviderContext(session_id="ses_1", tenant_id="default")
            addr = _addr()
            ref = await _seed(blobs, mem, ctx, addr)

            await demote_for_budget(mem, addr, ctx, keep_recent=0,
                                    scope=MemoryScope.TASK, kinds=_KINDS)

            collected = await blobs.collect(await mem.live_blob_refs())
            assert collected == 0, (
                f"降级后的图仍被 {collected} 次回收——占位里的 ref 还指着它，"
                "它就不是无人引用的")

            got = await blobs.get(ref, ctx)
            assert got is not None, "media:get_image 要的字节没了，L0.5 不再可逆"
            assert got[0] == _RAW
