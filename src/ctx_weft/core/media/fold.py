"""L0.5 降级的**执行**：读视图 → 把真图换成含 ref 的文本占位 → `memory.fold()`（子设计 §4.1）。

选取策略（哪些该降、哪些必须同批重写）在 `policy.py`，纯函数；本模块只做 IO 与重建。

**与 per-purpose 降级的区别**（台账「关键区分」表）：`content.py::downgrade_images_to_text`
产出的是**不落库**的瞬时占位，只影响本次 prompt，占位不可回读；本模块**重写 memory 记录**、
占位含完整 ref，模型之后能靠 `media:get_image` 取回。两者并存、互不替代。

════════════════════════════════════════════════════════════════════════════
落库方式：`memory.fold([id], [新事件])`，补偿记录必须**不带 id**
════════════════════════════════════════════════════════════════════════════

`fold` 是原子「遗忘 + 补偿」，不需要新增 `MemoryProvider` 方法。但补偿事件的
``id`` 必须留空：record-id 契约是「已存在的 id（**含已 superseded**）= no-op」
（`protocols/memory.py`，provider 实测 `in_memory.py:154`）。若照抄原 id，那一次
`fold` 就成了「把原记录标成 superseded，补偿被当重放丢弃」——**整条记录连同图片一起
消失**，且返回值看起来一切正常。

⚠️ **降级会换掉 record id**。调用方若在降级**之前**采集过 id 集（L1/L3 的折叠范围就是
这么来的，见子设计 §6.1），必须在 `demote_all` 之后**重新 `load_view`**：拿旧 id 去
`fold`，被降级过的那几条一条都 supersede 不掉，于是同一段对话在视图里出现两次
（旧的已被本模块 supersede，但**新的**补偿记录还活着，而摘要又已按旧内容生成）。
同理，调用方手里那份「降级前」的 records 也已过期——用它算摘要 / 取 `original` 节，
拿到的仍是真图，`content_to_text` 会把它们静默拍扁，§6.1 的目的完全落空。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import Any

from ctx_weft.core.media.policy import DemotionPlan, demotable_ref, plan_demotions
from ctx_weft.core.media.refs import encode_image_placeholder
from ctx_weft.protocols import MemoryEvent, MemoryScope, TextPart

logger = logging.getLogger(__name__)

__all__ = ["demote_all", "demote_for_budget"]


def _media_type(part: Any) -> str:
    if isinstance(part, dict):
        return str(part.get("media_type", "") or "")
    return str(getattr(part, "media_type", "") or "")


def _rebuild(rec: Any, indices: Sequence[int], scope: MemoryScope) -> MemoryEvent | None:
    """按 record 造补偿事件；``indices`` 指定的 part 换成占位。不可重建时返回 ``None``。

    字段逐一照抄（timestamp 保住位置、role/topic/metadata 保住语义），只有 ``id`` 留空
    （见模块 docstring）。``causation_id`` 不在 `MemoryRecord` 上，无从照抄——L0.5 降级
    会丢这一个字段，记在此处以免日后当成 provider 的 bug 去查。
    ``blob_refs`` 是**累积**的（旧的 + 本次降级的），不是照抄——见字段本身的注释。

    重建失败（`MemoryEvent.__post_init__` 的 v2 全址不变量不满足、address 缺失等）返回
    ``None`` 而不是抛：这条记录不降级，图还在、还能被取回，比让整级 compact 崩掉好。
    """
    address = getattr(rec, "address", None)
    if address is None:
        return None
    content = getattr(rec, "content", None)
    if content is None:                # content=None 造不出 MemoryEvent（__post_init__ 拒）
        return None
    demoted: list[str] = []
    if indices:
        new = list(content)
        for i in indices:
            ref = demotable_ref(content[i])
            if ref is None:            # 计划与内容对不上（不该发生）→ 整条放弃，别写坏占位
                return None
            new[i] = TextPart(text=encode_image_placeholder(ref, _media_type(content[i])))
            demoted.append(ref)
        content = new
    # 降级掉的 ref 必须显式声明（缺陷 2026-08-27）：占位是 TextPart，provider 的
    # mark 判据只看结构化字段，扫不出文本里的 ref。不声明 → 引用归零 → 字节过宽限期
    # 被回收 → media:get_image 取不回，L0.5 的「可逆」失效。
    # 累积而非覆盖：本记录可能已被降级过一轮，那一轮的 ref 只在它的 blob_refs 里。
    prior = list(getattr(rec, "blob_refs", None) or ())
    blob_refs = list(dict.fromkeys([*prior, *demoted]))
    try:
        return MemoryEvent(
            type=getattr(rec, "type", None),
            kind=getattr(rec, "kind", None),
            scope=getattr(rec, "scope", None) or scope,
            address=address,
            content=content,
            timestamp=getattr(rec, "timestamp", None),
            role=getattr(rec, "role", None),
            topic=getattr(rec, "topic", None),
            metadata=dict(getattr(rec, "metadata", None) or {}),
            blob_refs=blob_refs,
        )
    except (ValueError, TypeError):
        logger.warning("L0.5 降级跳过一条记录：无法由 MemoryRecord 重建 MemoryEvent "
                       "(id=%s)", getattr(rec, "id", None), exc_info=True)
        return None


async def _execute(memory: Any, records: Sequence[Any], plan: DemotionPlan,
                   scope: MemoryScope, ctx: Any) -> int:
    """逐批执行计划，返回**实际**降级的图片张数。

    一批 = 一次 `fold()`（同刻 tie 组整批，见 `policy` 判断题 3）。批内任一条重建失败
    就整批跳过——只降半组会把顺序弄乱，正是整批的理由。`fold()` 抛异常同样跳过该批继续
    （子设计 §10）：本级 `freed_tokens` 相应减少，编排自然升级到 L1。
    """
    by_id = {str(getattr(r, "id", "") or ""): r for r in records}
    total = 0
    for batch in plan.batches:
        events: list[MemoryEvent] = []
        for rid in batch:
            ev = _rebuild(by_id[rid], plan.demote_indices.get(rid, ()), scope)
            if ev is None:
                events = []
                break
            events.append(ev)
        if not events:
            continue
        try:
            await memory.fold(list(batch), events, ctx)
        except Exception:
            logger.warning("L0.5 降级跳过一批：fold 失败 (ids=%s)", list(batch),
                           exc_info=True)
            continue
        total += sum(len(plan.demote_indices.get(rid, ())) for rid in batch)
    return total


async def demote_for_budget(
    memory: Any,
    address: Any,
    ctx: Any,
    *,
    keep_recent: int,
    scope: MemoryScope = MemoryScope.TASK,
    kinds: Iterable[Any] | None = None,
) -> int:
    """L0.5 调用：降级该视图内除**最近 `keep_recent` 张图**之外的所有可降图片。

    返回**实际降级的图片张数**（不是记录条数；`fold()` 失败的批次不计）。
    ``keep_recent`` 按图片张数数，一条记录可能只有一部分图被降——口径与理由见
    `policy` 模块 docstring 判断题 1。

    只降 `source_type == "ref"` 的图（判断题 2）：inline base64 换成占位就再也取不回来。
    **`BlobStore` 未注册时视图里根本不存在 ref 形态的图**，于是选中空集、一次 `fold()`
    都不发、返回 0——「行为与改造前逐字节一致」（子设计 §10）由这同一条判据兜住。

    参数名注：子设计 §8 写的是 `demote_for_budget(memory, scope, ctx, ...)`，那个
    `scope` 指坐标；v2 已把坐标改名 `address`、`scope` 让位给归属范围枚举
    （`protocols/memory.py`）。此处随 `load_view(address, scope, ctx, kinds)` 的现行
    词汇，位置参数顺序不变。
    """
    records = await memory.load_view(address, scope, ctx, kinds=list(kinds) if kinds else None)
    plan = plan_demotions(records, keep_recent=keep_recent)
    if not plan.batches:
        return 0
    return await _execute(memory, records, plan, scope, ctx)


async def demote_all(
    memory: Any,
    record_ids: Sequence[str],
    ctx: Any,
    *,
    address: Any,
    scope: MemoryScope = MemoryScope.TASK,
    kinds: Iterable[Any] | None = None,
) -> int:
    """L1/L3 折叠前调用（子设计 §6.1）：对指定记录**无条件**降级，不受 `keep_recent` 保护。

    返回实际降级的图片张数。语义上等价于 ``keep_recent=0`` 且只作用于 ``record_ids``。

    仍需 ``address`` / ``scope``：`MemoryProvider` 没有「按 id 取记录」的读接口，且同刻
    tie 组的顺序保护要看**整个视图**——折叠范围的边界很可能正好切在一个同刻组中间，
    只看范围内的记录会把组外那半条的顺序丢掉。故本函数照样 `load_view` 全量视图，
    只是把降级范围限定在 ``record_ids`` 内。

    ⚠️ 调用方在本函数之后**必须重新 `load_view`**（id 与内容都已改变），理由见模块
    docstring。
    """
    wanted = {str(r) for r in record_ids}
    if not wanted:
        return 0
    records = await memory.load_view(address, scope, ctx, kinds=list(kinds) if kinds else None)
    plan = plan_demotions(records, keep_recent=0, only_ids=wanted)
    if not plan.batches:
        return 0
    return await _execute(memory, records, plan, scope, ctx)
