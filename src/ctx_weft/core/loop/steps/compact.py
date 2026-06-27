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
    """本 agent scope 内已结束 root task 的残留数（parent_task_id is None）。"""
    recs = await ctx.memory.recall_recent(
        state.scope, [MemoryEventType.TASK_DISPATCH_RESULT], 2000, ctx.provider_ctx,
    )
    return sum(1 for r in recs if r.metadata.get("parent_task_id") is None)


async def fold_root_experience(state: LoopState, ctx: LoopContext, keep_last: int,
                               summary_text: str) -> int:
    """折叠已结束 root task 的残留（agent 层，parent_task_id is None），保留最近 keep_last 个。

    当前 root 进行中派生的 sub-task 残留（parent_task_id 有值）**不折**——那是工作集，由
    close/GC 管理（spec 2026-06-23 §3c）。返回 superseded 条数。
    """
    memory = ctx.memory
    recs = await memory.recall_recent(
        state.scope,
        [MemoryEventType.TASK_DISPATCH, MemoryEventType.TASK_DISPATCH_RESULT,
         MemoryEventType.AGENT_COMPACT_SUMMARY, MemoryEventType.AGENT_CONVERSATION_TURN],
        2000, ctx.provider_ctx,
    )
    recs = list(reversed(recs))  # recall newest-first → chronological
    root_results = [
        r for r in recs
        if r.type == MemoryEventType.TASK_DISPATCH_RESULT
        and r.metadata.get("parent_task_id") is None
    ]
    if len(root_results) <= keep_last:
        return 0
    fold = root_results if keep_last <= 0 else root_results[:-keep_last]
    kept = [] if keep_last <= 0 else root_results[-keep_last:]
    fold_tcids = {r.metadata.get("tool_call_id") for r in fold}
    fold_task_ids = {r.metadata.get("child_task_id") for r in fold}
    ids = [r.id for r in fold]
    for r in recs:
        if (r.type == MemoryEventType.TASK_DISPATCH
                and r.metadata.get("tool_call_id") in fold_tcids):
            ids.append(r.id)
        elif r.type == MemoryEventType.AGENT_COMPACT_SUMMARY:
            ids.append(r.id)  # 旧摘要并入新摘要
        elif (r.type == MemoryEventType.AGENT_CONVERSATION_TURN
                and r.metadata.get("origin_task_id") in fold_task_ids):
            ids.append(r.id)  # 被折胶囊的 user / assistant-summary 回合一并折叠，避免落单
    await memory.supersede(ids, ctx.provider_ctx)
    anchor_ts = min((r.timestamp for r in kept), default=now_utc())
    # Truncation-only on summary-LLM failure (summary_text=="" → "[Experience compacted]"):
    # the fold still supersedes prior root experience so compaction bounds the agent layer
    # even without the LLM. Consistent with task-layer apply_compact; accepted edge
    # (rare LLM failure replaces root-experience text with the placeholder marker). [2026-06-23]
    await memory.ingest(
        MemoryEvent(
            type=MemoryEventType.AGENT_COMPACT_SUMMARY,
            scope=state.scope,
            content=summary_text or "[Experience compacted]",
            timestamp=anchor_ts - timedelta(microseconds=1),  # 逻辑置于保留窗口之前
            role="user",
            metadata={"keep_last": keep_last, "folded_count": len(fold)},
        ),
        ctx.provider_ctx,
    )
    return len(ids)


async def _compact_scope(
    state: LoopState, ctx: LoopContext, *, trigger: str = "compact"
) -> list[Any]:
    """对 state.scope 跑一次压缩：回收 finished 短 task(b) + task 层 fold_task(a) +
    agent 层 fold_root(c)，三者共享单次 summary。

    trigger 标记触发来源（"compact" = PrepareStep 内联；"pre_dispatch" = 派发前），透传进
    event payload 供遥测区分。无可折时只返回 close 产生的事件（可能为空），不空跑 summary LLM。
    """
    from ctx_weft.core.loop.steps.finalize import close_finished_short_tasks

    agent = state.agent
    keep_last = agent.loop_config.compact_keep_last

    # (b) 压力下回收 finished 短 task（交给 task close 机制）
    events: list[Any] = await close_finished_short_tasks(ctx.memory, state, ctx)

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
