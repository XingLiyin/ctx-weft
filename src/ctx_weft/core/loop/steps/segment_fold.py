"""segment_fold：段作用域折叠的框架侧策展（v2 P3c，策展上移）。

移植自 provider ``apply_compact`` 的段折语义（spec/06 §7 + 2026-07-21 排序契约），
政策收拢为两调用点（observe retry 段折 / background_observe 边界段折）共用的固定形态：

- keep_last=0：折叠池内可折记录全折；
- protect = role=user 的 CONVERSATION_TURN + SUMMARY kind（段锚点 + 多段摘要累积，
  后一段折不得吞前一段摘要、不得抢占其锚位）；
- 段界（since_last=USER_PROMPT 的 kind 化）= 最后一条 role=user 回合，其后为折叠池；
  短段免折残留的前段 raw 永不跨段折入本摘要；
- 锚点：摘要落「被折区起点之后第一条幸存记录之前」（ts − 1µs）；段尾无幸存者则锚到
  被折段末条位置——**不用 now()**：脱管的后台 observe 迟到收尾时，now() 可能晚于同刻
  注入的下一轮 user 回合，摘要越到新消息之后 → 下一轮装配误判「续跑」并埋掉新消息；
- 摘要 role：TASK 层 "assistant"（当前任务的上一段自述）；AGENT 层 "user"
  （agent 层折叠摘要是 prompt 首条，Anthropic 首条 assistant 会 400）。

provider 只执行显式 id 集的原子 fold；哪些该折由本模块决定（v2 §4 策展上移）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ctx_weft.core.media import placeholder_refs
from ctx_weft.core.utils.clock import now_utc
from ctx_weft.protocols import (
    MemoryAddress,
    MemoryEvent,
    MemoryKind,
    MemoryScope,
    MemoryProvider,
    ProviderContext,
)

_VIEW_KINDS = [MemoryKind.CONVERSATION_TURN, MemoryKind.SUMMARY, MemoryKind.TOOL_AUDIT]


@dataclass
class SegmentFoldResult:
    """段折结果（供 MEMORY_COMPACTED 事件 payload；字段名沿旧 CompactResult）。"""

    events_before: int
    events_after: int
    summary_event_id: str


def _is_user_turn(r) -> bool:
    return r.kind is MemoryKind.CONVERSATION_TURN and r.role == "user"


def _protected(r) -> bool:
    return _is_user_turn(r) or r.kind is MemoryKind.SUMMARY


async def segment_fold(
    memory: MemoryProvider,
    address: MemoryAddress,
    layer: MemoryScope,
    summary: str,
    ctx: ProviderContext,
    watermark: "datetime | None" = None,
) -> SegmentFoldResult:
    """折叠当前段并原子写入段摘要；返回前后计数与摘要 id。

    ``watermark``：段界水位线（2026-09-22）。给定时**只把它之前就已存在的记录**看作
    「当前段」——后台 observe 是 fire-and-forget，它判完时人可能已经开口，新的
    USER_PROMPT 已经落库。段界若仍动态查找，那条新消息就成了「最后一条 user 回合」，
    于是折叠池空、`to_archive` 空、`summary_ts` 落到 `now_utc()` 分支排到末尾，而要折
    的那一段一条都没折——锚点逻辑根本没机会生效。钉住水位线之后，迟到的折叠自己落回
    原位，调用方不必为此等待。None = 不设限（前台同步段折用，它没有迟到可言）。
    """
    view = await memory.load_view(address, layer, ctx, kinds=_VIEW_KINDS)
    events_before = len(view)

    # 段界与折叠池都只在水位线之内认；`following`（锚点）仍看**完整** view——新来的
    # USER_PROMPT 正是要被它认出来，好让摘要锚到它之前 1μs。
    in_scope = view if watermark is None else [r for r in view if r.timestamp <= watermark]

    # 段界：最后一条 role=user 回合之后为折叠池；无 user 回合 → 整分区（防御，同旧行为）
    boundary_idx = next(
        (i for i in range(len(in_scope) - 1, -1, -1) if _is_user_turn(in_scope[i])),
        None,
    )
    pool_start = 0 if boundary_idx is None else boundary_idx + 1
    pool = in_scope[pool_start:]

    to_archive = [r for r in pool if not _protected(r)]

    # 锚点（视图已按 (timestamp, seq_no) 升序 = 渲染序，用下标定位）
    if to_archive:
        archived_ids = {r.id for r in to_archive}
        first_archived_idx = next(i for i, r in enumerate(view) if r.id in archived_ids)
        following = [r for r in view[first_archived_idx:] if r.id not in archived_ids]
        if following:
            summary_ts = following[0].timestamp - timedelta(microseconds=1)
        else:
            summary_ts = to_archive[-1].timestamp  # 段尾：锚被折段末条位置，不用 now()
    else:
        summary_ts = now_utc()

    summary_event = MemoryEvent(
        kind=MemoryKind.SUMMARY,
        scope=layer,
        address=address,
        content=summary,
        timestamp=summary_ts,
        role="assistant" if layer is MemoryScope.TASK else "user",
        metadata={"keep_last": 0, "archived_count": len(to_archive)},
        # `_protected` 只护住 user 回合与 SUMMARY，role="tool" 记录会被折进 `to_archive`——
        # 若其正文（本次 LLM 摘要）逐字带着 L0.5 占位向前走，供体记录正被本次 fold
        # supersede，占位的活引用只剩这一条新记录能扛，必须显式声明（同 compact.py 两处）。
        blob_refs=placeholder_refs(summary),
    )
    new_ids = await memory.fold([r.id for r in to_archive], [summary_event], ctx)

    return SegmentFoldResult(
        events_before=events_before,
        events_after=events_before - len(to_archive) + 1,
        summary_event_id=new_ids[0] if new_ids else "",
    )
