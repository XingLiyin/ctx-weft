"""流终结算：两个 adapter 共用的收尾逻辑。

adapter 各自把 native 缓冲解析成 ``list[ToolCall]`` 后调本函数，统一处理：
  - 截断判定（有半截 tool call 但没等到终止事件）→ 抛 retriable 错（D1）。
  - 空/退化响应（无终止 + 无缓冲 + 无正文）→ 抛 retriable 错（N4）。
  - native tool call 优先；否则从正文还原 ``<tool_call>`` 文本 tool call（1b）。
  - 产出顺序：usage → tool_call(s) → done。
"""

from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any

from ctx_weft.protocols import LLMCallError, LLMChunk, LLMUsage, RAW_ARGS_KEY, ToolCall
from ctx_weft.core.utils.ids import generate_id
from ctx_weft.providers.llm.text_calls import (
    clean_visible,
    extract_think,
    scan_text_tool_calls,
    unwrap_raw_arguments,
)

logger = logging.getLogger(__name__)


def _rescue_json_object(raw: str) -> dict | None:
    """从一段畸形串里救出一个完整 JSON 对象；救不出返回 None。

    两个 adapter 累积 native 工具参数时按 index 分桶盲拼（``+=``）。若同一桶被两股参数流
    首尾相接（上游漏发 index / 重发 / 抖动），拼出的串整体非法，但里面往往嵌着一个可用的
    完整对象。策略：
      - 先试「干净前缀对象」：从头 ``raw_decode``，成功即取（丢尾部垃圾，覆盖「完整对象+尾巴」）。
      - 再试「干净后缀对象」：从最后一个 ``{`` 起逐个回退，取「恰好解析到串尾」的完整对象
        （覆盖「前段截断 + 后段完整」，即观测到的畸形）。
    """
    s = raw.strip()
    if not s:
        return None
    decoder = json.JSONDecoder()
    try:
        val, _ = decoder.raw_decode(s)
        if isinstance(val, dict):
            return val
    except json.JSONDecodeError:
        pass
    starts = [i for i, ch in enumerate(s) if ch == "{"]
    for i in reversed(starts):
        try:
            val, end = decoder.raw_decode(s[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(val, dict) and i + end == len(s):
            return val
    return None


def parse_tool_arguments(raw: str) -> dict:
    """把累积的 native 工具参数字符串解析成 dict（含畸形救援）。

    正常路径直接 ``json.loads``。整体非法时尝试从串里救出一个完整对象（见
    ``_rescue_json_object``，覆盖同一 index 桶被两股参数流拼接的情况）。仍救不出、或解析出
    的是非对象（数组/标量）→ 兜底 ``{"_raw": raw}`` 交 gateway 报错（不静默丢）。
    """
    if not raw:
        return {}
    try:
        val: Any = json.loads(raw)
    except json.JSONDecodeError:
        val = _rescue_json_object(raw)
        if val is not None:
            logger.warning(
                "Rescued a valid JSON object from a malformed tool-argument buffer "
                "(len=%d); upstream likely concatenated two argument streams.", len(raw),
            )
    if isinstance(val, dict):
        return val
    return {RAW_ARGS_KEY: raw}


def _unwrap_tc(tc: ToolCall) -> ToolCall:
    """解包 tool call 的 ``{"_raw": ...}`` 兜底哨兵（见 unwrap_raw_arguments）。

    参数没变时原样返回（保 ``is`` 身份，不做无谓 replace）。这是所有 native/文本 tool call
    汇入的收尾口，在此解包能保证下游（gateway 执行、memory 落库、回灌历史）都拿到干净参数。
    """
    new_args = unwrap_raw_arguments(tc.arguments)
    return tc if new_args is tc.arguments else dataclasses.replace(tc, arguments=new_args)


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
        out.extend(LLMChunk(kind="tool_call", tool_call=_unwrap_tc(tc)) for tc in native_tool_calls)
    else:
        # 正文里内联的文本 tool call（wrapped <tool_call>/<tool_code>/<function=>，或
        # <minimax:tool_call>）→ 收尾还原成规整 tool call。首个命中的方言负责解析。
        scan = scan_text_tool_calls(content_text)
        if scan is not None:
            dialect_name, parsed = scan
            # D2：标签出现但一个都没解析出来（截断的未闭合标签 / 畸形 JSON/XML / 缺 name）。
            # 若放行，上层会把这轮当「纯文本让位用户」误暂停、工具动作被静默吞掉、UI 卡 llm_pending。
            # 标 outage=True 走进程内退避重抽（格式抖动/流截断通常一两次即恢复），预算耗尽再转
            # LLMOutageError → session 可恢复 INTERRUPTED（靠 /resume 重驱动），而非整任务硬 FAILED。
            if not parsed:
                raise LLMCallError(
                    f"LLM emitted a {dialect_name} tool call tag that parsed to zero "
                    "tool calls (truncated or malformed text tool call)",
                    retriable=True,
                    outage=True,
                )
            out.extend(
                LLMChunk(
                    kind="tool_call",
                    tool_call=ToolCall(
                        id=generate_id("call"), name=p.name,
                        arguments=unwrap_raw_arguments(p.arguments),
                    ),
                )
                for p in parsed
            )

    out.append(LLMChunk(kind="done", finish_reason=finish_reason or "stop"))
    return out
