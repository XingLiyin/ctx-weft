"""LLM 请求发送的统一关口：集中所有「发送前合法化」方法 + 流式转发。

四个 step（act / observe / compact / recognize_intent）各自构造 ``LLMRequest`` 后
都经 :func:`stream_llm` 发送，使「发送前合法化」成为**唯一**关口：装配层（composer）
只负责构造逻辑消息结构，不做合法化；连续同角色 / 孤立 tool result 一律在这里、发送前
统一处理。

合法化两条不变式（:func:`stream_llm` 内顺序：先丢孤立 tool result，再合并连续同角色）：
  1. :func:`drop_orphan_tool_results` —— 丢弃 tool_call_id 无前序 assistant tool_call
     配对的 ``role="tool"`` 消息。compact 丢弃 assistant 但保留 TOOL_RESULT、recall 窗口
     在 assistant↔result 之间截断、跨层按 timestamp 归并错位等都会产生此类孤儿，会被
     Anthropic / OpenAI 直接 400。
  2. :func:`merge_consecutive_messages` —— 合并连续同角色消息（多模态安全）。

本模块仅依赖 protocols，且只被本 loop 层的 step import；故置于 ``core/loop`` 下。若将来
装配等下层也需复用这些合法化函数，应改为下沉到 ``core`` 根，避免下层反向依赖 loop。

注：本关口仅处理「孤立 tool result」；反向的「悬挂 assistant tool_call（无后继 result）」
不在此处理。adapter 不应重复实现以上不变式——已在 core 层集中保证。

另注：「prompt 必须以 user 收尾」不下沉到此处——那是 act 装配期的语义兜底（注入具体
文案、且 provider 并不强制），若在此每个 turn 强制，会在工具循环里于 tool result 之后误
插一条 user，篡改正常的工具调用流。该兜底保留在 composer._build_actor_messages。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from time import monotonic
from typing import TYPE_CHECKING

from ctx_weft.protocols import LLMMessage, LLMOutageError, TextPart
from ctx_weft.core.events.types import EventType
from ctx_weft.core.loop.driver import make_event

if TYPE_CHECKING:
    from ctx_weft.protocols import ContentPart, LLMChunk, LLMClient, LLMRequest


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


async def stream_llm(
    llm: "LLMClient", request: "LLMRequest", *, stream: bool = True
) -> AsyncIterator["LLMChunk"]:
    """发送前合法化 ``request.messages``，再流式转发 ``llm.complete`` 的 chunk。"""
    request.messages = merge_consecutive_messages(drop_orphan_tool_results(request.messages))
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
