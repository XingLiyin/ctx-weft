"""CompactStep：由 StepDriver 调度的前台记忆压缩步骤。

  - 作用域 = 当前 state.scope（当前 task + agent）。
  - escalating_compact：预算驱动的 L0.5→L1→L2→L3 升级编排（替旧的双阈值并行折 _compact_scope）。
    L0.5 = 图片降级（media.demote_for_budget，无 LLM、可逆），排在所有折叠之前（子设计 §6）。
    传入 token_estimate，未达 compact_target_ratio（回退 compact_token_ratio）× context_limit
    时空跑；否则按序试 L1 demote_kept_capsules（已完成历史 rich→lean，复用收尾摘要）→
    L2 fold_root_experience（lean 历史归并成 AGENT_COMPACT_SUMMARY）→ L3 collapse_task_layer
    （当前 task 层坍缩成一条 USER_PROMPT，原始消息 + 执行摘要两节）；级间按渲染后消息的
    token 净变化更新预算，降到 target 以下即停。一轮内复用不可变记录与任务树计数。
  - L1 从旧到新逐颗降级，缺收尾摘要时先补摘要；L2 的最近历史保留数是软偏好，预算仍
    超标时继续归并。旧历史新增或执行细节尚未收敛时不进入 L3；L3 保护最近回合及待执行工具。
  - ObserveStep 的前台重试与后台观察通过 segment_fold 写 TASK_COMPACT_SUMMARY，
    与这里的预算压缩分开调度。
"""

from __future__ import annotations

import dataclasses
import inspect
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from contextvars import ContextVar
from datetime import timedelta
from typing import Any

from ctx_weft.core.assembler import CompactIntent, ContextRequest
from ctx_weft.core.assembler.prompts import runtime_prompt
from ctx_weft.core.loop.driver import (
    COMPACT_NOOP_KEY,
    LoopContext,
    LoopState,
    Step,
    StepDriver,
    StepOutcome,
    assistant_memory_checkpoint,
    make_event,
)
from ctx_weft.core.loop.llm_gateway import (
    build_llm_request,
    llm_request_id,
    resolve_llm_identity,
    stream_llm_resilient,
)
from ctx_weft.core.loop.llm_stream import ensure_collected_stream
from ctx_weft.core.loop.steps._compact_snapshot import (
    CapsulePlan,
    compact_snapshot,
    current_snapshot,
)
from ctx_weft.core.loop.task_memory_fold import capsule_origin, task_record_group, task_record_kind

# 模块级 import（**实测不成环**）：`core.media` 的模块级依赖只到 `core.utils.content` /
# `protocols`，不回指 `core.loop`。若日后 media 模块级引入了 `core.loop` 的东西，
# 这里要退回函数级 import。
from ctx_weft.core.media import demote_all, demote_for_budget, placeholder_refs
from ctx_weft.core.utils.content import content_to_text
from ctx_weft.core.utils.estimate import estimate_message_tokens
from ctx_weft.protocols import (
    LLMCallError,
    MemoryAddress,
    MemoryEvent,
    MemoryKind,
    MemoryScope,
    load_records,
)
from ctx_weft.protocols.events import EventOrigin, EventType

logger = logging.getLogger(__name__)

# v2 P3a：类型清单改 kind 视图。task 层全量视图（对话 + 段摘要 + audit）= 旧
# _TASK_LAYER_TYPES/_TASK_BODY_TYPES 五类型；跨 task 聚合用半址（task_id=None）。
_TASK_VIEW_KINDS = [MemoryKind.CONVERSATION_TURN, MemoryKind.SUMMARY, MemoryKind.TOOL_AUDIT]

# Bind each history digest to the exact immutable rows it will replace. This is
# task-local: an observer running during the model await retains its own inputs.
_history_summary_ids: ContextVar[tuple[str, ...] | None] = ContextVar("history_summary_ids", default=None)
_DEFERRED_HISTORY_KEY = "_compact_deferred_history"
_SUMMARY_MAX_ATTEMPTS = 3

# ── 结构化 digest（spec: compact-fidelity）───────────────────────────────────
# 五小节契约（cue 见 composer 的两域 instruction）；Evidence 允许缺，其余必需。
DIGEST_SECTIONS = ("Goal", "Constraints", "Done", "Remaining", "Evidence")
REQUIRED_DIGEST_SECTIONS = DIGEST_SECTIONS[:4]

_DIGEST_SECTION_RE = {
    name: re.compile(rf"^## {name}[ \t]*\n(.*?)(?=^## |\Z)", re.S | re.M)
    for name in DIGEST_SECTIONS
}


@dataclasses.dataclass(frozen=True)
class ParsedDigest:
    """解析后的 digest：text 为落库形态（结构化时即原文），degraded 标降级。

    sections 供断言/评测消费（name → 小节正文；Evidence 可缺）。降级 = 整文作 digest
    （散文 digest 仍可用，不重试不中断——重试一次 LLM 调用换不回格式保证）。
    """

    text: str
    degraded: bool
    sections: dict[str, str] = dataclasses.field(default_factory=dict)


def parse_structured_digest(text: str) -> ParsedDigest:
    """宽松解析（spec: compact-fidelity）：抓 `## <Name>` 小节；Goal/Constraints/
    Done/Remaining 任一缺失（或空）→ 整文作 digest、degraded=True。旧散文 digest
    （本 change 之前的数据）天然走降级路径，无需迁移。"""
    sections: dict[str, str] = {}
    for name in DIGEST_SECTIONS:
        m = _DIGEST_SECTION_RE[name].search(text)
        if m and m.group(1).strip():
            sections[name] = m.group(1).strip()
    degraded = any(name not in sections for name in REQUIRED_DIGEST_SECTIONS)
    return ParsedDigest(text=text, degraded=degraded, sections=sections)


async def _resolve_digest(value) -> ParsedDigest:
    """把 summarize 的产物（ParsedDigest / 旧测试桩的裸 str / 取值的零参协程）归一。

    兼容裸 str 是刻意的：既有测试大量 monkeypatch summarize_for_compact 返回 str，
    落库点不应对替身形态敏感（str → 无小节 → degraded，语义自洽）。"""
    if dataclasses.is_dataclass(value) and isinstance(value, ParsedDigest):
        if not value.text.strip():
            raise LLMCallError("Compaction summary is empty; original records retained", retriable=False)
        return value
    if isinstance(value, str):
        return await _resolve_digest(parse_structured_digest(value))
    if inspect.isawaitable(value):
        return await _resolve_digest(await value)
    if callable(value):
        return await _resolve_digest(value())
    raise TypeError(f"unsupported digest payload: {type(value)!r}")


def _media_enabled(ctx) -> bool:
    """L0.5 与 §6.1 前置降级的总闸：只有真接了**可外部化**的 `MemoryBlobStore` 时才跑。

    未注册（`LoopContext.blob_store is None`）或注册的是 `NullMemoryBlobStore`
    （`can_externalize=False`）时，视图里根本不可能存在 `source_type == "ref"` 的图，
    降级必然返回 0；提前短路省掉整级的读操作，坐实子设计 §10「不接 MemoryBlobStore 时行为
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
) -> ParsedDigest:
    """装配 purpose="compact" 上下文并生成非空文本摘要，返回解析后的 digest。

    scope 选 cue（composer 据 extra["compact_scope"] 分流）："task"=整段执行摘要（默认，
    observe 兜底与坍缩共用）；"agent"=派发经验摘要。两域 cue 均要求五小节结构（spec:
    compact-fidelity）；产出经 parse_structured_digest 宽松解析——缺必需节整文降级
    （degraded=True）。空白或带工具调用的摘要最多尝试三次，耗尽后保留原记录并报错。

    LLM 摘要是 compact 的硬依赖（compact + observe 回退档共用本函数）：瞬时故障由
    stream_llm_resilient 自愈，自愈耗尽抛 LLMOutageError → 走 INTERRUPTED。**不再**在
    LLM 失败时静默退化为纯截断（旧的 except Exception 兜底已移除）。

    task-4 复审修复（第二轮）：``model`` 曾用 ``agent.runtime.get("llm_model", "mock")``
    ——inline compact 路径下 ``agent.runtime`` 从不会被写入 ``llm_model``（正常任务执行
    materialize() 出来的 Agent 就是空 runtime），这行代码因此在那条路径下恒回落
    ``"mock"``。改用同子系统其余调用方（act.py 等）已经在用的 ``resolve_llm_identity``
    ——真值来自 ``state.resolved_model``，是派发时 ``AgentLifecycleManager.resolve_model`` 解出的
    那一个，compact 的三条入口（PrepareStep / ActStep 派发前触发
    / ``compact_agent``，均由 driver 执行 CompactStep）用的都是已经带 ``resolved_model`` 的 ``LoopState``（前两者
    共享 ``_execute_task`` 构造的那份，同一份 state 上 act.py 的 ``resolve_llm_identity``
    早就在成功调用；后者是 ``runtime.py::compact_agent`` 显式 ``resolved_model=rm`` 构造
    的孤儿 run），故不额外加 try/except 兜底——真出现 None 就该炸，不吞。

    真实调查结论（供风险评估）：这次修复前，实际发给 LLM 服务商的模型**并不是**字面上的
    "mock"——``providers/llm/adapters/anthropic.py``/``openai.py`` 的 ``_build_payload`` 都已有
    ``model = request.model if request.model and request.model != "mock" else self._model``
    这行兜底替换（``anthropic.py`` 的 ``model`` 属性文档字符串原话：「实际调用一直用它替换
    "mock"……事件账面须与之同源」），而 ``self._model``（client 构造时配置的模型）与
    ``AgentLifecycleManager.resolve_model()`` 在 ``choice.model`` 为空时的回落值（``getattr(client,
    "model", "")``）同源，与 ``state.resolved_model.model`` 基本一致。也就是说**运行时
    行为本身不受影响**（真实 adapter 早已在暗中纠正这个 "mock" 哨兵），本次修复修的是
    「事件 payload 撒谎」而非「调错模型」——`docs/events-v2.md` §3.6 要求 model/llm_account
    是「实际用的那个」，撒谎的是事件，不是调用（`MockLLMAdapter` 例外：它没有这层兜底，
    ``last_request.model`` 会原样收到调用方传入的值——`compact.py` 里那处
    ``agent.runtime={"llm_model": rm.model}`` 桥接就是专给它搭的，见 `runtime.py`
    ``compact_agent``；本次改动后这座桥不再是唯一支撑，`resolve_llm_identity` 本身就
    正确，桥不拆但也不再是必需）。

    task-4 复审修复（第一轮）：compact 走的这次 LLM 调用现在也经 gateway 的门禁（origin==
    LOOP_COMPACT）自动获得 LLM_REQUEST_STARTED/LLM_PROMPT_SENT/LLM_TOKEN_STREAMED/
    LLM_REASONING_STREAMED；本函数补发收尾的 LLM_RESPONSE_FINISHED——两边必须成对，
    否则 host SSE 看到的是一条永远等不到收尾的挂死请求。payload 字段名/取值方式照抄
    act.py 那份（``_run_llm_turn``）。request_id 用同一个确定性公式独立算出（同
    act.py 的手法，见 llm_gateway.stream_llm_resilient 文档字符串）：两边都在本次
    调用任何事件发射前求值，天然一致，不需要新增参数或跨函数传值。

    收尾事件的发射条件与 gateway 那 4 个流式事件**同一个门禁**（``bus is not None`` 且
    ``state.origin == EventOrigin.LOOP_COMPACT``），而不是只看 ``bus``——否则本函数万一
    在别的 origin 下被调用（目前实际不会，但别指望调用方永远守规矩），会发一条没有配对
    REQUEST_STARTED 的孤儿 RESPONSE_FINISHED，比不发更糟。``bus is None``（多数单测的
    ctx 不接总线）时事件收发跳过——`state.sequence_counter` 在这些最小化 state 上不一定
    存在，故 request_id 的计算放在同一个 guard 里，不会在 bus 缺失时因缺字段而崩；
    ``resolve_llm_identity`` 则不受这个 guard 保护（见上，本就该在 bus 缺失时也正确解析、
    正确发送）。
    """
    agent = state.agent
    pending = getattr(state, "pending_act_turn", None)
    extra = {"compact_scope": scope,
             "exclude_memory_ids": [pending.assistant_record_id] if pending else []}
    selected = _history_summary_ids.get() if scope == "agent" else None
    if selected is not None:
        extra["observe_record_ids"] = list(selected)
    request = ContextRequest(
        purpose="compact",
        scope=state.scope,
        task=state.task,
        agent=agent,
        session=state.session,
        resolved_model=state.resolved_model,
        template=state.extra.get("template"),
        bound_capabilities=state.extra.get("bound_capabilities", []),
        token_counter=ctx.llm.tokenizer.count,
        extra=extra,
        intent=CompactIntent(scope="agent" if scope == "agent" else "task"),
    )
    inputs = await ctx.assembler.assemble(request)
    llm_request = await build_llm_request(ctx, state, inputs)
    model, llm_account = resolve_llm_identity(state)

    bus = getattr(ctx, "event_bus", None)
    # 同 gateway 那 4 个流式事件一个门禁：bus 存在 且 origin==LOOP_COMPACT（见上方 docstring）。
    should_emit_finished = bus is not None and getattr(state, "origin", None) == EventOrigin.LOOP_COMPACT
    for attempt in range(1, _SUMMARY_MAX_ATTEMPTS + 1):
        req_id = llm_request_id(state) if should_emit_finished else None
        stream = ensure_collected_stream(stream_llm_resilient(ctx, state, llm_request, inputs=inputs))
        async with aclosing(stream):
            async for _ in stream:
                stream.accept_current()
        result = stream.get_final_result()
        if should_emit_finished:
            await bus.emit(make_event(
                state, EventType.LLM_RESPONSE_FINISHED,
                payload={
                    "request_id": req_id, "content": result.text, "reasoning": result.reasoning,
                    "tool_calls": [dataclasses.asdict(tc) for tc in result.tool_calls],
                    "usage": dataclasses.asdict(result.usage),
                    "llm_model": model, "llm_account": llm_account,
                    "finish_reason": result.finish_reason or "stop", "turn": 0}))
        # Validate before creating any replacement MemoryEvent. Prose remains a
        # supported degraded digest; an empty or tool-only response cannot be one.
        if result.text.strip() and not result.tool_calls:
            return parse_structured_digest(result.text)
        if attempt < _SUMMARY_MAX_ATTEMPTS:
            logger.warning("Invalid compaction summary; retrying (%d/%d)", attempt, _SUMMARY_MAX_ATTEMPTS)
            if should_emit_finished:
                await bus.emit(make_event(state, EventType.LLM_RETRY_TRIGGERED, payload={
                    "request_id": req_id, "attempt": attempt, "max_attempts": _SUMMARY_MAX_ATTEMPTS,
                    "next_delay_sec": 0, "error_code": 0, "error": "Empty or non-text compaction summary"}))
    raise LLMCallError("Invalid compaction summary after 3 attempts; original records retained", retriable=False)


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


def _rendered_only(recs: list) -> list:
    """去掉装配时不渲染的记录（TOOL_AUDIT）。

    `AgentRecallSource` 只取对话回合 + 摘要进 prompt；审计记录只在 memory 里留痕。凡是
    「保留多少条给模型看」的计数都该按这个口径数，否则审计会白占名额。`getattr` 容忍测试桩。
    """
    return [r for r in recs if getattr(r, "kind", None) is not MemoryKind.TOOL_AUDIT]


def _split_for_collapse(
    recs: list, keep_last: int, *, protected_call_ids: set[str] | None = None,
    protected_record_ids: set[str] | None = None,
) -> tuple[list, list]:
    """把 task 层视图切成 (折区, 保留区)：保留**至少** keep_last 条会被渲染的记录，并对齐回合边界。

    两条规则，缺一条都会让保留区在模型眼里变空：

    1. **按渲染口径数名额**。TOOL_AUDIT 不进 prompt，不占 keep_last。
    2. **切点不落在一轮中间**。保留区里的每条工具结果，发起它的 assistant 也必须在保留区；
       否则结果成孤儿，发送前被 `drop_orphan_tool_results` 静默丢掉。一轮并行调两个工具时，
       旧的「最后 N 条记录」切法留下的是 `[result, audit, result]`，渲染出来 0 条。

    对齐只会让切点往前挪（保留更多），所以保留区可能超过 keep_last 条——keep_last 是下限。
    按 tool_call_id 找归属而不是「往前跳过连续的 tool 记录」：工具结果与 assistant 之间可能
    夹着别的记录（审计、异步回填），只看相邻关系会漏。
    """
    cut, seen = len(recs), 0
    if keep_last > 0:
        for i in range(len(recs) - 1, -1, -1):
            if getattr(recs[i], "kind", None) is MemoryKind.TOOL_AUDIT:
                continue
            seen, cut = seen + 1, i
            if seen >= keep_last:
                break
        if seen < keep_last:
            return [], list(recs)
    # An Act turn may be waiting for tools across compact -> prepare -> act.
    # Protect by call identity: media demotion can replace the record's ID.
    if protected_call_ids or protected_record_ids:
        protected_call_ids = protected_call_ids or set()
        protected_record_ids = protected_record_ids or set()
        cut = min([cut] + [i for i, r in enumerate(recs)
            if r.id in protected_record_ids or any(
                tc.get("id") in protected_call_ids
                for tc in r.metadata.get("tool_calls", []))])
    while True:
        kept_call_ids = {r.metadata.get("tool_call_id") for r in recs[cut:]
                         if r.role == "tool" and r.metadata.get("tool_call_id")}
        owner = next((i for i in range(cut) if recs[i].role == "assistant" and any(
            tc.get("id") in kept_call_ids for tc in (recs[i].metadata.get("tool_calls") or []))),
            None)
        groups = {task_record_group(r) for r in recs[cut:] if task_record_group(r)}
        group_owner = next((i for i in range(cut) if task_record_group(recs[i]) in groups), None)
        candidates = [i for i in (owner, group_owner) if i is not None]
        if not candidates:
            return recs[:cut], recs[cut:]
        cut = min(candidates)


def _worth_collapsing(fold: list) -> bool:
    """折区里除了**那一条最早的 user 回合**（原始请求 / 旧坍缩物）之外还有会被渲染的东西，才值得坍。

    折区只剩原始请求时，「坍缩」= 把 USER_PROMPT 换成「USER_PROMPT + 摘要」，token 不降反升，
    还白烧一次摘要 LLM。按回合切之后这种情况很常见：整个 task 只有一轮大并行调用时，对齐会把
    切点一路挪到那一轮的 assistant，折区里就只剩原始请求。

    只豁免**第一条** user 回合而不是所有 user 回合：连续多条用户消息（交互任务）合并成「第一条
    原文 + 摘要」仍然可能省 token，不该被挡掉。
    """
    rendered = _rendered_only(fold)
    first_user = next((r for r in rendered
                       if r.role == "user" and r.kind is MemoryKind.CONVERSATION_TURN
                       and not task_record_kind(r)), None)
    return any(r is not first_user for r in rendered)


async def collapse_task_layer(
    state, ctx, keep_last: int,
    summary_text: "str | ParsedDigest | Callable[[], Awaitable[str | ParsedDigest]]",
) -> int:
    """task compact（二级压缩）：把当前 task 层早期回合（含原始 USER_PROMPT 与 observer 的
    `## Progress So Far`）整体坍缩成一条新 USER_PROMPT，content = 原始消息 + COLLAPSE_DELIM +
    执行摘要；保留最近**至少** keep_last 条会被渲染的记录，切点对齐回合边界（见
    `_split_for_collapse`）。返回 supersede 条数；无可折之物 → 0，且不求值摘要。

    坍缩物是 USER_PROMPT 而非 assistant 摘要：composer 据 mtype=="user_prompt"+task_id 定位当前
    task 贴 `## Current Task/## Current Message` 框，故当前运行 task 坍缩后框架不丢；已结束胶囊被
    跨 task 召回时它就是一条背景 message。degraded 标记随 metadata 落库（spec: compact-fidelity）。

    `summary_text` 可以是算好的文本（兼容裸 str 的旧测试桩），也可以是**取摘要的零参 async
    函数**（`escalating_compact` 传的就是后者）：摘要在切分、no-op 判定、折区图片降级**之后**
    才求值——无可折之物时不花那次 LLM，且摘要输入里不会有即将被降级的真图（同 L1 的顺序）。
    """
    memory = ctx.memory
    pending = getattr(state, "pending_act_turn", None)
    protected_call_ids = {tc.id for tc in pending.record.tool_calls} if pending else set()
    protected_record_ids = {pending.assistant_record_id} if pending else set()
    recs = await load_records(memory,
        state.scope, MemoryScope.TASK, ctx.provider_ctx, kinds=_TASK_VIEW_KINDS)
    fold, kept = _split_for_collapse(recs, keep_last, protected_call_ids=protected_call_ids,
                                    protected_record_ids=protected_record_ids)
    if not _worth_collapsing(fold):
        return 0

    # §6.1：折叠范围内的残留真图**先无条件降级**（不受 keep_recent 保护）。L0.5 保住了
    # 最近 keep_recent 张，升级到这一级时它们恰好可能落进折区；不降的话下面取 `original`
    # 节走 content_to_text 会把它们静默拍扁——最老的图留下了可取回的占位，最新的反而
    # 痕迹全无，优先级完全颠倒。降级后 ref 随 `original` 节一起活下来。
    if _media_enabled(ctx) and await demote_all(
        memory, [r.id for r in fold], ctx.provider_ctx,
        address=state.scope, scope=MemoryScope.TASK, kinds=_TASK_VIEW_KINDS,
    ):
        await _refresh_media(state, ctx)
        # 🔴 必须重新 load_records：降级是 fold(旧 id, 新事件)，**record id 全换了**。
        # 拿降级前采集的 id 去下面那次 fold，被降过的一条都 supersede 不掉 → 同一段
        # 对话在视图里出现两次；且手里这份 recs 已过期，用它取 `original` 节拿到的仍是
        # 真图，照旧被拍扁，本段的目的完全落空。
        recs = await load_records(memory,
            state.scope, MemoryScope.TASK, ctx.provider_ctx, kinds=_TASK_VIEW_KINDS)
        fold, kept = _split_for_collapse(recs, keep_last, protected_call_ids=protected_call_ids,
                                        protected_record_ids=protected_record_ids)
        if not _worth_collapsing(fold):
            return 0

    # 「原始消息」节 = 折区最早一条 user 回合的原文（已坍缩过则取其原始节，保持有界）
    original = ""
    for r in fold:
        if r.kind is MemoryKind.CONVERSATION_TURN and r.role == "user" and not task_record_kind(r):
            text = r.content if isinstance(r.content, str) else content_to_text(r.content)
            original = _original_section(text)
            break

    # 锚：新 USER_PROMPT 须排在所有保留回合之前
    anchor_src = kept[0] if kept else fold[0]
    anchor_ts = anchor_src.timestamp - timedelta(microseconds=1)

    ids = [r.id for r in fold]
    digest = await _resolve_digest(summary_text)
    collapsed = f"{original}{COLLAPSE_DELIM}{digest.text}"
    # v2 P3d：遗忘+坍缩物一次原子 fold（旧徒手 supersede+ingest 有崩溃丢摘要窗口）
    await _fold_memory(state, ctx, ids, [
        MemoryEvent(
            kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK,
            address=state.scope,
            content=collapsed,
            timestamp=anchor_ts,
            role="user",
            metadata={"task_id": state.scope.task_id, "collapsed": True,
                      "keep_last": keep_last, "folded_count": len(fold),
                      "digest_degraded": digest.degraded},
            # 上面 §6.1 的前置降级把折区的真图换成了 L0.5 占位，`original` 节又把其中
            # 一部分逐字带进了坍缩物——那些 ref 于是**仍在视图里、仍被 get_image 定位得到**，
            # 而承载它们的原记录正要被这一次 fold 全部 supersede。占位是文本，GC 的 mark
            # 判据（`collect_blob_refs`）只看结构化字段，不声明就等于让引用归零：宽限期一过
            # 字节被回收，模型读到「call media:get_image(...) to bring it back」，换回来的却是
            # `[image unavailable]`——L0.5 承诺的「可逆」在这一步失效。
            # 只声明**幸存在坍缩物里**的那些（`placeholder_refs` 的口径），没进来的那些
            # 本就再也定位不到，一并声明只会让字节永远回收不掉。
            blob_refs=placeholder_refs(collapsed),
        ),
    ])
    return len(ids)


def _dispatch_finish_sets(recs) -> tuple[set, set]:
    """扫 conversation turn，按 origin 标记：has_dispatch（有 delegate/delegate_plan 调用回合）、
    has_finish（有 finish_task 调用回合）。`active = has_dispatch − has_finish` = 未结束的
    delegating task（在途 working set，不可折，spec §2.3）。"""
    has_dispatch: set = set()
    has_finish: set = set()
    for r in recs:
        if r.kind is not MemoryKind.CONVERSATION_TURN:
            continue
        oid = capsule_origin(r)
        if oid is None:
            continue
        kind = task_record_kind(r)
        if kind == "start":
            has_dispatch.add(oid)
        elif kind == "finish":
            has_finish.add(oid)
        for tc in (r.metadata.get("tool_calls") or []):
            name = str(tc.get("name", ""))
            if name.endswith("finish_task"):
                has_finish.add(oid)
            elif name.endswith("delegate_task") or name.endswith("delegate_plan"):
                has_dispatch.add(oid)
    return has_dispatch, has_finish


async def _active_memory_tokens(state: LoopState, ctx: LoopContext) -> int:
    """Count rendered history messages with prepare/gateway's full billing items.

    Within a compaction run, reuse immutable rows and unaffected task trees.
    System instructions and tool definitions stay in the caller's input estimate.
    """
    snapshot = current_snapshot(ctx)
    if snapshot is not None:
        await _history_view(state, ctx)
        return snapshot.tokens()
    body = await load_records(ctx.memory,
        _agent_half(state.scope), MemoryScope.TASK, ctx.provider_ctx, kinds=_TASK_VIEW_KINDS)
    agent_recs = await load_records(ctx.memory,
        _agent_half(state.scope), MemoryScope.AGENT, ctx.provider_ctx)
    from ctx_weft.core.assembler.builder import compile_history
    messages = compile_history([*body, *agent_recs], task_id=state.scope.task_id).messages
    return sum(estimate_message_tokens(m, ctx.llm.tokenizer.count) for m in messages)


@dataclasses.dataclass
class _HistoryUnits:
    parents: dict[str, Any]
    first_ts: dict[str, Any]
    roots: list[str]

    def expand(self, roots) -> set[str]:
        found = set(roots)
        while True:
            expanded = found | {oid for oid, parent in self.parents.items() if parent in found}
            if expanded == found:
                return found
            found = expanded


def _history_units(recs, current, body=()) -> _HistoryUnits:
    """One origin/ancestry policy for history demotion and folding.

    A labeled start describes a *child*, while origin identifies its owner.
    Historical starts accidentally stored owner == parent; never adopt that
    self-edge. An owner's closure is authoritative, including a root's None.
    """
    from ctx_weft.core.assembler.legacy_task_memory import normalize_legacy_tasks

    parents, ranks, first_ts = {}, {}, {}
    projected = normalize_legacy_tasks(recs)
    has_dispatch, has_finish = _dispatch_finish_sets(recs)
    for r in projected:
        if r.kind != MemoryKind.CONVERSATION_TURN:
            continue
        oid = capsule_origin(r)
        if oid is None:
            continue
        kind = task_record_kind(r)
        parent = r.metadata.get("parent_task_id") or None
        if parent == oid:
            parent = None
        if kind in {"start", "result"} and not r.metadata.get("legacy_task_record"):
            parent = None  # These labels describe the dispatched child.
        rank = 2 if kind == "finish" else 1 if kind in {"recap", "final_reply"} else 0
        if (oid not in parents or rank > ranks[oid]
                or rank == ranks[oid] and parents[oid] is None and parent is not None):
            parents[oid], ranks[oid] = parent, rank
        if oid not in first_ts or r.timestamp < first_ts[oid]:
            first_ts[oid] = r.timestamp
        if kind == "finish":
            has_finish.add(oid)
    # A late segment can outlive the closure IDs folded by an earlier snapshot.
    # Keep only these residual task identities, so later compactions can still
    # select their details even when no live closure remains.
    residuals = {oid for r in recs for oid in r.metadata.get("residual_task_ids", [])}
    for r in (body if residuals else ()):
        oid = r.address.task_id
        if oid in residuals and oid not in parents:
            parents[oid], first_ts[oid] = None, r.timestamp
    has_result = {capsule_origin(r) for r in projected
                  if (r.role == "tool" or task_record_kind(r) == "result")
                  and r.metadata.get("dispatch_state") != "running"}
    active = has_dispatch - has_finish - has_result
    closed_children = {r.metadata.get("child_task_id") for r in projected
                       if task_record_kind(r) == "result"
                       and r.metadata.get("dispatch_state") != "running"}
    open_owners = {capsule_origin(r) for r in projected
                   if task_record_kind(r) == "start" and r.metadata.get("child_task_id")
                   and r.metadata["child_task_id"] not in has_finish | closed_children}
    units = _HistoryUnits(parents, first_ts, [])
    # A live branch protects its ancestors, but not its completed siblings or
    # children. Select the largest completed subtrees below those ancestors.
    legacy_closed = {capsule_origin(r) for r in projected
                     if r.kind == MemoryKind.CONVERSATION_TURN and r.role == "assistant"
                     and task_record_kind(r) is None and not r.metadata.get("tool_calls")}
    closed = has_finish | has_result | legacy_closed | residuals
    protected = {current} | active | open_owners | (parents.keys() - closed)
    for oid in list(protected):
        parent = parents.get(oid)
        while parent is not None and parent not in protected:
            protected.add(parent)
            parent = parents.get(parent)
    units.roots = [oid for oid, parent in parents.items()
                   if oid not in protected and (parent not in parents or parent in protected)]
    units.roots.sort(key=lambda oid: first_ts[oid])
    return units


async def _history_view(state, ctx):
    snapshot = current_snapshot(ctx)
    if snapshot is not None:
        if await snapshot.refresh():
            state.extra[_DEFERRED_HISTORY_KEY] = True
        return snapshot.view()
    address = _agent_half(state.scope)
    turns = await load_records(ctx.memory, address, MemoryScope.AGENT, ctx.provider_ctx)
    body = await load_records(ctx.memory, address, MemoryScope.TASK, ctx.provider_ctx,
                              kinds=_TASK_VIEW_KINDS)
    return turns, body


async def _refresh_media(state, ctx):
    snapshot = current_snapshot(ctx)
    if snapshot is not None:
        await snapshot.refresh(known_mutation=True)


async def _fold_memory(state, ctx, ids, replacements, *, preserve_plans=False):
    snapshot = current_snapshot(ctx)
    if snapshot is not None:
        result = await snapshot.fold(ids, replacements)
        if not preserve_plans:
            snapshot.plans = None
        return result
    return await ctx.memory.fold(ids, replacements, ctx.provider_ctx)


@asynccontextmanager
async def _history_commit(state, ctx, origins):
    """Serialize commit with observer writes, never with model calls."""
    registry = StepDriver.for_context(ctx).executions
    async with AsyncExitStack() as stack:
        for oid in sorted(origins):
            for step in ("observe", "finish_report"):
                await stack.enter_async_context(registry.task_guard(oid, step))
        await stack.enter_async_context(assistant_memory_checkpoint(state, ctx))
        yield


async def _count_root_residues(state: LoopState, ctx: LoopContext) -> int:
    recs, body = await _history_view(state, ctx)
    return len(_history_units(recs, getattr(state.scope, "task_id", None), body).roots)


def _history_selection(state, recs, body, keep_last, *, lean_only=False):
    units = _history_units(recs, getattr(state.scope, "task_id", None), body)
    roots = units.roots
    if lean_only and roots:
        lean_roots = []
        body_origins = {r.address.task_id for r in body}
        non_lean = {capsule_origin(r) for r in recs if r.kind == MemoryKind.CONVERSATION_TURN
                    and (task_record_kind(r) != "finish"
                         or r.metadata.get("finish_policy", {}).get("capsule_state") != "lean")}
        for oid in roots:
            if not units.expand([oid]) & (body_origins | non_lean):
                lean_roots.append(oid)
        roots = lean_roots
    roots = roots if keep_last <= 0 else roots[:-keep_last]
    origins = units.expand(roots)
    ids = [r.id for r in body if r.address.task_id in origins] if origins else []
    ids += [r.id for r in recs
            if (r.kind == MemoryKind.CONVERSATION_TURN and capsule_origin(r) in origins)
            or (r.kind == MemoryKind.SUMMARY and (roots or keep_last <= 0)
                and (not lean_only or capsule_origin(r) is None))]
    return units, roots, origins, ids


async def fold_root_experience(
        state: LoopState, ctx: LoopContext, keep_last: int,
        summary_text: "str | ParsedDigest | Callable[[], Awaitable[str | ParsedDigest]]",
        *, lean_only: bool = False,
) -> int:
    """Summarize an immutable selection, then fold exactly those record IDs.

    Observers may replace or append records during the model await. Those later
    records are outside this snapshot: preserve them without regenerating this
    digest or expanding its deletion set.
    """
    recs, body = await _history_view(state, ctx)
    _, roots, origins, ids = _history_selection(state, recs, body, keep_last, lean_only=lean_only)
    if not ids:
        return 0

    if _media_enabled(ctx):
        await demote_all(ctx.memory, ids, ctx.provider_ctx, address=_agent_half(state.scope),
                         scope=MemoryScope.TASK, kinds=_TASK_VIEW_KINDS)
        await demote_all(ctx.memory, ids, ctx.provider_ctx, address=_agent_half(state.scope),
                         scope=MemoryScope.AGENT)
        await _refresh_media(state, ctx)
        recs, body = await _history_view(state, ctx)
        _, roots, origins, ids = _history_selection(state, recs, body, keep_last, lean_only=lean_only)
        if not ids:
            return 0

    selected_ids = set(ids)
    kept_ts = [r.timestamp for r in [*recs, *body] if r.id not in selected_ids]
    anchor = min(kept_ts) if kept_ts else max(r.timestamp for r in [*recs, *body])
    token = _history_summary_ids.set(tuple(ids))
    try:
        digest = await _resolve_digest(summary_text)
    finally:
        _history_summary_ids.reset(token)

    async with _history_commit(state, ctx, origins):
        live_recs, live_body = await _history_view(state, ctx)
        prior_residuals = {oid for r in recs for oid in r.metadata.get("residual_task_ids", [])}
        residuals = {r.address.task_id for r in live_body
                     if r.id not in selected_ids and r.address.task_id in origins | prior_residuals}
        late = any(r.id not in selected_ids and capsule_origin(r) in origins for r in live_recs)
        if (late or residuals) and hasattr(state, "extra"):
            state.extra[_DEFERRED_HISTORY_KEY] = True
        text = digest.text
        metadata = {"keep_last": keep_last, "folded_count": len(roots),
                    "digest_degraded": digest.degraded}
        if residuals:
            metadata["residual_task_ids"] = sorted(residuals)
        await _fold_memory(state, ctx, ids, [MemoryEvent(
            kind=MemoryKind.SUMMARY, scope=MemoryScope.AGENT, address=state.scope,
            content=text, timestamp=anchor - timedelta(microseconds=1), role="user",
            metadata=metadata,
            blob_refs=placeholder_refs(text),
        )])
    return len(ids)


async def demote_kept_capsules(
    state: LoopState, ctx: LoopContext, origin_ids: set, *, repair_summary=None,
) -> int:
    plans = await _demotion_plans(state, ctx)
    origins = set().union(*(plans[oid].origins for oid in origin_ids if oid in plans))
    async with _history_commit(state, ctx, origins):
        # Re-read view IDs under the guards; rebuild plans if an observer wrote.
        plans = await _demotion_plans(state, ctx)
        selected = {oid: plans[oid] for oid in origin_ids if oid in plans}
        if any(not p.origins <= origins for p in selected.values()):
            state.extra[_DEFERRED_HISTORY_KEY] = True
            return 0
        n = await _demote_kept_capsules(state, ctx, set(selected), plans=selected)
    if repair_summary is not None:
        for oid in sorted(oid for oid, plan in selected.items() if plan.needs_repair and not plan.demotable):
            n += await _repair_history_capsule(state, ctx, oid, repair_summary)
    return n


async def _repair_history_capsule(state, ctx, origin, summary_text) -> int:
    """Repair missing/late history summaries without holding observer write locks.

    The usual rich→lean path reuses a finish summary without an LLM. Missing
    summaries and post-fold residuals need their own immutable digest snapshot.
    """
    from ctx_weft.core.assembler.legacy_task_memory import normalize_legacy_tasks
    from ctx_weft.core.loop.steps.observe import record_id

    async def snapshot():
        recs, body = await _history_view(state, ctx)
        units = _history_units(recs, getattr(state.scope, "task_id", None), body)
        origins = units.expand([origin]) if origin in units.roots else set()
        rows = [r for r in body if r.address.task_id in origins]
        rows += [r for r in recs if r.kind == MemoryKind.CONVERSATION_TURN
                 and capsule_origin(r) in origins]
        closing = next((r for r in normalize_legacy_tasks(recs)
                        if capsule_origin(r) == origin and task_record_kind(r) == "finish"), None)
        return units, origins, rows, closing

    units, origins, rows, closing = await snapshot()
    if not rows or (len(rows) == 1 and closing is not None
                    and closing.metadata.get("finish_policy", {}).get("capsule_state") == "lean"
                    and closing.metadata.get("finish_policy", {}).get("has_summary") is not False):
        return 0
    if _media_enabled(ctx):
        for layer in (MemoryScope.TASK, MemoryScope.AGENT):
            await demote_all(ctx.memory, [r.id for r in rows], ctx.provider_ctx,
                             address=_agent_half(state.scope), scope=layer)
        await _refresh_media(state, ctx)
        units, origins, rows, closing = await snapshot()
        if not rows:
            return 0
    ids = [r.id for r in rows]
    token = _history_summary_ids.set(tuple(ids))
    try:
        digest = await _resolve_digest(summary_text)
    finally:
        _history_summary_ids.reset(token)
    if not digest.text.strip():
        return 0
    metadata = dict(closing.metadata) if closing is not None else {}
    metadata.pop("legacy_task_record", None)
    metadata.update({"origin_task_id": origin, "parent_task_id": units.parents.get(origin),
                     "task_memory": {"version": 1, "kind": "finish",
                                     "group_id": (task_record_group(closing) or origin) if closing else origin},
                     "digest_degraded": digest.degraded})
    metadata["finish_policy"] = {**metadata.get("finish_policy", {}), "has_summary": True,
                                 "capsule_state": "lean", "slot_count": 1, "preserve_raw": False}
    metadata["finish_policy"].pop("final_reply_record_id", None)
    content = runtime_prompt("task_memory_record", record=dict(
        kind="finish", task_id=origin, parent_task_id=units.parents.get(origin) or "",
        title=origin, description="", outcome=metadata["finish_policy"].get("outcome", "unknown"),
        body=digest.text)).strip()
    async with _history_commit(state, ctx, origins):
        live_recs, live_body = await _history_view(state, ctx)
        if origin not in _history_units(live_recs, getattr(state.scope, "task_id", None), live_body).roots:
            state.extra[_DEFERRED_HISTORY_KEY] = True
            return 0
        late = [r for r in live_body if r.address.task_id in origins and r.id not in ids]
        late += [r for r in live_recs if capsule_origin(r) in origins and r.id not in ids]
        if late:
            state.extra[_DEFERRED_HISTORY_KEY] = True
            metadata["residual_task_ids"] = sorted(origins)
        await _fold_memory(state, ctx, ids, [MemoryEvent(
            id=record_id("repair_capsule", *ids), kind=MemoryKind.CONVERSATION_TURN,
            scope=MemoryScope.AGENT, address=_agent_half(state.scope), role="user", content=content,
            timestamp=closing.timestamp if closing else max(r.timestamp for r in rows),
            metadata=metadata, blob_refs=placeholder_refs(content),
        )])
    return len(ids)


async def _demotion_plans(state, ctx):
    recs, body = await _history_view(state, ctx)
    snapshot = current_snapshot(ctx)
    if snapshot is not None and snapshot.plans is not None:
        return snapshot.plans
    plans = _plan_capsules(recs, body, getattr(state.scope, "task_id", None))
    if snapshot is not None:
        snapshot.plans = plans
    return plans


def _plan_capsules(turns, body, current):
    """Index disjoint completed subtrees once, preserving the selection policy."""
    from collections import defaultdict

    from ctx_weft.core.assembler.legacy_task_memory import normalize_legacy_tasks

    turns = [r for r in turns if r.kind == MemoryKind.CONVERSATION_TURN]
    projected = normalize_legacy_tasks(turns)
    finishes = {capsule_origin(r): r for r in projected
                if task_record_kind(r) == "finish"}
    # Earliest agent capsules stored a user/assistant closure without task-memory
    # labels. Their assistant body is already the finish summary, not task raw.
    for r in reversed(projected):
        if (r.role == "assistant" and task_record_kind(r) is None
                and not r.metadata.get("tool_calls") and capsule_origin(r) is not None):
            finishes.setdefault(capsule_origin(r), r)
    units = _history_units(turns, current, body)
    closed_children = {r.metadata.get("child_task_id") for r in projected
                       if task_record_kind(r) == "result"
                       and r.metadata.get("dispatch_state") != "running"}
    children, records = defaultdict(list), defaultdict(list)
    for oid, parent in units.parents.items():
        children[parent].append(oid)
    for row in body:
        records[row.address.task_id].append(row)
    for row in turns:
        records[capsule_origin(row)].append(row)
    open_owners = {capsule_origin(r) for r in projected
                   if task_record_kind(r) == "start" and r.metadata.get("child_task_id")
                   and r.metadata["child_task_id"] not in finishes.keys() | closed_children}
    plans = {}
    for origin in units.roots:
        descendants, todo = set(), [origin]
        while todo:
            oid = todo.pop()
            if oid not in descendants:
                descendants.add(oid)
                todo.extend(children[oid])
        rows = tuple(r for oid in sorted(descendants) for r in records[oid])
        closing = finishes.get(origin)
        policy = closing.metadata.get("finish_policy", {}) if closing else {}
        demotable = bool(closing is not None and policy.get("has_summary") is not False
                         and policy.get("capsule_state") != "lean" and not descendants & open_owners)
        needs_repair = bool(rows) and not (len(rows) == 1 and closing is not None
                            and policy.get("capsule_state") == "lean" and policy.get("has_summary") is not False)
        plans[origin] = CapsulePlan(frozenset(descendants), rows, closing, demotable, needs_repair)
    return plans


async def _demote_kept_capsules(state, ctx, origin_ids, *, plans=None) -> int:
    """Replace each eligible completed subtree with its existing finish summary."""
    from ctx_weft.core.loop.steps.observe import record_id

    plans = await _demotion_plans(state, ctx) if plans is None else plans
    total = 0
    for origin in origin_ids:
        plan = plans.get(origin)
        if plan is None or not plan.demotable or plan.closing is None:
            continue
        closing = plan.closing
        ids = [r.id for r in plan.rows]
        policy = {**closing.metadata.get("finish_policy", {}), "capsule_state": "lean",
                  "has_summary": True, "slot_count": 1, "preserve_raw": False}
        policy.pop("final_reply_record_id", None)
        metadata = {**closing.metadata, "finish_policy": policy}
        if task_record_kind(closing) is None:
            metadata["task_memory"] = {"version": 1, "kind": "finish", "group_id": closing.id}
        lean = MemoryEvent(
            kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.AGENT,
            address=closing.address, content=(runtime_prompt("task_memory_record", record=closing.metadata["legacy_task_record"]).strip()
                if closing.metadata.get("legacy_task_record") else closing.content), timestamp=closing.timestamp,
            role="user", id=record_id("demote_capsule", closing.id), blob_refs=closing.blob_refs,
            metadata=metadata)
        lean.metadata.pop("legacy_task_record", None)  # Read-only normalization facts are not storage.
        await _fold_memory(state, ctx, ids, [lean], preserve_plans=True)
        snapshot = current_snapshot(ctx)
        if snapshot is not None and snapshot.plans is not None:
            snapshot.plans.pop(origin, None)
        total += len(ids)
    return total



async def _kept_origin_ids(state: LoopState, ctx: LoopContext, keep_last: int | None) -> list:
    """Completed history, oldest first; None selects all for L1 demotion."""
    recs, body = await _history_view(state, ctx)
    roots = _history_units(recs, getattr(state.scope, "task_id", None), body).roots
    if keep_last is None:
        if current_snapshot(ctx) is not None:
            plans = await _demotion_plans(state, ctx)
            return [oid for oid in roots if oid in plans and (plans[oid].demotable or plans[oid].needs_repair)]
        return roots
    return roots[-keep_last:] if keep_last > 0 else []


async def _old_history_remaining(state, ctx) -> bool:
    """Do completed old tasks still have execution details rather than a summary?"""
    recs, body = await _history_view(state, ctx)
    units = _history_units(recs, getattr(state.scope, "task_id", None), body)
    origins = units.expand(units.roots)
    if not origins:
        return False
    if any(r.address.task_id in origins for r in body):
        return True
    return any(capsule_origin(r) in origins
               and task_record_kind(r) in {"recap", "final_reply"} for r in recs)


async def escalating_compact(
    state: LoopState, ctx: LoopContext, *, token_estimate: int, trigger: str = "compact", force: bool = False
) -> list[Any]:
    """Run L0.5 → L1 → L2 → L3 with a cache scoped to this foreground call."""
    with compact_snapshot(state, ctx):
        return await _escalating_compact(state, ctx, token_estimate=token_estimate, trigger=trigger, force=force)


async def _escalating_compact(
    state: LoopState, ctx: LoopContext, *, token_estimate: int, trigger: str, force: bool,
) -> list[Any]:
    """预算驱动升级式 compact：L0.5 图片降级 → L1 rich→lean → L2 lean 历史归并 → L3 当前 task，
    每级后把历史消息 token 的净变化加到 token_estimate，降到 target 以下即停。
    级间不完整重装配（Q4=c，调用方进 act 前重装配一次校正）。无 context_limit 或已达标 → []。"""
    agent = state.agent
    lc = agent.loop_config
    eff = state.resolved_model.effective_input_limit
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
    state.extra.pop(_DEFERRED_HISTORY_KEY, None)
    if est < target_tokens and not force:
        return []
    # Same-agent foreground runs are serialized by TaskManager. No model await
    # belongs under this lock; background observe pins its own IDs under it.
    async with assistant_memory_checkpoint(state, ctx):
        pass
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

    level_payloads: list[dict[str, Any]] = []

    def _finish(evts: list[Any]) -> list[Any]:
        """收尾：聚合本轮各级 MemoryCompacted，追加一条 MemoryCompactFinished。"""
        # Allocate sequences after all immediately streamed model events.
        evts.extend(make_event(state, EventType.MEMORY_COMPACTED, payload=payload)
                    for payload in level_payloads)
        folded = [e for e in evts if e.type == EventType.MEMORY_COMPACTED]
        evts.append(make_event(state, EventType.MEMORY_COMPACT_FINISHED, payload={
            "task_id": state.task.id, "agent_id": agent.id, "trigger": trigger,
            "total_superseded": sum(e.payload.get("superseded_count", 0) for e in folded),
            "freed_tokens": sum(e.payload.get("freed_tokens", 0) for e in folded),
            "levels": [e.payload.get("source", "") for e in folded],
            "est_before": token_estimate, "est_after": est, "target_tokens": target_tokens}))
        return evts

    last_tokens: int | None = None

    async def _apply(level_coro, *, media=False):
        """跑一级折叠，用活跃 token before/after 增量累减 est。返回 (superseded_count, freed)。

        before 惰性复用上一级的 after（相邻级间省一次重复测量）；首次或跳级后重新测量。"""
        nonlocal est, last_tokens
        # Keep the original baseline: an observer's intervening writes count as
        # growth on the next measurement, rather than being silently rebased away.
        # 历史归并的「前置降级 + 摘要 + fold」整体作为**一个** level_coro 交给本函数：
        # 把降级提到 _apply 之外会破坏这个前提，且降级省下的 token 要么被重复计入、要么彻底丢掉。
        before = last_tokens if last_tokens is not None else await _active_memory_tokens(state, ctx)
        n = await level_coro if inspect.isawaitable(level_coro) else level_coro
        if media:
            await _refresh_media(state, ctx)
        after = await _active_memory_tokens(state, ctx)
        delta = after - before
        est += delta
        freed = max(0, -delta)
        last_tokens = after
        return n, freed

    async def _checkpoint_budget():
        """Check a fresh full history before advancing to another lossy level."""
        nonlocal est, last_tokens
        snapshot = current_snapshot(ctx)
        if snapshot is not None and snapshot.ids and last_tokens is not None:
            await _history_view(state, ctx)
            after = snapshot.tokens(full=True)
            est += after - last_tokens
            last_tokens = after

    # L0.5 · 图片降级（子设计 §6：排在所有折叠之前）
    # 三条理由：无 LLM（不花一次调用）、单位收益最高（一张图按当前口径最低 1600 token，
    # 满额 5MB 图 40960）、**可逆**（占位仍在原位，模型随时 media:get_image 取回）。
    # L1/L2/L3 折的是记录本身，一旦执行位置就没了；所以先花可逆的额度。
    # 未接 MemoryBlobStore 时 _media_enabled 直接短路，本级连一次 memory 读都不发（§10）。
    if _media_enabled(ctx):
        n, freed = await _apply(demote_for_budget(
            ctx.memory, _agent_half(state.scope), ctx.provider_ctx,
            keep_recent=keep_recent_images, scope=MemoryScope.TASK,
            kinds=_TASK_VIEW_KINDS), media=True)
        if n:
            # 注：本级的 n 是**图片张数**，不是记录条数（记录并没有被折走，只是被重写）。
            # 仍填进 superseded_count 以与各级同构、让 MemoryCompactFinished 的聚合口径
            # 不必分叉；真正无歧义的计数在 demoted_images。
            level_payloads.append({
                "superseded_count": n, "demoted_images": n, "layer": "task",
                "source": "demote_images", "trigger": trigger, "freed_tokens": freed})
    if est < target_tokens and not force:
        return _finish(events)

    # L1 · All completed rich histories are candidates, including completed
    # branches below a live task. Demote oldest first and stop as soon as we fit.
    # Reuse finish summaries; only missing summaries/residuals need an LLM repair.
    l1_n = l1_freed = l1_capsules = 0
    for oid in await _kept_origin_ids(state, ctx, None):
        if est < target_tokens and not force:
            break
        n, freed = await _apply(demote_kept_capsules(
            state, ctx, {oid},
            repair_summary=lambda: summarize_for_compact(state, ctx, scope="agent")))
        if n:
            l1_n += n
            l1_freed += freed
            l1_capsules += 1
        if state.extra.get(_DEFERRED_HISTORY_KEY):
            break
    if l1_n:
        level_payloads.append({
            "superseded_count": l1_n, "layer": "agent", "source": "demote_lean",
            "trigger": trigger, "freed_tokens": l1_freed, "demoted_capsules": l1_capsules})
    await _checkpoint_budget()
    if (state.extra.get(_DEFERRED_HISTORY_KEY)
            or est < target_tokens and not force
            or await _old_history_remaining(state, ctx)):
        return _finish(events)

    # L2 · Summarize lean histories and prior digests. Prefer keeping the most
    # recent capsules, but exhaust that preference before sacrificing live work.
    # The callable remains lazy: no selected history means no model invocation.
    l2_n = l2_freed = 0
    for history_keep in ((0,) if force else dict.fromkeys((keep_last, 0))):
        n, freed = await _apply(fold_root_experience(
            state, ctx, history_keep,
            lambda: summarize_for_compact(state, ctx, scope="agent"), lean_only=True))
        l2_n += n
        l2_freed += freed
        if state.extra.get(_DEFERRED_HISTORY_KEY) or est < target_tokens and not force:
            break
    if l2_n:
        level_payloads.append({
            "superseded_count": l2_n, "layer": "agent", "source": "root_experience",
            "trigger": trigger, "freed_tokens": l2_freed})
    await _checkpoint_budget()
    if (state.extra.get(_DEFERRED_HISTORY_KEY)
            or est < target_tokens and not force
            or await _old_history_remaining(state, ctx)):
        return _finish(events)

    # L3 · 坍缩当前 task（段摘要坍成更少，保 collapse_keep 条；仅当有 task 层材料可折）
    # 计数用 TASK 视图里**会被渲染**的记录（对话回合 + SUMMARY 段摘要）——与
    # collapse_task_layer 的保留名额同口径：retry 累积的是段摘要，只数 raw 会漏计、L3 永不
    # 触发；而 TOOL_AUDIT 不进 prompt，算进来会让门槛虚高、提前开门。
    task_n = len(_rendered_only(await load_records(ctx.memory,
        state.scope, MemoryScope.TASK, ctx.provider_ctx, kinds=_TASK_VIEW_KINDS)))
    if task_n > collapse_keep:
        # 传「取摘要的函数」而非摘要文本（同 L2）：按回合切之后 L3 可能发现无可折之物而
        # no-op，先算好就白烧一次 LLM；且折区图片降级发生在 collapse_task_layer 内部，
        # 摘要必须在那之后生成，否则摘要输入里仍是真图。
        n, freed = await _apply(collapse_task_layer(
            state, ctx, collapse_keep,
            lambda: summarize_for_compact(state, ctx, scope="task")))
        if n:
            level_payloads.append({
                "superseded_count": n, "layer": "task", "source": "collapse",
                "trigger": trigger, "freed_tokens": freed})
    logger.info("escalating_compact[%s]: agent=%s task=%s est→%d target=%d",
                trigger, agent.id, state.task.id, est, target_tokens)
    return _finish(events)


class CompactStep(Step):
    """Foreground compaction with an explicit continuation supplied by the caller."""

    name = "compact"

    async def execute(self, state: LoopState, ctx: LoopContext) -> StepOutcome:
        options = getattr(getattr(ctx, "execution", None), "options", {})
        after = options.get("after")
        if after not in (None, "prepare"):
            raise ValueError(f"Unsupported compact continuation: {after!r}")
        guard = state.agent.loop_guard
        token_estimate = options.get("token_estimate", guard.context_tokens)
        # The request has captured the old estimate. Any level may mutate memory,
        # including before a later level fails; invalidate its incremental baseline.
        guard.context_tokens = 0
        guard.context_message_count = 0
        events = await escalating_compact(
            state, ctx, token_estimate=token_estimate,
            trigger=options.get("trigger", "compact"), force=options.get("force", False))
        state.extra[COMPACT_NOOP_KEY] = not any(
            ev.type == EventType.MEMORY_COMPACTED for ev in events)
        return StepOutcome(
            next_step=after,
            next_step_options={"after_compact": True} if after == "prepare" else {},
            state_patch={"prompt_inputs": None}, events=events,
        )
