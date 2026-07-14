"""LLM 请求发送的统一关口：集中所有「发送前合法化」方法 + 流式转发。

四个 step（act / observe / compact / recognize_intent）各自构造 ``LLMRequest`` 后
都经 :func:`stream_llm` 发送，使「发送前合法化」成为**唯一**关口：装配层（composer）
只负责构造逻辑消息结构，不做合法化；连续同角色 / 孤立 tool result 一律在这里、发送前
统一处理。

合法化六条不变式（:func:`legalize_messages` 内顺序：剥离悬挂 tool_call → 把 tool result 挪到
其 tool_call 之后 → 删空 content → 丢前导非 user → 丢孤立 tool result → 合并连续同角色）：
  1. :func:`drop_dangling_tool_calls` —— 剥离无后继 tool_result 配对的 assistant tool_call。
     reconcile/act 正常已补齐 dangling（见模块尾注），此处是发送前最后**防御性**兜底：只删
     不补、命中即打 ERROR 日志（说明上游对账漏了）。Anthropic 对悬挂 tool_use 直接 400。
  2. :func:`reorder_tool_results_after_calls` —— 把每条 tool result 紧挪到其 assistant
     tool_call 之后。跨层按 timestamp 归并会把 user 回合插进 assistant↔result 之间（OpenAI
     兼容端点据此 400），集合级配对查不出这种错位——本步按 owner 重排消除中间夹角色。只搬不删。
  3. :func:`remove_empty_messages` —— 删除内容为空且无 tool_calls 的 user/assistant 消息
     （Anthropic 对空 content 块 400）。同为防御性兜底，命中打 ERROR 日志。① 把全悬挂的
     assistant 剥成空消息后，正好由此清理。tool 消息即便空也不在此删（删了会制造悬挂）。
  4. :func:`ensure_leading_user` —— 丢弃开头 role != "user" 的消息直到首条为 user（Anthropic
     硬规则，否则 400）。丢弃前导 assistant 后其配对 tool 会成孤儿，由下一步清理。
  5. :func:`drop_orphan_tool_results` —— 丢弃 tool_call_id 无前序 assistant tool_call
     配对的 ``role="tool"`` 消息。compact 丢弃 assistant 但保留 TOOL_RESULT、recall 窗口
     在 assistant↔result 之间截断、跨层按 timestamp 归并错位等都会产生此类孤儿，会被
     Anthropic / OpenAI 直接 400。
  6. :func:`merge_consecutive_messages` —— 合并连续同角色消息（多模态安全）。

本模块仅依赖 protocols，且只被本 loop 层的 step import；故置于 ``core/loop`` 下。若将来
装配等下层也需复用这些合法化函数，应改为下沉到 ``core`` 根，避免下层反向依赖 loop。

注：「悬挂 assistant tool_call（无后继 result）」的**正路**是上游对账补 TOOL_RESULT
（resume 前的 ReconcileStep 重跑工具、act 为被打断工具补 result），本关口的
:func:`drop_dangling_tool_calls` 只是发送前防 400 的兜底，命中打 ERROR 提示上游漏补。
adapter 不应重复实现以上不变式——已在 core 层集中保证。

另注：「prompt 必须以 user 收尾」不下沉到此处——那是 act 装配期的语义兜底（注入具体
文案、且 provider 并不强制），若在此每个 turn 强制，会在工具循环里于 tool result 之后误
插一条 user，篡改正常的工具调用流。该兜底保留在 composer._build_actor_messages。
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator
from time import monotonic
from typing import TYPE_CHECKING

import json
from ctx_weft.protocols import LLMMessage, LLMOutageError, TextPart
from ctx_weft.core.events.types import EventType
from ctx_weft.core.loop.driver import make_event
from ctx_weft.core.utils import content_to_text, dynamic_max_tokens, estimate_tokens

if TYPE_CHECKING:
    from ctx_weft.protocols import ContentPart, LLMChunk, LLMClient, LLMRequest

logger = logging.getLogger(__name__)


def drop_dangling_tool_calls(messages: list[LLMMessage]) -> list[LLMMessage]:
    """剥离无后继 tool_result 配对的 assistant tool_call（防御性，命中打 ERROR 日志）。

    正路是上游对账补齐（reconcile/act）；此处仅发送前兜底，只删不补——保证不 400，但打
    ERROR 提示上游漏补。多 tool_call 的 assistant 只剥悬挂的那几个，保留有 result 的；全悬挂
    则剥成空 tool_calls（随后由 :func:`remove_empty_messages` 清理）。绝不动有 result 的调用，
    故不会反向制造孤立 tool result。
    """
    resolved = {m.tool_call_id for m in messages if m.role == "tool" and m.tool_call_id}
    out: list[LLMMessage] = []
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            kept = [tc for tc in m.tool_calls if tc.get("id") in resolved]
            if len(kept) != len(m.tool_calls):
                dangling = [tc.get("id") for tc in m.tool_calls if tc.get("id") not in resolved]
                logger.error(
                    "stream_llm: dropping %d dangling tool_call(s) with no tool_result "
                    "(上游对账漏补？): %s",
                    len(dangling), dangling,
                )
                m = LLMMessage(
                    role=m.role, content=m.content, tool_calls=kept,
                    tool_call_id=m.tool_call_id, reasoning_content=m.reasoning_content,
                )
        out.append(m)
    return out


def _is_empty_content(content: "str | list[ContentPart]") -> bool:
    """内容是否为「空」：空串/纯空白，或空列表/仅含空白 TextPart。非文本块（图片等）视为有内容。"""
    if isinstance(content, str):
        return not content.strip()
    if not content:
        return True
    for p in content:
        if isinstance(p, TextPart):
            if p.text and p.text.strip():
                return False
        else:
            return False
    return True


def remove_empty_messages(messages: list[LLMMessage]) -> list[LLMMessage]:
    """删除内容为空且无 tool_calls 的 user/assistant 消息（防御性，命中打 ERROR 日志）。

    Anthropic 对空 content 块直接 400。只管 user/assistant：纯 tool_use 的 assistant（有
    tool_calls、无文本）合法须保留；tool 消息即便空也不删（删了会制造悬挂 tool_call）。
    """
    out: list[LLMMessage] = []
    for m in messages:
        if m.role in ("user", "assistant") and not m.tool_calls and _is_empty_content(m.content):
            logger.error(
                "stream_llm: dropping empty-content %s message (no text, no tool_calls)", m.role
            )
            continue
        out.append(m)
    return out


def drop_orphan_tool_results(messages: list[LLMMessage]) -> list[LLMMessage]:
    """丢弃 tool_call_id 无前序 assistant tool_call 配对的孤立 tool result 消息。"""
    seen: set[str] = set()
    out: list[LLMMessage] = []
    for m in messages:
        if m.role == "assistant":
            seen.update(tc.get("id", "") for tc in m.tool_calls if tc.get("id"))
        elif m.role == "tool":
            if not m.tool_call_id or m.tool_call_id not in seen:
                continue  # 孤儿 → 丢弃
        out.append(m)
    return out


def _merge_message_content(
    prev: "str | list[ContentPart]", cur: "str | list[ContentPart]"
) -> "str | list[ContentPart]":
    """连接两条消息的内容。两侧皆为 str → 以空行拼接（保留原文本语义）；任一侧为多模态
    时 → 拼成 ContentPart 列表（str 侧转 TextPart），避免把图片拍扁丢弃。"""
    if isinstance(prev, str) and isinstance(cur, str):
        return f"{prev}\n\n{cur}"

    def as_parts(c: "str | list[ContentPart]") -> "list[ContentPart]":
        return [TextPart(text=c)] if isinstance(c, str) else list(c)

    return as_parts(prev) + as_parts(cur)


def ensure_leading_user(messages: list[LLMMessage]) -> list[LLMMessage]:
    """丢弃开头 role != "user" 的消息直到首条为 user（Anthropic 首条必须 user，否则 400）。

    只动头部、不注入文案——与「以 user 收尾」语义兜底（保留在 composer）不同，对正常工具
    循环（中段 tool result 之后无 user）无影响。前导 assistant 被丢后其配对 tool 会成孤儿，
    由随后的 drop_orphan_tool_results 清理（见 stream_llm 串联顺序）。
    """
    i = 0
    while i < len(messages) and messages[i].role != "user":
        i += 1
    return messages[i:] if i else messages


def reorder_tool_results_after_calls(messages: list[LLMMessage]) -> list[LLMMessage]:
    """把每条 tool result 紧挪到其 assistant tool_call 之后，消除中间夹着的非 tool 消息。

    OpenAI 兼容端点要求 ``role="tool"`` 紧跟在 ``role="assistant"(tool_calls)`` 之后、按
    ``tool_call_id`` 配对，中间不得夹其它角色（夹了即 400「insufficient tool messages
    following tool_calls message」）。跨层历史按 timestamp 归并会把 user 回合插进
    assistant↔tool_result 之间，甚至让 result 落到 call 之前；集合级配对
    （:func:`drop_dangling_tool_calls` / :func:`drop_orphan_tool_results`）只校验「id 是否
    存在」查不出这种**错位**。本步按 owner 把每个 tool result 移到其 assistant tool_call 之后，
    并按该 assistant 的 ``tool_calls`` 顺序排列。无 owner（没有任何 assistant 调用该 id）的
    孤儿 tool 原地保留，交 :func:`drop_orphan_tool_results` 处理；只搬不删、不补。
    """
    owned_ids = {
        tc.get("id")
        for m in messages
        if m.role == "assistant"
        for tc in m.tool_calls
        if tc.get("id")
    }
    results_by_id: dict[str, list[LLMMessage]] = {}
    for m in messages:
        if m.role == "tool" and m.tool_call_id in owned_ids:
            results_by_id.setdefault(m.tool_call_id, []).append(m)

    out: list[LLMMessage] = []
    for m in messages:
        if m.role == "tool" and m.tool_call_id in owned_ids:
            continue  # 由其 owner assistant 之后统一发出（见下）
        out.append(m)
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                out.extend(results_by_id.get(tc.get("id"), ()))
    return out


def merge_consecutive_messages(messages: list[LLMMessage]) -> list[LLMMessage]:
    """合并连续相同角色的消息（tool 消息因绑定 tool_call_id 不合并）。"""
    merged: list[LLMMessage] = []
    for msg in messages:
        if (
            merged
            and merged[-1].role == msg.role
            and msg.role != "tool"
        ):
            prev = merged[-1]
            merged[-1] = LLMMessage(
                role=prev.role,
                content=_merge_message_content(prev.content, msg.content),
                tool_calls=prev.tool_calls + msg.tool_calls,
            )
        else:
            merged.append(msg)
    return merged


def legalize_messages(messages: list[LLMMessage]) -> list[LLMMessage]:
    """发送前把消息序列规整为 provider 合法形态（六条不变式，见模块 docstring）。

    纯函数、不依赖 llm/网络，便于单测全链路。``stream_llm`` 在发送前调用一次。
    """
    return merge_consecutive_messages(
        drop_orphan_tool_results(
            ensure_leading_user(
                remove_empty_messages(
                    reorder_tool_results_after_calls(
                        drop_dangling_tool_calls(messages)
                    )
                )
            )
        )
    )


def _estimate_request_tokens(request: "LLMRequest") -> int:
    """估算本次待发 prompt 的 token（system + 全部 messages + tools schema）。

    遍历**全部** messages，含本轮新加的 role="tool" result——这是 loop_guard.context_tokens
    （上一轮真实值）漏掉的增量，"取大"逻辑正靠它补齐。len//4 口径不变（低估已知）。
    """
    total = estimate_tokens(request.system or "")
    for m in request.messages:
        total += estimate_tokens(content_to_text(m.content))
    for t in request.tools:
        total += estimate_tokens(t.name) + estimate_tokens(t.description or "")
        total += estimate_tokens(json.dumps(t.input_schema, ensure_ascii=False))
    return total


def apply_dynamic_max_tokens(ctx, request: "LLMRequest", loop_guard) -> None:
    """发送前按窗口就地写入 request.max_tokens。

    仅当未显式设值（None）且有 loop_guard 时生效。天花板取 llm.output_ceiling（duck-type，
    缺省/None → 回退 llm.context_limit）。margin/floor 从 ctx.config 取，缺省回退安全值。
    """
    if request.max_tokens is not None or loop_guard is None:
        return
    llm = ctx.llm
    margin = int(_cfg_val(ctx, "dynamic_max_tokens_margin", 4096))
    floor = int(_cfg_val(ctx, "dynamic_max_tokens_floor", 1024))
    ceiling = getattr(llm, "output_ceiling", None) or llm.context_limit
    request.max_tokens = dynamic_max_tokens(
        loop_guard.context_limit,
        loop_guard.context_tokens,
        _estimate_request_tokens(request),
        ceiling,
        margin=margin,
        floor=floor,
    )


async def stream_llm(
    llm: "LLMClient", request: "LLMRequest", *, stream: bool = True
) -> AsyncIterator["LLMChunk"]:
    """发送前合法化 ``request.messages``，再流式转发 ``llm.complete`` 的 chunk。"""
    request.messages = legalize_messages(request.messages)
    async for chunk in llm.complete(request, stream=stream):
        yield chunk


# ── 自愈退避包装 ───────────────────────────────────────────────────────────────

# 默认值（ctx.config 缺省时回退；与 RuntimeConfig 默认一致）
_DEFAULT_MAX_ATTEMPTS = 8
_DEFAULT_MAX_DURATION_SEC = 300.0
_DEFAULT_BASE_DELAY_SEC = 2.0
_DEFAULT_MAX_INTERVAL_SEC = 60.0


def _cfg_val(ctx, name: str, default: float) -> float:
    cfg = getattr(ctx, "config", None)
    return getattr(cfg, name, default) if cfg is not None else default


def _is_outage(e: BaseException) -> bool:
    return bool(getattr(e, "outage", False))


def _compute_delay(
    attempt: int,
    *,
    base: float,
    max_interval: float,
    retry_after: "float | None",
) -> float:
    """指数退避延迟（attempt 1-based）；优先采用 Retry-After；上限 max_interval。"""
    if retry_after is not None and retry_after > 0:
        return min(retry_after, max_interval)
    return min(base * (2 ** (attempt - 1)), max_interval)


async def _sleep_cancellable(delay: float, tok) -> None:
    """Sleep up to *delay* seconds; if *tok* cancels first, raise CancelledError."""
    if tok is None:
        await asyncio.sleep(delay)
        return
    waiter = asyncio.ensure_future(tok.wait())
    try:
        await asyncio.wait({waiter}, timeout=delay)
    finally:
        if not waiter.done():
            waiter.cancel()
    tok.raise_if_cancelled()


async def _emit_retry(
    ctx,
    state,
    attempt: int,
    max_attempts: int,
    delay: float,
    error: BaseException,
) -> None:
    bus = getattr(ctx, "event_bus", None)
    if bus is None or state is None:
        return
    await bus.emit(make_event(state, EventType.LLM_RETRY_TRIGGERED, payload={
        "attempt": attempt,
        "max_attempts": max_attempts,
        "next_delay_sec": delay,
        "error_code": getattr(error, "status_code", 0),
        "error": str(error),
    }))


async def stream_llm_resilient(ctx, state, request) -> AsyncIterator["LLMChunk"]:
    """退避自愈包装：把 retriable+outage 的 LLM 调用在进程内指数退避重试。

    - 仅当本次尝试**尚未 yield 任何 chunk** 时重试（避免重复 token / 污染累积）。
    - 已 yield 后断流（中途 outage）→ 直接 LLMOutageError（靠 /resume 重驱动）。
    - 非 outage 的 retriable（如 _finalize 截断）与永久错 → 原样抛出（保留既有语义）。
    - 预算（max_attempts / max_duration）耗尽 → LLMOutageError。
    - 退避期间尊重 cancel_token。
    """
    from ctx_weft.protocols import LLMCallError  # local to avoid re-export confusion

    loop_guard = getattr(getattr(state, "agent", None), "loop_guard", None)
    apply_dynamic_max_tokens(ctx, request, loop_guard)

    max_attempts = int(_cfg_val(ctx, "llm_self_heal_max_attempts", _DEFAULT_MAX_ATTEMPTS))
    max_duration = _cfg_val(ctx, "llm_self_heal_max_duration_sec", _DEFAULT_MAX_DURATION_SEC)
    base = _cfg_val(ctx, "llm_self_heal_base_delay_sec", _DEFAULT_BASE_DELAY_SEC)
    max_interval = _cfg_val(ctx, "llm_self_heal_max_interval_sec", _DEFAULT_MAX_INTERVAL_SEC)

    deadline = monotonic() + max_duration
    tok = getattr(ctx, "cancel_token", None)
    attempt = 0
    while True:
        yielded_anything = False
        try:
            async for chunk in stream_llm(ctx.llm, request):
                yielded_anything = True
                yield chunk
            return  # success
        except LLMCallError as e:
            if not getattr(e, "retriable", True) or not _is_outage(e):
                raise  # 永久错 / 非 outage retriable → 原样抛出
            if yielded_anything:
                raise LLMOutageError(f"LLM outage mid-stream: {e}") from e
            if tok is not None:
                tok.raise_if_cancelled()
            attempt += 1
            if attempt >= max_attempts or monotonic() >= deadline:
                raise LLMOutageError(
                    f"LLM self-heal exhausted after {attempt} attempt(s): {e}",
                    status_code=getattr(e, "status_code", 0),
                ) from e
            delay = _compute_delay(
                attempt,
                base=base,
                max_interval=max_interval,
                retry_after=getattr(e, "retry_after_sec", None),
            )
            delay += random.uniform(0, delay * 0.1)  # jitter
            await _emit_retry(ctx, state, attempt, max_attempts, delay, e)
            await _sleep_cancellable(delay, tok)
