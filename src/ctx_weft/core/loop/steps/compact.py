"""CompactStep：长对话压缩，由 PrepareStep 内联直调（不再以 task 形式调度）。

  - 作用域 = 当前 state.scope（当前 task + agent）。
  - 计算可折叠层（agent 派发日志 / task 对话），任一层 active 条数 > keep_last 才折。
  - 复用 act 装配内容 + 末尾压缩指令（composer purpose="compact"），一次 summary。
  - 对每个超额层 apply_compact 同一份 summary。
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from ctx_weft.core.assembler.assembler import ContextRequest
from ctx_weft.core.events import EventType
from ctx_weft.core.loop.driver import LoopContext, LoopState, Step, StepOutcome, make_event
from ctx_weft.core.loop.llm_gateway import stream_llm_resilient
from ctx_weft.core.utils import now_utc
from ctx_weft.protocols import LLMRequest, MemoryEvent, MemoryEventType, MemoryLayer

logger = logging.getLogger(__name__)

TASK_COMPACT_TYPES = [
    MemoryEventType.USER_PROMPT,
    MemoryEventType.LLM_RESPONSE,
    MemoryEventType.TOOL_RESULT,
]

# task 层 body 类型（跨层 fold 的 L0→L1 删除目标，spec 2026-06-28 §4）。
# 按 agent 跨-task 召回（recall_recent_by_agent），每条 metadata["task_id"] 标来源 task。
_TASK_BODY_TYPES = [
    MemoryEventType.USER_PROMPT,
    MemoryEventType.LLM_RESPONSE,
    MemoryEventType.TOOL_INVOCATION,
    MemoryEventType.TOOL_RESULT,
    MemoryEventType.TASK_COMPACT_SUMMARY,
]


async def summarize_for_compact(state: LoopState, ctx: LoopContext) -> str:
    """装配 purpose="compact" 上下文 + 一次 LLM 摘要，返回摘要文本。

    LLM 摘要是 compact 的硬依赖（compact + observe 回退档共用本函数）：瞬时故障由
    stream_llm_resilient 自愈，自愈耗尽抛 LLMOutageError → 走 INTERRUPTED。**不再**在
    LLM 失败时静默退化为纯截断（旧的 except Exception 兜底已移除）。
    """
    agent = state.agent
    request = ContextRequest(
        purpose="compact",
        scope=state.scope,
        task=state.task,
        agent=agent,
        session=state.session,
        template=state.extra.get("template"),
        bound_capabilities=state.extra.get("bound_capabilities", []),
        actor_transcript=state.transcript,
    )
    compact_prompt = await ctx.assembler.assemble(request)

    llm_request = LLMRequest(
        model=agent.runtime.get("llm_model", "mock"),
        system=compact_prompt.system,
        messages=compact_prompt.messages,
        tools=[],
    )
    summary_text = ""
    async for chunk in stream_llm_resilient(ctx, state, llm_request):
        if chunk.kind == "token":
            summary_text += chunk.text
    return summary_text


async def maybe_compact_before_dispatch(
    state: LoopState, ctx: LoopContext, *, prompt_tokens: int
) -> list[Any]:
    """派发执行前的按需压缩：本轮 prompt token 越过 predispatch_compact_token_ratio 时，
    对父 scope 跑一次完整压缩（task 层 fold_task + agent 层 fold_root + 回收 finished 短 task，
    共享单次 summary），使父 resume 更精简、inherit 快照为压缩后版本。

    与 PrepareStep 的 compact 同构（复用 _compact_scope），区别只在触发条件：这里由独立的
    predispatch token 阈值门控，且仅在本轮含派发调用时由 ActStep 调用。门控不满足返回空列表。
    """
    agent = state.agent
    ratio = getattr(agent.loop_config, "predispatch_compact_token_ratio", 0.0)
    if ratio <= 0:
        return []
    context_limit = agent.loop_guard.context_limit
    tokens = prompt_tokens or agent.loop_guard.context_tokens
    if context_limit <= 0 or tokens <= 0:
        return []
    if tokens / context_limit < ratio:
        return []
    return await _compact_scope(state, ctx, trigger="pre_dispatch")


async def _count_root_residues(state: LoopState, ctx: LoopContext) -> int:
    """本 agent scope 内「仍有 task 层 body 的结束顶层单元」数（= L0 单元数，spec 2026-06-28 §4）。

    顶层单元 = 按 origin_task_id 分组的 finish 对（AGENT_CONVERSATION_TURN），其 parent_task_id
    为 None 或不在本 scope origin 集内。L0 ⟺ 该单元 task 层 body（USER_PROMPT/LLM_RESPONSE/…）
    尚未被 fold L0→L1 删除（仍可 recall_recent_by_agent 召回）。L1（已删 body）/L2 不计——它们
    不再驱动 token 压力的 fold。"""
    recs = await ctx.memory.recall_recent(
        state.scope, [MemoryEventType.AGENT_CONVERSATION_TURN], 2000, ctx.provider_ctx,
    )
    parent_of: dict[str, Any] = {}
    for r in recs:
        oid = r.metadata.get("origin_task_id")
        if oid is not None and oid not in parent_of:
            parent_of[oid] = r.metadata.get("parent_task_id")
    origins = set(parent_of)
    top = {oid for oid, pid in parent_of.items() if pid is None or pid not in origins}
    if not top:
        return 0
    # 哪些顶层单元仍有 body：跨-task 按 agent 召回 task 层 body，看其 task_id 是否落在 top 集
    body = await ctx.memory.recall_recent_by_agent(
        state.scope, _TASK_BODY_TYPES, 2000, ctx.provider_ctx,
    )
    with_body = {r.metadata.get("task_id") for r in body} & top
    return len(with_body)


async def fold_root_experience(state: LoopState, ctx: LoopContext, keep_last: int,
                               summary_text: str) -> int:
    """跨层三级降级 fold（spec 2026-06-28 §4）。

    顶层折叠单元 = 一组同 origin_task_id 的 finish 对（AGENT_CONVERSATION_TURN，agent 层）+
    其 task 层 body（USER_PROMPT/LLM_RESPONSE/…，按 task_id 关联）。顶层 = parent_task_id is
    None 或 parent_task_id ∉ 本 scope origin 集；折某顶层时沿 parent 链纳后代子树。三级：

    - L0（完整）：最近 keep_full(=keep_last) 个顶层单元 → 不动（body + finish 对都留）。
    - L1（黑盒）：超 keep_full 的单元 → **删 task 层 body**（supersede），仅剩 agent 层 finish 对。
    - L2（文本）：超 keep_pair（更老）的单元 → **连 finish 对 + 配对派发对 + 旧 AGENT_COMPACT_SUMMARY
      也 supersede**，折成一条新 AGENT_COMPACT_SUMMARY（锚保留集最早 ts − 1µs，不变量 A）。

    跨层 supersede 一次原子提交（ids 跨 task 层 body + agent 层 finish 对，provider 按 id 生效、
    不按 scope 过滤）。返回 supersede 的总条数。
    """
    memory = ctx.memory
    keep_pair = getattr(state.agent.loop_config, "compact_keep_pair", 30)
    recs = await memory.recall_recent(
        state.scope,
        [MemoryEventType.TASK_DISPATCH, MemoryEventType.TASK_DISPATCH_RESULT,
         MemoryEventType.AGENT_COMPACT_SUMMARY, MemoryEventType.AGENT_CONVERSATION_TURN],
        2000, ctx.provider_ctx,
    )
    recs = list(reversed(recs))  # newest-first → chronological
    # task 层 body（跨 task 按 agent 召回，每条 metadata["task_id"] 标来源单元）
    body_recs = await memory.recall_recent_by_agent(
        state.scope, _TASK_BODY_TYPES, 2000, ctx.provider_ctx,
    )

    # 按 origin 分组 finish 对 + 记 parent + 最早 ts（仅 finish 对定义顶层单元集）
    parent_of: dict[str, Any] = {}
    first_ts: dict[str, Any] = {}
    for r in recs:
        if r.type != MemoryEventType.AGENT_CONVERSATION_TURN:
            continue
        oid = r.metadata.get("origin_task_id")
        if oid is None:
            continue
        parent_of.setdefault(oid, r.metadata.get("parent_task_id"))
        if oid not in first_ts or r.timestamp < first_ts[oid]:
            first_ts[oid] = r.timestamp
    origins = set(parent_of)
    top = [oid for oid, pid in parent_of.items() if pid is None or pid not in origins]
    if len(top) <= keep_last:
        return 0
    top.sort(key=lambda oid: first_ts[oid])

    def _expand(roots: list) -> set:
        """从顶层 origin 出发，沿 parent 链纳入所有后代组（同 agent 内嵌子胶囊）。"""
        out = set(roots)
        changed = True
        while changed:
            changed = False
            for oid, pid in parent_of.items():
                if pid in out and oid not in out:
                    out.add(oid)
                    changed = True
        return out

    # 顶层分桶：超 keep_full → L1+；超 keep_pair → L2（keep_pair ≥ keep_full）
    l1_top = top if keep_last <= 0 else top[:-keep_last]          # 超 keep_full（含 L2）
    l2_top = top if keep_pair <= 0 else top[:-keep_pair]          # 超 keep_pair（最老）
    kept_full_top = [] if keep_last <= 0 else top[-keep_last:]    # 保 L0
    l1_set = _expand(l1_top)        # 删 body 的单元（含其子树）
    l2_set = _expand(l2_top)        # 连 finish 对一并删的单元（含其子树）
    kept_set = _expand(kept_full_top)

    ids: list = []

    # L0→L1：删 l1_set 单元的 task 层 body（跨层 supersede）
    for r in body_recs:
        if r.metadata.get("task_id") in l1_set:
            ids.append(r.id)

    # L1→L2：l2_set 单元的 finish 对 + 旧 AGENT_COMPACT_SUMMARY → supersede
    for r in recs:
        if (r.type == MemoryEventType.AGENT_CONVERSATION_TURN
                and r.metadata.get("origin_task_id") in l2_set):
            ids.append(r.id)
        elif r.type == MemoryEventType.AGENT_COMPACT_SUMMARY:
            ids.append(r.id)  # 旧摘要并入新摘要
    # 配对 cross-agent / 同 agent scheduled 派发对：delegating task ∈ l2_set
    l2_tcids: set = set()
    for r in recs:
        if (r.type == MemoryEventType.TASK_DISPATCH_RESULT
                and r.metadata.get("parent_task_id") in l2_set):
            ids.append(r.id)
            tc = r.metadata.get("tool_call_id")
            if tc:
                l2_tcids.add(tc)
    for r in recs:
        if (r.type == MemoryEventType.TASK_DISPATCH
                and r.metadata.get("tool_call_id") in l2_tcids):
            ids.append(r.id)

    if not ids:
        return 0
    await memory.supersede(ids, ctx.provider_ctx)

    # L2 折出新摘要：仅当确有单元降到 L2 时写
    if not l2_top:
        return len(ids)

    # anchor = L2 之外仍存的最早单元（L1 黑盒/L0 完整）的 finish 对 ts − 1µs。
    # （新摘要须排在所有保留 finish 对/派发对之前 → 不变量 A）
    surviving = origins - l2_set
    kept_ts: list = []
    for r in recs:
        oid = r.metadata.get("origin_task_id")
        pid = r.metadata.get("parent_task_id")
        if ((r.type == MemoryEventType.AGENT_CONVERSATION_TURN and oid in surviving)
                or (r.type == MemoryEventType.TASK_DISPATCH_RESULT and pid in surviving)):
            kept_ts.append(r.timestamp)
    anchor_ts = (min(kept_ts) if kept_ts else now_utc())
    await memory.ingest(
        MemoryEvent(
            type=MemoryEventType.AGENT_COMPACT_SUMMARY,
            scope=state.scope,
            content=summary_text or "[Experience compacted]",
            timestamp=anchor_ts - timedelta(microseconds=1),
            role="user",
            metadata={"keep_last": keep_last, "keep_pair": keep_pair,
                      "folded_count": len(l2_top)},
        ),
        ctx.provider_ctx,
    )
    return len(ids)


async def _compact_scope(
    state: LoopState, ctx: LoopContext, *, trigger: str = "compact"
) -> list[Any]:
    """对 state.scope 跑一次压缩：task 层 fold_task(a) + agent 层 fold_root(c)，
    两者共享单次 summary。

    trigger 标记触发来源（"compact" = PrepareStep 内联；"pre_dispatch" = 派发前），透传进
    event payload 供遥测区分。无可折时返回空事件列表，不空跑 summary LLM。

    task-resident（spec 2026-06-28 §5）：取消「压力下回收 finished 短 task」——结束 task 的
    body 留 task 层（即胶囊），由跨层 fold 管理，不再 close_finished_short_tasks 坍缩。
    """
    agent = state.agent
    keep_last = agent.loop_config.compact_keep_last

    events: list[Any] = []

    # (a) 活跃 task 长对话；(c) 已结束 root 残留
    task_n = await ctx.memory.count_recent(
        scope=state.scope, types=TASK_COMPACT_TYPES, ctx=ctx.provider_ctx)
    fold_task = task_n > keep_last
    fold_root = await _count_root_residues(state, ctx) > keep_last
    if not fold_task and not fold_root:
        return events

    events.append(make_event(state, EventType.MEMORY_COMPACT_STARTED, payload={
        "task_id": state.task.id, "agent_id": agent.id, "keep_last": keep_last,
        "fold_task": fold_task, "fold_root": fold_root, "trigger": trigger,
    }))
    summary_text = await summarize_for_compact(state, ctx)

    if fold_task:
        result = await ctx.memory.apply_compact(
            scope=state.scope, summary=summary_text or "[Context compacted]",
            keep_last=keep_last, ctx=ctx.provider_ctx, layer=MemoryLayer.TASK,
            protect_types=(MemoryEventType.USER_PROMPT,))
        events.append(make_event(state, EventType.MEMORY_COMPACTED, payload={
            "events_before": result.events_before, "events_after": result.events_after,
            "summary_event_id": result.summary_event_id, "layer": "task",
            "trigger": trigger, "used_llm": bool(summary_text)}))

    if fold_root:
        n = await fold_root_experience(state, ctx, keep_last, summary_text)
        if n:
            events.append(make_event(state, EventType.MEMORY_COMPACTED, payload={
                "superseded_count": n, "layer": "agent", "source": "root_experience",
                "trigger": trigger, "used_llm": bool(summary_text)}))

    logger.info("compact[%s]: agent=%s task=%s fold_task=%s fold_root=%s summary_len=%d",
                trigger, agent.id, state.task.id, fold_task, fold_root, len(summary_text))
    return events


class CompactStep(Step):
    """Standalone compaction over state.scope. Invoked inline by PrepareStep."""

    name = "compact"

    async def execute(self, state: LoopState, ctx: LoopContext) -> StepOutcome:
        return StepOutcome(
            next_step=None,
            events=await _compact_scope(state, ctx, trigger="compact"),
        )
