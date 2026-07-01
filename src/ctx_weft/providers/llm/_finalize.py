"""流终结算：两个 adapter 共用的收尾逻辑。

adapter 各自把 native 缓冲解析成 ``list[ToolCall]`` 后调本函数，统一处理：
  - 截断判定（有半截 tool call 但没等到终止事件）→ 抛 retriable 错（D1）。
  - 空/退化响应（无终止 + 无缓冲 + 无正文）→ 抛 retriable 错（N4）。
  - native tool call 优先；否则从正文还原 ``<tool_call>`` 文本 tool call（1b）。
  - 产出顺序：usage → tool_call(s) → done。
"""

from __future__ import annotations

import logging

from ctx_weft.protocols import LLMCallError, LLMChunk, LLMUsage, ToolCall
from ctx_weft.core.utils import generate_id
from ctx_weft.providers.llm.text_calls import (
    clean_visible,
    contains_tool_call_tag,
    extract_think,
    parse_tool_calls_from_text,
)

logger = logging.getLogger(__name__)


def build_finalize_chunks(
    *,
    content_text: str,
    native_tool_calls: list[ToolCall],
    had_native_buffer: bool,
    saw_terminal: bool,
    usage: LLMUsage | None,
    finish_reason: str | None,
    emitted_visible_len: int = 0,
) -> list[LLMChunk]:
    """计算收尾应 yield 的 chunk 序列；截断/空响应时抛 ``LLMCallError(retriable=True)``。

    ``emitted_visible_len``：流式阶段已吐出的可见正文长度（ContentGate.emitted_len）。
    收尾在此补吐尚未流出的「可见正文尾部」（如标签误判后被扣下的文本、闭合 think 之后的答案）。
    """
    # D1：缓冲里有半截 tool call，但流没等到终止事件就结束 → 截断，当错误重试。
    if not saw_terminal and had_native_buffer:
        logger.warning(
            "LLM stream ended before tool call completed (truncated); retriable rerun. "
            "parsed_tool_calls=%d finish_reason=%s",
            len(native_tool_calls), finish_reason,
        )
        raise LLMCallError(
            "LLM stream ended before tool call completed (truncated response)",
            retriable=True,
        )
    # N4：没终止事件、没缓冲、正文也空 → 流异常空（连接早断/网关吞流）→ 重试，
    # 别让上层把「空」当成「纯文本让位用户」而误暂停。
    if not saw_terminal and not had_native_buffer and not content_text.strip():
        raise LLMCallError(
            "LLM stream ended empty without a terminal event",
            retriable=True,
        )

    out: list[LLMChunk] = []

    # 内联 <think>：抽成 reasoning（流式阶段被门扣下，未吐过）。
    reasoning, _ = extract_think(content_text)
    if reasoning:
        out.append(LLMChunk(kind="reasoning", text=reasoning))

    # 补吐尚未流出的可见正文尾部（增量门 D3 扣下、收尾澄清的部分）。
    visible = clean_visible(content_text)
    tail = visible[emitted_visible_len:] if emitted_visible_len < len(visible) else ""
    if tail:
        out.append(LLMChunk(kind="token", text=tail))

    if usage is not None:
        out.append(LLMChunk(kind="usage", usage=usage, finish_reason=finish_reason or "stop"))

    if native_tool_calls:
        out.extend(LLMChunk(kind="tool_call", tool_call=tc) for tc in native_tool_calls)
    elif contains_tool_call_tag(content_text):
        # D2：正文里出现了 tool call 标签,但一个都没解析出来（截断的未闭合标签 /
        # 畸形 JSON/XML / 缺 name）。若放行,上层会把这轮当「纯文本让位用户」误暂停,
        # 工具动作被静默吞掉、UI 卡在 llm_pending。标 outage=True 走退避自愈：进程内
        # 同 prompt 退避重抽（格式抖动/流截断通常一两次即恢复）,预算耗尽再转 LLMOutageError
        # → session 可恢复 INTERRUPTED（靠 /resume 重驱动）,而非整任务重跑 3 次后硬 FAILED。
        parsed = parse_tool_calls_from_text(content_text).tool_calls
        if not parsed:
            raise LLMCallError(
                "LLM emitted a tool call tag that parsed to zero tool calls "
                "(truncated or malformed text tool call)",
                retriable=True,
                outage=True,
            )
        out.extend(
            LLMChunk(
                kind="tool_call",
                tool_call=ToolCall(id=generate_id("call"), name=p.name, arguments=p.arguments),
            )
            for p in parsed
        )

    out.append(LLMChunk(kind="done", finish_reason=finish_reason or "stop"))
    return out
