"""CompactStep：长对话压缩，由 PrepareStep 内联直调（不再以 task 形式调度）。

  - 作用域 = 当前 state.scope（当前 task + agent）。
  - escalating_compact：预算驱动的 L0.5→L1→L2→L3 升级编排（替旧的双阈值并行折 _compact_scope）。
    L0.5 = 图片降级（media.demote_for_budget，无 LLM、可逆），排在所有折叠之前（子设计 §6）。
    传入 token_estimate，未达 compact_target_ratio（回退 compact_token_ratio）× context_limit
    时空跑；否则按序试 L1 fold_root_experience（agent 层折叠成 AGENT_COMPACT_SUMMARY）→
    L2 demote_kept_capsules（L1 保留胶囊里 rich→lean 降级，无 LLM）→ L3 collapse_task_layer
    （当前 task 层坍缩成一条 USER_PROMPT，原始消息 + 执行摘要两节）；级间用 _active_memory_tokens
    的增量从估算里累减，降到 target 以下即停。
  - L1/L2 各有可折性 guard（无可折对象则跳过）；L3 无 guard，只要预算门开就会试（内部
    collapse_task_layer 自己在 ≤keep_last 时 noop）。
  - 注：observe(max_turns) 与 background_observe 仍走 apply_compact 写 TASK_COMPACT_SUMMARY
    形成胶囊，不在此文件改动范围内。
"""

from __future__ import annotations

import inspect
import logging
from datetime import timedelta
from typing import Any

from ctx_weft.core.assembler.assembler import ContextRequest
from ctx_weft.core.events import EventType
from ctx_weft.core.loop.driver import LoopContext, LoopState, Step, StepOutcome, make_event
from ctx_weft.core.loop.llm_gateway import request_prompt_estimate, stream_llm_resilient
# 模块级 import（**实测不成环**）：`core.media` 的模块级依赖只到 `core.content` /
# `protocols`，不回指 `core.loop`——`capability.py` 对 `CONTENT_PARTS_KEY` 用的正是
# 惰性 import，就是为了让这一条能写在模块级（Task 4 台账）。若日后 media 模块级引入了
# `core.loop` 的东西，这里要退回函数级 import。
from ctx_weft.core.media import demote_all, demote_for_budget
from ctx_weft.core.utils import content_to_text, effective_limit, image_tokens, now_utc
from ctx_weft.protocols import (
    LLMRequest, MemoryAddress, MemoryEvent, MemoryEventType, MemoryKind, MemoryScope,
)

logger = logging.getLogger(__name__)

# v2 P3a：类型清单改 kind 视图。task 层全量视图（对话 + 段摘要 + audit）= 旧
# _TASK_LAYER_TYPES/_TASK_BODY_TYPES 五类型；跨 task 聚合用半址（task_id=None）。
_TASK_VIEW_KINDS = [MemoryKind.CONVERSATION_TURN, MemoryKind.SUMMARY, MemoryKind.TOOL_AUDIT]


def _media_enabled(ctx) -> bool:
    """L0.5 与 §6.1 前置降级的总闸：只有真接了**可外部化**的 `BlobStore` 时才跑。

    未注册（`LoopContext.blob_store is None`）或注册的是 `NullBlobStore`
    （`can_externalize=False`）时，视图里根本不可能存在 `source_type == "ref"` 的图，
    降级必然返回 0；提前短路省掉整级的读操作，坐实子设计 §10「不接 BlobStore 时行为
    与改造前完全一致」——包括**一次多余的 memory 读都不发**。

    ⚠️ 这**不是** Task 2 判断题 2 拒绝的「第二处 registry 探询」。那条拒的是拿 registry
    当「哪些 part 该降」的判据（已注册但存量记录仍是 inline base64 时会放行并把图弄丢）。
    选谁降级仍然只由 `policy.demotable_ref` 的 ref 判据决定；本闸只能让降级**少做**，
    不能让它多做。代价是一个边角：曾接过 blob store、现已摘掉的宿主，存量 ref 图不再
    被降级——那些图本来也 rehydrate 不回来了。
    """
    store = getattr(ctx, "blob_store", None)
    return store is not None and bool(getattr(store, "can_externalize", False))


def _agent_half(scope) -> MemoryAddress:
    """agent 层半址（AGENT 视图禁带 task_id）。"""
    return MemoryAddress(session_id=scope.session_id, agent_id=scope.agent_id)


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
        token_counter=ctx.llm.tokenizer.count,
        extra={"compact_scope": scope},
    )
    compact_prompt = await ctx.assembler.assemble(request)

    llm_request = LLMRequest(
        model=agent.runtime.get("llm_model", "mock"),
        system=compact_prompt.system,
        messages=compact_prompt.messages,
        tools=[],
    )
    # 一次性调用（无循环内基线）→ baseline=None，走 max(整份估算, context_tokens)。
    llm_request.prompt_token_estimate = request_prompt_estimate(
        ctx.llm.tokenizer, llm_request, getattr(agent, "loop_guard", None), None)
    summary_text = ""
    async for chunk in stream_llm_resilient(ctx, state, llm_request):
        if chunk.kind == "token":
            summary_text += chunk.text
    return summary_text


# 坍缩 USER_PROMPT 的两节分隔标记；再坍缩时据此切出「原始消息」节，保持有界。
COLLAPSE_DELIM = "\n\n---\n## Execution Summary (earlier conversation compacted)\n"
# 存量标记：分隔标记进 prompt，改英文后旧库里的已坍缩 USER_PROMPT 仍带中文标记。切不出
# 原文节 = 把「原文 + 旧摘要」整体当原文，再坍缩一次就翻倍，故读侧一并认旧标记。
LEGACY_COLLAPSE_DELIMS = ("\n\n---\n## 执行摘要（先前对话已压缩）\n",)


def _original_section(content: str) -> str:
    """取（可能已坍缩过的）USER_PROMPT 的「原始消息」节：有分隔标记取其前段，否则整体即原文。"""
    for delim in (COLLAPSE_DELIM, *LEGACY_COLLAPSE_DELIMS):
        idx = content.find(delim)
        if idx != -1:
            return content[:idx]
    return content


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
    recs = await memory.load_view(
        state.scope, MemoryScope.TASK, ctx.provider_ctx, kinds=_TASK_VIEW_KINDS)
    if len(recs) <= keep_last:
        return 0

    fold = recs if keep_last <= 0 else recs[:-keep_last]

    # §6.1：折叠范围内的残留真图**先无条件降级**（不受 keep_recent 保护）。L0.5 保住了
    # 最近 keep_recent 张，升级到这一级时它们恰好可能落进折区；不降的话下面取 `original`
    # 节走 content_to_text 会把它们静默拍扁——最老的图留下了可取回的占位，最新的反而
    # 痕迹全无，优先级完全颠倒。降级后 ref 随 `original` 节一起活下来。
    if _media_enabled(ctx) and await demote_all(
        memory, [r.id for r in fold], ctx.provider_ctx,
        address=state.scope, scope=MemoryScope.TASK, kinds=_TASK_VIEW_KINDS,
    ):
        # 🔴 必须重新 load_view：降级是 fold(旧 id, 新事件)，**record id 全换了**。
        # 拿降级前采集的 id 去下面那次 fold，被降过的一条都 supersede 不掉 → 同一段
        # 对话在视图里出现两次；且手里这份 recs 已过期，用它取 `original` 节拿到的仍是
        # 真图，照旧被拍扁，本段的目的完全落空。
        recs = await memory.load_view(
            state.scope, MemoryScope.TASK, ctx.provider_ctx, kinds=_TASK_VIEW_KINDS)
        if len(recs) <= keep_last:
            return 0
        fold = recs if keep_last <= 0 else recs[:-keep_last]
    kept = [] if keep_last <= 0 else recs[-keep_last:]

    # 「原始消息」节 = 折区最早一条 user 回合的原文（已坍缩过则取其原始节，保持有界）
    original = ""
    for r in fold:
        if r.kind is MemoryKind.CONVERSATION_TURN and r.role == "user":
            text = r.content if isinstance(r.content, str) else content_to_text(r.content)
            original = _original_section(text)
            break

    # 锚：新 USER_PROMPT 须排在所有保留回合之前
    anchor_src = kept[0] if kept else fold[0]
    anchor_ts = anchor_src.timestamp - timedelta(microseconds=1)

    ids = [r.id for r in fold]
    # v2 P3d：遗忘+坍缩物一次原子 fold（旧徒手 supersede+ingest 有崩溃丢摘要窗口）
    await memory.fold(ids, [
        MemoryEvent(
            kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK,
            address=state.scope,
            content=f"{original}{COLLAPSE_DELIM}{summary_text or '[Context compacted]'}",
            timestamp=anchor_ts,
            role="user",
            metadata={"task_id": state.scope.task_id, "collapsed": True,
                      "keep_last": keep_last, "folded_count": len(fold)},
        ),
    ], ctx.provider_ctx)
    return len(ids)


async def maybe_compact_before_dispatch(
    state: LoopState, ctx: LoopContext, *, prompt_tokens: int
) -> list[Any]:
    """派发执行前的按需压缩：本轮 prompt token 越过 predispatch_compact_token_ratio 时，
    对父 scope 跑一次完整压缩（task 层 fold_task + agent 层 fold_root + 回收 finished 短 task，
    共享单次 summary），使父 resume 更精简、inherit 快照为压缩后版本。

    与 PrepareStep 的 compact 同构（复用 escalating_compact），区别只在触发条件：这里由独立的
    predispatch token 阈值门控，且仅在本轮含派发调用时由 ActStep 调用。门控不满足返回空列表。
    """
    agent = state.agent
    ratio = getattr(agent.loop_config, "predispatch_compact_token_ratio", 0.0)
    if ratio <= 0:
        return []
    context_limit = agent.loop_guard.context_limit
    reserve = getattr(agent.loop_guard, "reserved_output_tokens", 0)
    eff = effective_limit(context_limit, reserve)
    tokens = prompt_tokens or agent.loop_guard.context_tokens
    if eff <= 0 or tokens <= 0:
        return []
    if tokens / eff < ratio:
        return []
    return await escalating_compact(state, ctx, token_estimate=tokens, trigger="pre_dispatch")


def _dispatch_finish_sets(recs) -> tuple[set, set]:
    """扫 conversation turn，按 origin 标记：has_dispatch（有 delegate/delegate_plan 调用回合）、
    has_finish（有 finish_task 调用回合）。`active = has_dispatch − has_finish` = 未结束的
    delegating task（在途 working set，不可折，spec §2.3）。"""
    has_dispatch: set = set()
    has_finish: set = set()
    for r in recs:
        if r.kind is not MemoryKind.CONVERSATION_TURN or r.role != "assistant":
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


async def _active_memory_tokens(state: LoopState, ctx: LoopContext) -> int:
    """当前 scope 活跃记忆的 token 代理：task 层 body（跨 task 半址聚合）+ agent 层对话/摘要，
    逐条 content 求 ctx.llm.tokenizer.count 之和。用于升级 compact 级间的 before/after 增量粗估（非精确装配）。"""
    total = 0
    body = await ctx.memory.load_view(
        _agent_half(state.scope), MemoryScope.TASK, ctx.provider_ctx, kinds=_TASK_VIEW_KINDS)
    agent_recs = await ctx.memory.load_view(
        _agent_half(state.scope), MemoryScope.AGENT, ctx.provider_ctx)
    for r in [*body, *agent_recs]:
        text = r.content if isinstance(r.content, str) else content_to_text(r.content)
        # 图片另计：不计入则降级图片的 freed_tokens 恒为 0，escalating_compact 的
        # est 不减、误判该级白跑而继续升级（见 spec 2026-08-20-multimodal-design §6.5）。
        total += ctx.llm.tokenizer.count(text) + image_tokens(r.content)
    return total


async def _count_root_residues(state: LoopState, ctx: LoopContext) -> int:
    """本 agent scope 内「结束顶层单元（胶囊）」数（spec 2026-06-29，驱动 fold 触发阈值）。

    顶层单元 = 按 origin_task_id 分组的 conversation turn。一个 root task **无论当前 agent 亲自
    执行（finish 对 → has_finish）、还是派给别的 agent 执行且结果已回（dispatch + result/tool 回合
    → has_result）**，都是「已完成顶层单元」，计入 keep_last。真·在途（active，不计）= 派出但结果
    未回：has_dispatch 且既无 has_finish 也无 has_result。当前正在跑的 task 永不当完成单元。"""
    recs = await ctx.memory.load_view(
        _agent_half(state.scope), MemoryScope.AGENT, ctx.provider_ctx,
        kinds=[MemoryKind.CONVERSATION_TURN],
    )
    # parent prefer-non-None：dispatch result 回合不带 parent（None），不得覆盖权威 parent。
    parent_of: dict[str, Any] = {}
    has_dispatch, has_finish = _dispatch_finish_sets(recs)
    # has_result：该 origin 有返回结果（result/tool 回合）——派发型 root task 的「完成」标志。
    has_result = {r.metadata.get("origin_task_id") for r in recs
                  if r.role == "tool" and r.metadata.get("origin_task_id") is not None}
    for r in recs:
        oid = r.metadata.get("origin_task_id")
        if oid is None:
            continue
        p = r.metadata.get("parent_task_id")
        if oid not in parent_of or (parent_of[oid] is None and p is not None):
            parent_of[oid] = p
    origins = set(parent_of)
    active = has_dispatch - has_finish - has_result  # 派出且结果已回=完成；仅结果未回才算在途
    current = getattr(state.scope, "task_id", None)   # 当前正在跑的 task 不算可折完成单元
    top = {oid for oid, pid in parent_of.items()
           if oid != current and oid not in active and (pid is None or pid not in origins)}
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
    # AGENT 视图默认 kinds（对话+摘要）；legacy dispatch 配对已在 load_view 内归一，升序即时序。
    recs = await memory.load_view(
        _agent_half(state.scope), MemoryScope.AGENT, ctx.provider_ctx,
    )
    # task 层胶囊（跨 task 半址聚合，每条 address.task_id 标来源单元）
    body_recs = await memory.load_view(
        _agent_half(state.scope), MemoryScope.TASK, ctx.provider_ctx, kinds=_TASK_VIEW_KINDS,
    )

    # 按 origin 分组 conversation turn（finish 对 + dispatch 对同 origin）+ 记 parent + 最早 ts。
    # parent prefer-non-None：单元 parent 由权威回合（gateway delegate / finish 对）给出，dispatch
    # result 回合不带 parent（None），不得覆盖真实 parent。
    parent_of: dict[str, Any] = {}
    first_ts: dict[str, Any] = {}
    has_dispatch, has_finish = _dispatch_finish_sets(recs)
    for r in recs:
        if r.kind is not MemoryKind.CONVERSATION_TURN:
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
    # 完成的顶层单元（保留/折叠的对象）= 同 agent 亲做（has_finish）或派给别的 agent 且结果已回
    # （has_result：有 result/tool 回合）。仅「派出但结果未回」才是真·在途 active，不作可折胶囊。
    has_result = {r.metadata.get("origin_task_id") for r in recs
                  if r.role == "tool" and r.metadata.get("origin_task_id") is not None}
    active = has_dispatch - has_finish - has_result
    current = getattr(state.scope, "task_id", None)  # 当前正在跑的 task 不折
    top = [oid for oid, pid in parent_of.items()
           if oid != current and oid not in active and (pid is None or pid not in origins)]
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

    def _collect(agent_recs: list, task_recs: list) -> list:
        """本次要 supersede 的 id 全集。降级会换掉 record id，故这一步必须能重跑。"""
        out: list = []
        # 折掉 fold_set 单元的 task 层胶囊（user 回合 + 段摘要等；address.task_id 标来源）
        for r in task_recs:
            if (r.address.task_id if r.address else r.metadata.get("task_id")) in fold_set:
                out.append(r.id)
        # + 其 agent 层 conversation turn（finish 对 + dispatch 对，同 origin 同命运）+ 旧摘要
        for r in agent_recs:
            if (r.kind is MemoryKind.CONVERSATION_TURN
                    and r.metadata.get("origin_task_id") in fold_set):
                out.append(r.id)
            elif r.kind is MemoryKind.SUMMARY:
                out.append(r.id)  # 旧摘要并入新摘要
        return out

    ids = _collect(recs, body_recs)
    if not ids:
        return 0

    # §6.1：折叠范围内的残留真图先无条件降级（不受 keep_recent 保护），理由同
    # collapse_task_layer 处。折区横跨两个视图，故两次 demote_all（各自 load_view）。
    if _media_enabled(ctx):
        demoted = await demote_all(
            memory, ids, ctx.provider_ctx, address=_agent_half(state.scope),
            scope=MemoryScope.TASK, kinds=_TASK_VIEW_KINDS)
        demoted += await demote_all(
            memory, ids, ctx.provider_ctx, address=_agent_half(state.scope),
            scope=MemoryScope.AGENT)
        if demoted:
            # 🔴 必须重新 load_view + 重算 ids：降级换掉了 record id，拿旧 id 去下面那次
            # fold，被降过的一条都 supersede 不掉 → 同一段对话在视图里出现两次
            # （旧的没被 supersede、新的补偿记录也活着），而摘要已按旧内容生成。
            recs = await memory.load_view(
                _agent_half(state.scope), MemoryScope.AGENT, ctx.provider_ctx)
            body_recs = await memory.load_view(
                _agent_half(state.scope), MemoryScope.TASK, ctx.provider_ctx,
                kinds=_TASK_VIEW_KINDS)
            ids = _collect(recs, body_recs)
            if not ids:
                return 0

    # 折出新摘要：仅当确有单元被折时写（纯遗忘 = fold(ids, [])）
    if not fold_top:
        await memory.fold(ids, [], ctx.provider_ctx)
        return len(ids)

    # anchor = 仍保留单元的最早足迹 ts − 1µs（新摘要须排在所有保留胶囊之前）。足迹 = agent 层
    # conversation turn **加** task 层胶囊：健康数据里 user 回合与 task 层 user_prompt 同 ts，只看
    # 回合即够；但存量数据可能 user 回合被折而 task 层 user_prompt 存活（跨层折叠不同步），此时只看
    # 回合会漏掉更早的胶囊 ts、令摘要排到该胶囊之后。并入 body_recs ts 对健康数据是 no-op、对该
    # 类 split 令锚点回到最早（防御）。
    surviving = origins - fold_set
    kept_ts: list = []
    for r in recs:
        oid = r.metadata.get("origin_task_id")
        if r.kind is MemoryKind.CONVERSATION_TURN and oid in surviving:
            kept_ts.append(r.timestamp)
    for r in body_recs:
        if (r.address.task_id if r.address else r.metadata.get("task_id")) in surviving:
            kept_ts.append(r.timestamp)
    anchor_ts = (min(kept_ts) if kept_ts else now_utc())
    # v2 P3d：跨层遗忘 + 新摘要一次原子 fold（关旧「raw 已删而摘要未写」窗口）
    await memory.fold(ids, [
        MemoryEvent(
            kind=MemoryKind.SUMMARY, scope=MemoryScope.AGENT,
            address=state.scope,
            content=summary_text or "[Experience compacted]",
            timestamp=anchor_ts - timedelta(microseconds=1),
            role="user",
            metadata={"keep_last": keep_last, "folded_count": len(fold_top)},
        ),
    ], ctx.provider_ctx)
    return len(ids)


async def demote_kept_capsules(state: LoopState, ctx: LoopContext, origin_ids: set) -> int:
    """L2：把 origin_ids 里本 agent 亲做的 rich 胶囊降级成 sub-agent lean 表示。
    - 删该 task 的 task 层 body（USER_PROMPT/段摘要/raw，metadata['task_id'] in origin_ids）。
    - agent 层 finish 对：supersede assistant 槽（act_recap + finish 调用），保留 tool 槽（综合总结回填）
      作 lean 表示。已无 assistant 槽（已 lean / 纯 dispatch 对）的单元跳过。
    无 LLM。返回 supersede 条数。"""
    memory = ctx.memory
    body = await memory.load_view(
        _agent_half(state.scope), MemoryScope.TASK, ctx.provider_ctx, kinds=_TASK_VIEW_KINDS)
    turns = await memory.load_view(
        _agent_half(state.scope), MemoryScope.AGENT, ctx.provider_ctx,
        kinds=[MemoryKind.CONVERSATION_TURN])
    has_dispatch, has_finish = _dispatch_finish_sets(turns)

    ids: list = []
    for r in body:
        if (r.address.task_id if r.address else r.metadata.get("task_id")) in origin_ids:
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
    await memory.fold(ids, [], ctx.provider_ctx)  # 纯遗忘（v2 P3d）
    return len(ids)


async def _kept_origin_ids(state: LoopState, ctx: LoopContext, keep_last: int) -> set:
    """L1 折后仍保留的最近 keep_last 个顶层单元的 origin_task_id（L2 的降级对象）。"""
    recs = await ctx.memory.load_view(
        _agent_half(state.scope), MemoryScope.AGENT, ctx.provider_ctx,
        kinds=[MemoryKind.CONVERSATION_TURN])
    parent_of, first_ts = {}, {}
    has_dispatch, has_finish = _dispatch_finish_sets(recs)
    for r in recs:
        if r.kind is not MemoryKind.CONVERSATION_TURN:
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
    # 与 fold_root_experience 同口径：完成单元 = has_finish 或 has_result（结果已回）；当前 task 除外。
    has_result = {r.metadata.get("origin_task_id") for r in recs
                  if r.role == "tool" and r.metadata.get("origin_task_id") is not None}
    active = has_dispatch - has_finish - has_result
    current = getattr(state.scope, "task_id", None)
    top = [oid for oid, pid in parent_of.items()
           if oid != current and oid not in active and (pid is None or pid not in origins)]
    top.sort(key=lambda oid: first_ts[oid])
    return set(top[-keep_last:]) if keep_last > 0 else set()


async def escalating_compact(
    state: LoopState, ctx: LoopContext, *, token_estimate: int, trigger: str = "compact"
) -> list[Any]:
    """预算驱动升级式 compact（替 _compact_scope）：L0.5 图片降级 → L1 agent 折 → L2 rich→lean → L3 坍当前 task，
    每级后用 _active_memory_tokens 的增量从 token_estimate 累减，降到 target 以下即停。
    级间不完整重装配（Q4=c，调用方进 act 前重装配一次校正）。无 context_limit 或已达标 → []。"""
    agent = state.agent
    lc = agent.loop_config
    context_limit = agent.loop_guard.context_limit
    reserve = getattr(agent.loop_guard, "reserved_output_tokens", 0)
    eff = effective_limit(context_limit, reserve)
    if eff <= 0:
        return []
    target_ratio = lc.compact_target_ratio if getattr(lc, "compact_target_ratio", 0.0) > 0 \
        else lc.compact_token_ratio
    target_tokens = int(eff * target_ratio)
    keep_last = lc.compact_keep_last
    collapse_keep = getattr(lc, "collapse_keep_last", keep_last)
    # §12 未决参数，暂定 2 —— 从 loop_config 读（与 compact_keep_last 等同构），不硬编码。
    keep_recent_images = getattr(lc, "compact_keep_recent_images", 2)

    est = token_estimate
    if est < target_tokens:
        return []
    # MemoryCompactStarted：有 event_bus 时立即 live 发（folds 含 LLM 摘要、耗时数秒；随批次事后发
    # 会让前端状态条错过整个「压缩中」窗口）。无 bus（单测）时退回塞进返回批次，保持既有契约与用例。
    started = make_event(state, EventType.MEMORY_COMPACT_STARTED, payload={
        "task_id": state.task.id, "agent_id": agent.id, "trigger": trigger,
        "token_estimate": est, "target_tokens": target_tokens})
    bus = getattr(ctx, "event_bus", None)
    events: list[Any] = []
    if bus is not None:
        await bus.emit(started)
    else:
        events.append(started)

    def _finish(evts: list[Any]) -> list[Any]:
        """收尾：聚合本轮各级 MemoryCompacted，追加一条 MemoryCompactFinished。"""
        folded = [e for e in evts if e.type == EventType.MEMORY_COMPACTED]
        evts.append(make_event(state, EventType.MEMORY_COMPACT_FINISHED, payload={
            "task_id": state.task.id, "agent_id": agent.id, "trigger": trigger,
            "total_superseded": sum(e.payload.get("superseded_count", 0) for e in folded),
            "freed_tokens": sum(e.payload.get("freed_tokens", 0) for e in folded),
            "levels": [e.payload.get("source", "") for e in folded],
            "est_before": token_estimate, "est_after": est, "target_tokens": target_tokens}))
        return evts

    last_tokens: int | None = None

    async def _apply(level_coro):
        """跑一级折叠，用活跃 token before/after 增量累减 est。返回 (superseded_count, freed)。

        before 惰性复用上一级的 after（相邻级间省一次重复测量）；首次或跳级后重新测量。"""
        nonlocal est, last_tokens
        # 不变量：前一级的 after 直接当这一级的 before 复用，只在 _active_memory_tokens 是纯快照读、
        # 且两次 _apply 之间没有其他改动内存的操作时才成立。未来若在级间插入其他写操作，须重新测量。
        before = last_tokens if last_tokens is not None else await _active_memory_tokens(state, ctx)
        n = await level_coro if inspect.isawaitable(level_coro) else level_coro
        after = await _active_memory_tokens(state, ctx)
        freed = max(0, before - after)
        est -= freed
        last_tokens = after
        return n, freed

    # L0.5 · 图片降级（子设计 §6：排在所有折叠之前）
    # 三条理由：无 LLM（不花一次调用）、单位收益最高（一张图按当前口径最低 1600 token，
    # 满额 5MB 图 40960）、**可逆**（占位仍在原位，模型随时 media:get_image 取回）。
    # L1/L2/L3 折的是记录本身，一旦执行位置就没了；所以先花可逆的额度。
    # 未接 BlobStore 时 _media_enabled 直接短路，本级连一次 memory 读都不发（§10）。
    if _media_enabled(ctx):
        n, freed = await _apply(demote_for_budget(
            ctx.memory, _agent_half(state.scope), ctx.provider_ctx,
            keep_recent=keep_recent_images, scope=MemoryScope.TASK,
            kinds=_TASK_VIEW_KINDS))
        if n:
            # 注：本级的 n 是**图片张数**，不是记录条数（记录并没有被折走，只是被重写）。
            # 仍填进 superseded_count 以与各级同构、让 MemoryCompactFinished 的聚合口径
            # 不必分叉；真正无歧义的计数在 demoted_images。
            events.append(make_event(state, EventType.MEMORY_COMPACTED, payload={
                "superseded_count": n, "demoted_images": n, "layer": "task",
                "source": "demote_images", "trigger": trigger, "freed_tokens": freed}))
    if est < target_tokens:
        return _finish(events)

    # L1 · agent 折（仅当有可折顶层单元）
    if await _count_root_residues(state, ctx) > keep_last:
        summary_agent = await summarize_for_compact(state, ctx, scope="agent")
        n, freed = await _apply(fold_root_experience(state, ctx, keep_last, summary_agent))
        if n:
            events.append(make_event(state, EventType.MEMORY_COMPACTED, payload={
                "superseded_count": n, "layer": "agent", "source": "root_experience",
                "trigger": trigger, "freed_tokens": freed}))
    if est < target_tokens:
        return _finish(events)

    # L2 · 保留的同 agent rich 胶囊降级 lean（无 LLM）
    kept = await _kept_origin_ids(state, ctx, keep_last)
    if kept:
        n, freed = await _apply(demote_kept_capsules(state, ctx, kept))
        if n:
            events.append(make_event(state, EventType.MEMORY_COMPACTED, payload={
                "superseded_count": n, "layer": "agent", "source": "demote_lean",
                "trigger": trigger, "freed_tokens": freed}))
    if est < target_tokens:
        return _finish(events)

    # L3 · 坍缩当前 task（段摘要坍成更少，保 collapse_keep 条；仅当有 task 层材料可折）
    # 计数用全量 TASK 视图（含 SUMMARY 段摘要）——与 collapse_task_layer 实际所折一致：
    # retry 累积的是段摘要，只数 raw 会漏计、L3 永不触发。
    task_n = len(await ctx.memory.load_view(
        state.scope, MemoryScope.TASK, ctx.provider_ctx, kinds=_TASK_VIEW_KINDS))
    if task_n > collapse_keep:
        summary_task = await summarize_for_compact(state, ctx, scope="task")
        n, freed = await _apply(collapse_task_layer(state, ctx, collapse_keep, summary_task))
        if n:
            events.append(make_event(state, EventType.MEMORY_COMPACTED, payload={
                "superseded_count": n, "layer": "task", "source": "collapse",
                "trigger": trigger, "freed_tokens": freed}))
    logger.info("escalating_compact[%s]: agent=%s task=%s est→%d target=%d",
                trigger, agent.id, state.task.id, est, target_tokens)
    return _finish(events)


class CompactStep(Step):
    """Standalone compaction over state.scope. Invoked inline by PrepareStep."""

    name = "compact"

    async def execute(self, state: LoopState, ctx: LoopContext) -> StepOutcome:
        return StepOutcome(
            next_step=None,
            events=await escalating_compact(
                state, ctx, token_estimate=state.agent.loop_guard.context_tokens,
                trigger="compact"),
        )
