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
    """本 agent scope 内顶层折叠单元数：按 origin_task_id 分组的 AGENT_CONVERSATION_TURN 胶囊，
    其 parent_task_id 为 None 或不在本 scope origin 集内（cross-agent 子胶囊在自己 scope 当顶层）。
    取代旧的「数 TASK_DISPATCH_RESULT(parent_task_id is None)」（spec §3.11）。"""
    recs = await ctx.memory.recall_recent(
        state.scope, [MemoryEventType.AGENT_CONVERSATION_TURN], 2000, ctx.provider_ctx,
    )
    parent_of: dict[str, Any] = {}
    for r in recs:
        oid = r.metadata.get("origin_task_id")
        if oid is not None and oid not in parent_of:
            parent_of[oid] = r.metadata.get("parent_task_id")
    origins = set(parent_of)
    return sum(1 for oid, pid in parent_of.items() if pid is None or pid not in origins)


async def fold_root_experience(state: LoopState, ctx: LoopContext, keep_last: int,
                               summary_text: str) -> int:
    """折叠最老的已结束 root 胶囊（agent 层），保留最近 keep_last 个顶层单元。

    折叠单元 = 一组同 origin_task_id 的 AGENT_CONVERSATION_TURN（spec §3.11）。顶层单元 =
    parent_task_id is None 或 parent_task_id ∉ 本 scope origin 集。折某顶层单元时连其后代组
    （parent 链）+ 配对 cross-agent 派发对一并 supersede。返回 superseded 条数。
    """
    memory = ctx.memory
    recs = await memory.recall_recent(
        state.scope,
        [MemoryEventType.TASK_DISPATCH, MemoryEventType.TASK_DISPATCH_RESULT,
         MemoryEventType.AGENT_COMPACT_SUMMARY, MemoryEventType.AGENT_CONVERSATION_TURN],
        2000, ctx.provider_ctx,
    )
    recs = list(reversed(recs))  # newest-first → chronological

    # 按 origin 分组胶囊 + 记 parent + 最早 ts
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
    fold_top = top if keep_last <= 0 else top[:-keep_last]
    kept_top = [] if keep_last <= 0 else top[-keep_last:]

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

    fold_set = _expand(fold_top)
    kept_set = _expand(kept_top)

    ids: list = []
    for r in recs:
        if (r.type == MemoryEventType.AGENT_CONVERSATION_TURN
                and r.metadata.get("origin_task_id") in fold_set):
            ids.append(r.id)
        elif r.type == MemoryEventType.AGENT_COMPACT_SUMMARY:
            ids.append(r.id)  # 旧摘要并入新摘要
    # 配对 cross-agent / 同 agent scheduled 派发对：delegating task（parent_task_id）在折叠集内
    fold_tcids: set = set()
    for r in recs:
        if (r.type == MemoryEventType.TASK_DISPATCH_RESULT
                and r.metadata.get("parent_task_id") in fold_set):
            ids.append(r.id)
            tc = r.metadata.get("tool_call_id")
            if tc:
                fold_tcids.add(tc)
    for r in recs:
        if (r.type == MemoryEventType.TASK_DISPATCH
                and r.metadata.get("tool_call_id") in fold_tcids):
            ids.append(r.id)
    await memory.supersede(ids, ctx.provider_ctx)

    # anchor = 保留单元全部记录（回合 + 其派发对）最早 ts − 1µs
    kept_ts: list = []
    for r in recs:
        oid = r.metadata.get("origin_task_id")
        pid = r.metadata.get("parent_task_id")
        if ((r.type == MemoryEventType.AGENT_CONVERSATION_TURN and oid in kept_set)
                or (r.type == MemoryEventType.TASK_DISPATCH_RESULT and pid in kept_set)):
            kept_ts.append(r.timestamp)
    anchor_ts = (min(kept_ts) if kept_ts else now_utc())
    await memory.ingest(
        MemoryEvent(
            type=MemoryEventType.AGENT_COMPACT_SUMMARY,
            scope=state.scope,
            content=summary_text or "[Experience compacted]",
            timestamp=anchor_ts - timedelta(microseconds=1),
            role="user",
            metadata={"keep_last": keep_last, "folded_count": len(fold_top)},
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
