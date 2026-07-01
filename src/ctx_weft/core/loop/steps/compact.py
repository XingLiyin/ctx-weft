"""CompactStep：长对话压缩，由 PrepareStep 内联直调（不再以 task 形式调度）。

  - 作用域 = 当前 state.scope（当前 task + agent）。
  - 两层各自阈值与摘要：task 层超 collapse_keep_last → collapse_task_layer 把早期回合
    坍缩成一条 USER_PROMPT（原始消息 + 执行摘要两节）；agent 层超 compact_keep_last →
    fold_root_experience 折派发经验成 AGENT_COMPACT_SUMMARY。
  - 每层各产各的 summary（task/agent 两条 cue），且仅在本层确需折叠时才调 LLM。
  - 注：observe(max_turns) 与 background_observe 仍走 apply_compact 写 TASK_COMPACT_SUMMARY
    形成胶囊，不在此文件改动范围内。
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from ctx_weft.core.assembler.assembler import ContextRequest
from ctx_weft.core.events import EventType
from ctx_weft.core.loop.driver import LoopContext, LoopState, Step, StepOutcome, make_event
from ctx_weft.core.loop.llm_gateway import stream_llm_resilient
from ctx_weft.core.loop.steps.legacy_dispatch import normalize_legacy_dispatch
from ctx_weft.core.utils import content_to_text, now_utc, estimate_tokens
from ctx_weft.protocols import LLMRequest, MemoryEvent, MemoryEventType

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


async def summarize_for_compact(
    state: LoopState, ctx: LoopContext, *, scope: str = "task"
) -> str:
    """装配 purpose="compact" 上下文 + 一次 LLM 摘要，返回摘要文本。

    scope 选 cue（composer 据 extra["compact_scope"] 分流）："task"=整段执行摘要（默认，
    observe 兜底与坍缩共用）；"agent"=派发经验摘要。

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
        extra={"compact_scope": scope},
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


# 坍缩 USER_PROMPT 的两节分隔标记；再坍缩时据此切出「原始消息」节，保持有界。
COLLAPSE_DELIM = "\n\n---\n## 执行摘要（先前对话已压缩）\n"

# task 层可折类型（当前 task 私有执行对话；TOOL_INVOCATION 仅审计，但一并 supersede）。
_TASK_LAYER_TYPES = [
    MemoryEventType.USER_PROMPT,
    MemoryEventType.LLM_RESPONSE,
    MemoryEventType.TOOL_INVOCATION,
    MemoryEventType.TOOL_RESULT,
    MemoryEventType.TASK_COMPACT_SUMMARY,
]


def _original_section(content: str) -> str:
    """取（可能已坍缩过的）USER_PROMPT 的「原始消息」节：有分隔标记取其前段，否则整体即原文。"""
    idx = content.find(COLLAPSE_DELIM)
    return content[:idx] if idx != -1 else content


async def collapse_task_layer(
    state, ctx, keep_last: int, summary_text: str
) -> int:
    """task compact（二级压缩）：把当前 task 层超过 keep_last 的早期回合（含原始 USER_PROMPT
    与 observer 的 `## Progress So Far`）整体坍缩成一条新 USER_PROMPT，content = 原始消息 +
    COLLAPSE_DELIM + 执行摘要；保留最近 keep_last 条 raw。返回 supersede 条数（≤keep_last → 0）。

    坍缩物是 USER_PROMPT 而非 assistant 摘要：composer 据 mtype=="user_prompt"+task_id 定位当前
    task 贴 `## Current Task/## Current Message` 框，故当前运行 task 坍缩后框架不丢；已结束胶囊被
    跨 task 召回时它就是一条背景 message。
    """
    memory = ctx.memory
    recs = await memory.recall_recent(state.scope, _TASK_LAYER_TYPES, 2000, ctx.provider_ctx)
    recs = list(reversed(recs))  # newest-first → chronological
    if len(recs) <= keep_last:
        return 0

    fold = recs if keep_last <= 0 else recs[:-keep_last]
    kept = [] if keep_last <= 0 else recs[-keep_last:]

    # 「原始消息」节 = 折区最早一条 USER_PROMPT 的原文（已坍缩过则取其原始节，保持有界）
    original = ""
    for r in fold:
        if r.type == MemoryEventType.USER_PROMPT:
            text = r.content if isinstance(r.content, str) else content_to_text(r.content)
            original = _original_section(text)
            break

    # 锚：新 USER_PROMPT 须排在所有保留回合之前
    anchor_src = kept[0] if kept else fold[0]
    anchor_ts = anchor_src.timestamp - timedelta(microseconds=1)

    ids = [r.id for r in fold]
    await memory.supersede(ids, ctx.provider_ctx)
    await memory.ingest(
        MemoryEvent(
            type=MemoryEventType.USER_PROMPT,
            scope=state.scope,
            content=f"{original}{COLLAPSE_DELIM}{summary_text or '[Context compacted]'}",
            timestamp=anchor_ts,
            role="user",
            metadata={"task_id": state.scope.task_id, "collapsed": True,
                      "keep_last": keep_last, "folded_count": len(fold)},
        ),
        ctx.provider_ctx,
    )
    return len(ids)


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


def _dispatch_finish_sets(recs) -> tuple[set, set]:
    """扫 conversation turn，按 origin 标记：has_dispatch（有 delegate/delegate_plan 调用回合）、
    has_finish（有 finish_task 调用回合）。`active = has_dispatch − has_finish` = 未结束的
    delegating task（在途 working set，不可折，spec §2.3）。"""
    has_dispatch: set = set()
    has_finish: set = set()
    for r in recs:
        if r.type != MemoryEventType.AGENT_CONVERSATION_TURN or r.role != "assistant":
            continue
        oid = r.metadata.get("origin_task_id")
        if oid is None:
            continue
        for tc in (r.metadata.get("tool_calls") or []):
            name = str(tc.get("name", ""))
            if name.endswith("finish_task"):
                has_finish.add(oid)
            elif name.endswith("delegate_task") or name.endswith("delegate_plan"):
                has_dispatch.add(oid)
    return has_dispatch, has_finish


_AGENT_LAYER_TYPES = [
    MemoryEventType.AGENT_CONVERSATION_TURN,
    MemoryEventType.AGENT_COMPACT_SUMMARY,
]


async def _active_memory_tokens(state: LoopState, ctx: LoopContext) -> int:
    """当前 scope 活跃记忆的 token 代理：task 层 body（跨 task 按 agent 召回）+ agent 层对话/摘要，
    逐条 content 求 estimate_tokens 之和。用于升级 compact 级间的 before/after 增量粗估（非精确装配）。"""
    total = 0
    body = await ctx.memory.recall_recent_by_agent(
        state.scope, _TASK_BODY_TYPES, 2000, ctx.provider_ctx)
    agent_recs = await ctx.memory.recall_recent(
        state.scope, _AGENT_LAYER_TYPES, 2000, ctx.provider_ctx)
    for r in [*body, *agent_recs]:
        text = r.content if isinstance(r.content, str) else content_to_text(r.content)
        total += estimate_tokens(text)
    return total


async def _count_root_residues(state: LoopState, ctx: LoopContext) -> int:
    """本 agent scope 内「结束顶层单元（胶囊）」数（spec 2026-06-29，驱动 fold 触发阈值）。

    顶层单元 = 按 origin_task_id 分组的 conversation turn（finish 对 + dispatch 对），其
    parent_task_id 为 None 或不在本 scope origin 集内。删 L1 后,结束单元就是胶囊（仍含 task 层
    胶囊 + agent 对话），折成摘要的单元已 supersede、不在此集。active delegating task（有 dispatch
    回合无 finish 对 = 未结束）是在途 working set,不计入。"""
    recs = await ctx.memory.recall_recent(
        state.scope, [MemoryEventType.AGENT_CONVERSATION_TURN], 2000, ctx.provider_ctx,
    )
    # parent prefer-non-None：dispatch result 回合不带 parent（None），不得覆盖权威 parent。
    parent_of: dict[str, Any] = {}
    has_dispatch, has_finish = _dispatch_finish_sets(recs)
    for r in recs:
        oid = r.metadata.get("origin_task_id")
        if oid is None:
            continue
        p = r.metadata.get("parent_task_id")
        if oid not in parent_of or (parent_of[oid] is None and p is not None):
            parent_of[oid] = p
    origins = set(parent_of)
    active = {oid for oid in has_dispatch if oid not in has_finish}
    top = {oid for oid, pid in parent_of.items()
           if oid not in active and (pid is None or pid not in origins)}
    return len(top)


async def fold_root_experience(state: LoopState, ctx: LoopContext, keep_last: int,
                               summary_text: str) -> int:
    """跨层折叠 fold：task 的详细度只 3 级（spec 2026-06-29 重订，删 L1 黑盒中间态）。

    - **执行中**：RUNNING/SUSPENDED → task 层 raw（每条工具调用展开），不在本函数管辖。
    - **胶囊**：close 时自动形成（`finalize._supersede_final_raw_segment` 删末 raw 段、留
      USER_PROMPT + TASK_COMPACT_SUMMARY）= task 层胶囊 + agent 层 finish 对（+ dispatch 对）。
      胶囊**只软删、不做 L1 压缩**——一直完整保留,直到超 keep_last 才整体折成摘要。
    - **总结**：超 keep_last 的最老顶层单元 → 连同其 task 层胶囊 + agent 层 finish/dispatch 对 +
      旧 AGENT_COMPACT_SUMMARY 一并 supersede,折成一条新 AGENT_COMPACT_SUMMARY（锚保留集最早
      ts − 1µs，不变量 A）。

    顶层折叠单元 = 一组同 origin_task_id 的 conversation turn（finish 对 + dispatch 对）；顶层 =
    parent_task_id is None 或 ∉ 本 scope origin 集；折某顶层时沿 parent 链纳后代子树。active
    delegating task（有 dispatch 回合无 finish 对 = 未结束）是在途 working set,不当可折顶层单元。

    跨层 supersede 一次原子提交（ids 跨 task 层胶囊 + agent 层对话,provider 按 id 生效、不按
    scope 过滤）。返回 supersede 的总条数。
    """
    memory = ctx.memory
    recs = await memory.recall_recent(
        state.scope,
        [MemoryEventType.TASK_DISPATCH, MemoryEventType.TASK_DISPATCH_RESULT,
         MemoryEventType.AGENT_COMPACT_SUMMARY, MemoryEventType.AGENT_CONVERSATION_TURN],
        2000, ctx.provider_ctx,
    )
    # §5.5：存量 legacy dispatch 对在读侧归一化成 conversation turn，下面只需面对单一表示。
    recs = normalize_legacy_dispatch(recs)
    recs = list(reversed(recs))  # newest-first → chronological
    # task 层胶囊（跨 task 按 agent 召回，每条 metadata["task_id"] 标来源单元）
    body_recs = await memory.recall_recent_by_agent(
        state.scope, _TASK_BODY_TYPES, 2000, ctx.provider_ctx,
    )

    # 按 origin 分组 conversation turn（finish 对 + dispatch 对同 origin）+ 记 parent + 最早 ts。
    # parent prefer-non-None：单元 parent 由权威回合（gateway delegate / finish 对）给出，dispatch
    # result 回合不带 parent（None），不得覆盖真实 parent。
    parent_of: dict[str, Any] = {}
    first_ts: dict[str, Any] = {}
    has_dispatch, has_finish = _dispatch_finish_sets(recs)
    for r in recs:
        if r.type != MemoryEventType.AGENT_CONVERSATION_TURN:
            continue
        oid = r.metadata.get("origin_task_id")
        if oid is None:
            continue
        p = r.metadata.get("parent_task_id")
        if oid not in parent_of or (parent_of[oid] is None and p is not None):
            parent_of[oid] = p
        if oid not in first_ts or r.timestamp < first_ts[oid]:
            first_ts[oid] = r.timestamp
    origins = set(parent_of)
    active = {oid for oid in has_dispatch if oid not in has_finish}  # 未结束 delegating task
    top = [oid for oid, pid in parent_of.items()
           if oid not in active and (pid is None or pid not in origins)]
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

    # 单一阈值：保最近 keep_last 个胶囊（完整：task 层胶囊 + agent 对话），更老的整体折成摘要。
    fold_top = top if keep_last <= 0 else top[:-keep_last]
    fold_set = _expand(fold_top)        # 整体折成摘要的单元（含其子树）

    ids: list = []
    # 折掉 fold_set 单元的 task 层胶囊（USER_PROMPT + TASK_COMPACT_SUMMARY 等）
    for r in body_recs:
        if r.metadata.get("task_id") in fold_set:
            ids.append(r.id)
    # + 其 agent 层 conversation turn（finish 对 + dispatch 对，同 origin 同命运）+ 旧摘要
    for r in recs:
        if (r.type == MemoryEventType.AGENT_CONVERSATION_TURN
                and r.metadata.get("origin_task_id") in fold_set):
            ids.append(r.id)
        elif r.type == MemoryEventType.AGENT_COMPACT_SUMMARY:
            ids.append(r.id)  # 旧摘要并入新摘要

    if not ids:
        return 0
    await memory.supersede(ids, ctx.provider_ctx)

    # 折出新摘要：仅当确有单元被折时写
    if not fold_top:
        return len(ids)

    # anchor = 仍保留单元（胶囊）的最早 conversation turn ts − 1µs（新摘要须排在所有保留胶囊之前）
    surviving = origins - fold_set
    kept_ts: list = []
    for r in recs:
        oid = r.metadata.get("origin_task_id")
        if r.type == MemoryEventType.AGENT_CONVERSATION_TURN and oid in surviving:
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


async def demote_kept_capsules(state: LoopState, ctx: LoopContext, origin_ids: set) -> int:
    """L2：把 origin_ids 里本 agent 亲做的 rich 胶囊降级成 sub-agent lean 表示。
    - 删该 task 的 task 层 body（USER_PROMPT/段摘要/raw，metadata['task_id'] in origin_ids）。
    - agent 层 finish 对：supersede assistant 槽（act_recap + finish 调用），保留 tool 槽（综合总结回填）
      作 lean 表示。已无 assistant 槽（已 lean / 纯 dispatch 对）的单元跳过。
    无 LLM。返回 supersede 条数。"""
    memory = ctx.memory
    body = await memory.recall_recent_by_agent(state.scope, _TASK_BODY_TYPES, 2000, ctx.provider_ctx)
    turns = await memory.recall_recent(
        state.scope, [MemoryEventType.AGENT_CONVERSATION_TURN], 2000, ctx.provider_ctx)
    has_dispatch, has_finish = _dispatch_finish_sets(turns)

    ids: list = []
    for r in body:
        if r.metadata.get("task_id") in origin_ids:
            ids.append(r.id)   # 删 task 层 body（降级核心：丢交互细节）
    for r in turns:
        oid = r.metadata.get("origin_task_id")
        if oid not in origin_ids:
            continue
        # 仅降级「本 agent 亲做」单元（有 finish 回合）；纯 dispatch 对本就 lean，不动
        if oid in has_finish and r.role == "assistant":
            ids.append(r.id)   # 折 finish 对 assistant 槽，仅留 tool 槽回填
    if not ids:
        return 0
    await memory.supersede(ids, ctx.provider_ctx)
    return len(ids)


async def _compact_scope(
    state: LoopState, ctx: LoopContext, *, trigger: str = "compact"
) -> list[Any]:
    """对 state.scope 跑一次压缩：task 层 fold_task(a) + agent 层 fold_root(c)，
    各用各阈值与各自 summary（两份摘要按需产生）。

    trigger 标记触发来源（"compact" = PrepareStep 内联；"pre_dispatch" = 派发前），透传进
    event payload 供遥测区分。无可折时返回空事件列表，不空跑 summary LLM。

    双阈值：
      collapse_keep（来自 loop_config.collapse_keep_last，默认=compact_keep_last）控制
      task 层坍缩（collapse_task_layer）；
      keep_last（来自 loop_config.compact_keep_last）控制 agent 层折叠（fold_root_experience）。

    task-resident（spec 2026-06-28 §5）：取消「压力下回收 finished 短 task」——结束 task 的
    body 留 task 层（即胶囊），由跨层 fold 管理，不再 close_finished_short_tasks 坍缩。
    """
    agent = state.agent
    keep_last = agent.loop_config.compact_keep_last                               # agent 折叠：胶囊数
    collapse_keep = getattr(agent.loop_config, "collapse_keep_last", keep_last)   # task 坍缩：回合数

    events: list[Any] = []

    # (a) 活跃 task 长对话（按 collapse 阈值）；(c) 已结束 root 残留（按胶囊阈值）
    task_n = await ctx.memory.count_recent(
        scope=state.scope, types=TASK_COMPACT_TYPES, ctx=ctx.provider_ctx)
    fold_task = task_n > collapse_keep
    fold_root = await _count_root_residues(state, ctx) > keep_last
    if not fold_task and not fold_root:
        return events

    events.append(make_event(state, EventType.MEMORY_COMPACT_STARTED, payload={
        "task_id": state.task.id, "agent_id": agent.id,
        "collapse_keep_last": collapse_keep, "compact_keep_last": keep_last,
        "fold_task": fold_task, "fold_root": fold_root, "trigger": trigger,
    }))

    # 按需产两份摘要：task 坍缩用 collapse_keep、agent 折叠用 keep_last；各用各 cue、各只在本层折时调 LLM。
    if fold_task:
        summary_task = await summarize_for_compact(state, ctx, scope="task")
        n_task = await collapse_task_layer(state, ctx, collapse_keep, summary_task)
        if n_task:
            events.append(make_event(state, EventType.MEMORY_COMPACTED, payload={
                "superseded_count": n_task, "layer": "task", "source": "collapse",
                "trigger": trigger, "used_llm": bool(summary_task)}))

    if fold_root:
        summary_agent = await summarize_for_compact(state, ctx, scope="agent")
        n = await fold_root_experience(state, ctx, keep_last, summary_agent)
        if n:
            events.append(make_event(state, EventType.MEMORY_COMPACTED, payload={
                "superseded_count": n, "layer": "agent", "source": "root_experience",
                "trigger": trigger, "used_llm": bool(summary_agent)}))

    logger.info("compact[%s]: agent=%s task=%s fold_task=%s(keep=%d) fold_root=%s(keep=%d)",
                trigger, agent.id, state.task.id, fold_task, collapse_keep, fold_root, keep_last)
    return events


class CompactStep(Step):
    """Standalone compaction over state.scope. Invoked inline by PrepareStep."""

    name = "compact"

    async def execute(self, state: LoopState, ctx: LoopContext) -> StepOutcome:
        return StepOutcome(
            next_step=None,
            events=await _compact_scope(state, ctx, trigger="compact"),
        )
