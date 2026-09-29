"""段作用域折叠：框架侧的策展（`segment_fold`）与它的免折门（`is_short_segment`）。

两者是一件事的两面——「这一段要不要折」与「折的时候折哪些」，判据（段界 = 末条 role=user
回合、水位线只认开跑前的记录）逐字相同。2026-09-29 从 `steps/` 搬来合并：门此前住在
`steps/background_observe.py`，与它唯一的同门调用方 `observe._fold_retry_segment` 隔着
一个模块，而那个模块还管着并发、判决提交、close 交接三件无关的事。

`segment_fold` 的策展语义（v2 P3c，策展上移）：

移植自 provider ``apply_compact`` 的段折语义（spec/06 §7 + 2026-07-21 排序契约），
政策收拢为两调用点（observe retry 段折 / 后台 recap 的边界段折）共用的固定形态：

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
from typing import TYPE_CHECKING

from ctx_weft.core.media import placeholder_refs
from ctx_weft.core.utils.clock import now_utc
from ctx_weft.core.utils.content import content_to_text, image_tokens
from ctx_weft.protocols import (
    MemoryAddress,
    MemoryEvent,
    MemoryKind,
    MemoryScope,
    MemoryProvider,
    ProviderContext,
)

_VIEW_KINDS = [MemoryKind.CONVERSATION_TURN, MemoryKind.SUMMARY, MemoryKind.TOOL_AUDIT]


if TYPE_CHECKING:
    from ctx_weft.core.loop.driver import LoopContext, LoopState


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


# ── 免折门 ────────────────────────────────────────────────────────────────────


async def is_short_segment(
    state: "LoopState", ctx: "LoopContext", watermark: "datetime | None" = None,
) -> bool:
    """短段免折门：**当前段**（最后一条 active USER_PROMPT 之后）满足以下任一即为短段：

    - 段内 LLM 回复（role=assistant 回合）≤ 1 条——**不看 token**：一条回复折成摘要
      是净亏（recap 常比原文还长，且原文对下一轮信息更全），折它只是白花一次 LLM；
    - 段内 raw token ≤ short_segment_token_threshold。

    「短 → 原文成胶囊」决策（finalize._is_short_leaf）在段级的判定，
    `background.recap`（plain_text/interrupt 边界）与
    `observe._fold_retry_segment`（retry 段折）共用。配置缺失（手构 state /
    单测）→ False = 门关闭，照常折叠。

    段作用域（2026-07-21）：只数末条 UP 之后的 raw，与折叠的 since_last=USER_PROMPT
    对齐——免折残留的前段 raw 不计入，否则「前段累积 + 当前段极短」会被误判为可折，
    而折叠又只折当前段，产出比原文还长的摘要。
    """
    threshold = getattr(state.agent.loop_config, "short_segment_token_threshold", 0)
    if threshold <= 0:
        return False
    from ctx_weft.protocols import MemoryKind, MemoryScope

    # v2 P3a：TASK 视图（对话 + audit，无 SUMMARY——旧类型清单不含段摘要）升序；
    # 段界 = 末条 role=user 回合，其后即当前段 raw。
    view = await ctx.memory.load_view(
        state.scope, MemoryScope.TASK, ctx.provider_ctx,
        kinds=[MemoryKind.CONVERSATION_TURN, MemoryKind.TOOL_AUDIT],
    )
    # 段界水位线（2026-09-22，同 `segment_fold`）：只认这段后台 observe 开跑时就已存在
    # 的记录。不设限的话，人中途说的话会成为新段界，把「当前段」判成空段而误判为短段。
    if watermark is not None:
        view = [r for r in view if r.timestamp <= watermark]
    seg_records: list = []
    for r in view:
        if r.kind is MemoryKind.CONVERSATION_TURN and r.role == "user":
            seg_records = []  # 新段界：清空重计
            continue
        seg_records.append(r)
    # 单回复段免折（2026-08-19）：token 再多也不折——见 docstring。
    n_llm = sum(1 for r in seg_records
                if r.kind is MemoryKind.CONVERSATION_TURN and r.role == "assistant")
    if n_llm <= 1:
        return True
    seg_text = " ".join(
        r.content if isinstance(r.content, str) else content_to_text(r.content)
        for r in seg_records
    )
    # 保留「join 后数一次」的文本口径（逐条估算会引入 4×N 的 framing 漂移），
    # 图片另行求和补上——不补则图片密集段被误判短段免折、该段 raw 永久保留。
    seg_images = sum(image_tokens(r.content) for r in seg_records)
    return ctx.llm.tokenizer.count(seg_text) + seg_images <= threshold
