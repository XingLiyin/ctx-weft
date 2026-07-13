"""解析嵌在模型正文里的 tool call / ``<think>`` 标签。

部分模型（尤其国产 OpenAI 兼容）不走 native function-calling，而是把工具调用以
``<tool_call>...</tool_call>`` 或 XML ``<function=><parameter=>`` 写进正文文本；推理则
内联在 ``<think>...</think>``。本模块是纯函数解析器，供 adapter 在「流终结算」时把这些
还原成规整的 tool call / reasoning。移植自 QwenPaw ``local_models/tag_parser.py``。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from ctx_weft.protocols.llm import RAW_ARGS_KEY

logger = logging.getLogger(__name__)


def unwrap_raw_arguments(args: dict) -> dict:
    """把 ``{"_raw": "<json>"}`` 兜底哨兵解包回真实参数（内层是合法 JSON 对象时）。

    ``_raw`` 是 adapter 对「native 工具参数没解析成 JSON」的兜底键（见 ``_parse_buffers`` /
    ``_parse_tool_blocks``）。两种来源都应还原：

      1. 兜底原文其实合法（罕见的流式拼接抖动）——救回来直接能用；
      2. **模型把 ``_raw`` 误当参数名照抄** ``{"_raw": "<合法json>"}``——一旦回灌历史里出现过
         ``_raw``（前一次真畸形被兜底），模型就会模仿它，反复把参数包进 ``_raw``、gateway 反复
         报「必填项缺失」，自我强化成死循环。解包即斩断这个环。

    内层无法解析成 dict（真畸形 / 非对象）→ **原样返回同一对象**（``is`` 不变），交 gateway 报错。
    最多解 3 层，防病态嵌套 ``_raw``。
    """
    for _ in range(3):
        if not (
            isinstance(args, dict)
            and list(args) == [RAW_ARGS_KEY]
            and isinstance(args[RAW_ARGS_KEY], str)
        ):
            return args
        try:
            inner = json.loads(args[RAW_ARGS_KEY])
        except (json.JSONDecodeError, TypeError):
            return args
        if not isinstance(inner, dict):
            return args
        args = inner
    return args

THINK_START = "<think>"
THINK_END = "</think>"
TOOL_CALL_START = "<tool_call>"
# MiniMax 把工具调用写成 Anthropic 风格 XML，外套 <minimax:tool_call>：
#   <minimax:tool_call>
#     <invoke name="tool_name">
#       <parameter name="arg">value</parameter>
#     </invoke>
#   </minimax:tool_call>
MINIMAX_TOOL_CALL_START = "<minimax:tool_call>"

_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)
_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)

# 严格 XML：<function=name>...</function>
_XML_FUNC_RE = re.compile(r"<function=([^>]+)>(.*?)</function>", re.DOTALL)
_XML_PARAM_RE = re.compile(r"<parameter=([^>]+)>(.*?)</parameter>", re.DOTALL)
# 宽松 XML（无闭合标签）：值从标签后延伸到下一个标签 / 末尾
_XML_FUNC_LENIENT_RE = re.compile(
    r"<function=([^>]+)>(.*?)(?=<function=|</function>|\Z)", re.DOTALL
)
_XML_PARAM_LENIENT_RE = re.compile(
    r"<parameter=([^>]+)>(.*?)"
    r"(?=<parameter=|</parameter>|<function=|</function>|\Z)",
    re.DOTALL,
)

# MiniMax 格式：<minimax:tool_call> 块内含一个/多个 <invoke name="...">，每个含
# 若干 <parameter name="...">value</parameter>。invoke/parameter 均用宽松匹配，
# 容忍缺失闭合标签（流式截断残尾）；值多为多行文本。
_MINIMAX_BLOCK_RE = re.compile(r"<minimax:tool_call>\s*(.*?)\s*</minimax:tool_call>", re.DOTALL)
_MINIMAX_INVOKE_RE = re.compile(
    r'<invoke\s+name="([^"]+)"\s*>(.*?)(?=</invoke>|<invoke\s|</minimax:tool_call>|\Z)',
    re.DOTALL,
)
_MINIMAX_PARAM_RE = re.compile(
    r'<parameter\s+name="([^"]+)"\s*>(.*?)(?=</parameter>|<parameter\s|</invoke>|\Z)',
    re.DOTALL,
)

# <tool_code> 与 <tool_call> 完全对等（同一「wrapped」方言的两套 JSON 键别名）。
# 反向引用保证开闭标签配对，不会 <tool_call> 开、</tool_code> 闭混匹配。
_WRAPPED_BLOCK_RE = re.compile(r"<(tool_call|tool_code)>\s*(.*?)\s*</\1>", re.DOTALL)


@dataclass
class ParsedToolCall:
    """从文本里解析出的一次工具调用。"""

    name: str
    arguments: dict
    raw_arguments: str


@dataclass
class TextScan:
    """``parse_tool_calls_from_text`` 的结果。"""

    text_before: str = ""
    tool_calls: list[ParsedToolCall] = field(default_factory=list)
    has_open_tag: bool = False


def contains_tool_call_tag(text: str) -> bool:
    """快速子串判断：正文里是否含 tool call 标签。"""
    return TOOL_CALL_START in text or "<function=" in text


def _extract_params_lenient(body: str) -> dict:
    arguments: dict = {}
    for m in _XML_PARAM_LENIENT_RE.finditer(body):
        name = m.group(1).strip()
        if name:
            arguments[name] = m.group(2).strip()
    return arguments


def _parse_xml_tool_call(raw: str) -> ParsedToolCall | None:
    """解析 XML 风格的 tool call（严格优先，退化到宽松）。"""
    func = _XML_FUNC_RE.search(raw)
    if func:
        name = func.group(1).strip()
        if not name:
            return None
        body = func.group(2)
        arguments = {
            m.group(1).strip(): m.group(2).strip()
            for m in _XML_PARAM_RE.finditer(body)
        }
        lenient = _extract_params_lenient(body)
        if len(lenient) > len(arguments):
            arguments = lenient
        if not arguments and "<parameter=" in body:
            return None
        return ParsedToolCall(name, arguments, json.dumps(arguments, ensure_ascii=False))

    func = _XML_FUNC_LENIENT_RE.search(raw)
    if not func:
        return None
    name = func.group(1).strip()
    if not name:
        return None
    arguments = _extract_params_lenient(func.group(2))
    if not arguments:
        return None
    return ParsedToolCall(name, arguments, json.dumps(arguments, ensure_ascii=False))


def _parse_single_tool_call(raw: str) -> ParsedToolCall | None:
    """解析一个 ``<tool_call>`` 块的内容：JSON → 严格 XML → 宽松 XML。"""
    stripped = raw.strip()
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        data = None

    if isinstance(data, dict):
        # <tool_call> uses name/arguments; <tool_code> uses tool/args. Accept both.
        name = data.get("name") or data.get("tool") or ""
        if not name:
            logger.warning("Text tool call missing 'name'/'tool': %.200s", stripped)
            return None
        if "arguments" in data:
            arguments = data["arguments"]
        elif "args" in data:
            arguments = data["args"]
        else:
            arguments = {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (json.JSONDecodeError, TypeError):
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        return ParsedToolCall(name, arguments, json.dumps(arguments, ensure_ascii=False))

    result = _parse_xml_tool_call(stripped)
    if result is None:
        logger.warning("Failed to parse text tool call: %.200s", stripped)
    return result


def _parse_wrapped(text: str) -> list[ParsedToolCall]:
    """抽取所有 <tool_call>/<tool_code> 块，每块按 JSON → 严格 XML → 宽松 XML 解析。"""
    calls: list[ParsedToolCall] = []
    for m in _WRAPPED_BLOCK_RE.finditer(text):
        parsed = _parse_single_tool_call(m.group(2))
        if parsed is not None:
            calls.append(parsed)
    return calls


def _parse_minimax(text: str) -> list[ParsedToolCall]:
    """抽取 <minimax:tool_call> 块里的所有 <invoke>（见 parse_minimax_tool_calls）。"""
    return parse_minimax_tool_calls(text)


@dataclass(frozen=True)
class TextToolCallDialect:
    """一种「工具调用写进正文文本」的方言：起始标签集 + 解析器。

    ``open_markers`` 一处三用：detect（是否命中本方言）、可见门 ``_VISIBLE_MARKERS``
    （从正文里扣掉标签）、以及块正则的锚点。加新方言只需往 ``DIALECTS`` 加一项。
    """

    name: str
    open_markers: tuple[str, ...]
    parse: Callable[[str], list[ParsedToolCall]]

    def detect(self, text: str) -> bool:
        return any(m in text for m in self.open_markers)


WRAPPED_DIALECT = TextToolCallDialect(
    "wrapped", ("<tool_call>", "<tool_code>", "<function="), _parse_wrapped
)
MINIMAX_DIALECT = TextToolCallDialect(
    "minimax", ("<minimax:tool_call>",), _parse_minimax
)
# 顺序即 _finalize 的检测优先级：wrapped 先于 minimax。
DIALECTS: tuple[TextToolCallDialect, ...] = (WRAPPED_DIALECT, MINIMAX_DIALECT)


def scan_text_tool_calls(text: str) -> tuple[str, list[ParsedToolCall]] | None:
    """首个 detect 命中的方言 → ``(name, calls)``；都不命中 → ``None``。

    ``(name, [])`` 表示「标签在但零解析」（截断/畸形）——供 finalize 判 outage 退避自愈。
    """
    for dialect in DIALECTS:
        if dialect.detect(text):
            return dialect.name, dialect.parse(text)
    return None


def parse_tool_calls_from_text(text: str) -> TextScan:
    """抽取所有 ``<tool_call>...</tool_call>`` 块。

    返回标签前正文、解析出的 tool calls，以及是否存在未闭合标签（流式残留）。
    """
    matches = list(_TOOL_CALL_RE.finditer(text))
    if not matches:
        open_idx = text.rfind(TOOL_CALL_START)
        if open_idx != -1:
            return TextScan(text_before=text[:open_idx].rstrip(), has_open_tag=True)
        return TextScan(text_before=text)

    text_before = text[: matches[0].start()].rstrip()
    remaining = text[matches[-1].end():]
    has_open_tag = TOOL_CALL_START in remaining

    tool_calls: list[ParsedToolCall] = []
    for m in matches:
        parsed = _parse_single_tool_call(m.group(1))
        if parsed is not None:
            tool_calls.append(parsed)

    return TextScan(text_before=text_before, tool_calls=tool_calls, has_open_tag=has_open_tag)


def contains_minimax_tool_call(text: str) -> bool:
    """快速子串判断：正文里是否含 MiniMax 风格 <minimax:tool_call> 标签。"""
    return MINIMAX_TOOL_CALL_START in text


def parse_minimax_tool_calls(text: str) -> list[ParsedToolCall]:
    """抽取 <minimax:tool_call> 块里的所有 <invoke>（每个含若干 <parameter>）。

    支持一个块内多个 invoke、多个块；块未闭合（流式截断）时取起始标签之后的内容兜底。
    """
    blocks = _MINIMAX_BLOCK_RE.findall(text)
    if not blocks:
        start = text.find(MINIMAX_TOOL_CALL_START)
        if start == -1:
            return []
        blocks = [text[start + len(MINIMAX_TOOL_CALL_START):]]

    calls: list[ParsedToolCall] = []
    for body in blocks:
        for inv in _MINIMAX_INVOKE_RE.finditer(body):
            name = inv.group(1).strip()
            if not name:
                continue
            arguments = {
                p.group(1).strip(): p.group(2).strip()
                for p in _MINIMAX_PARAM_RE.finditer(inv.group(2))
            }
            calls.append(ParsedToolCall(name, arguments, json.dumps(arguments, ensure_ascii=False)))
    if not calls:
        logger.warning("Found <minimax:tool_call> but parsed no invoke: %.200s", text)
    return calls


_VISIBLE_MARKERS = (THINK_START,) + tuple(
    m for dialect in DIALECTS for m in dialect.open_markers
)


def merge_content(acc: str, chunk: str) -> str:
    """合并一条 content 增量（N5：兼容 delta 与 cumulative 两种流式语义）。

    多数 provider 流式给增量（拼接）；少数重发「至今全文」（cumulative）。若 chunk 以
    已累积全文为前缀且更长 → 判定 cumulative，直接替换，避免正文重复。
    """
    if acc and chunk.startswith(acc):
        return chunk
    return acc + chunk


def clean_visible(raw: str) -> str:
    """返回当前可安全作为「可见正文」吐出的部分。

    - 闭合的 ``<think>...</think>`` 整块移除（思考不进正文）。
    - 从首个未闭合 ``<think>`` / ``<tool_call>`` / ``<function=`` 处截断（其后内容延后处理）。
    - 末尾若是某标签的部分前缀（如 ``<to``）则一并扣下，待后续增量补全。
    随累积文本增长，本函数返回值单调地以前次结果为前缀延展。
    """
    s = _THINK_RE.sub("", raw)
    cut = len(s)
    for marker in _VISIBLE_MARKERS:
        i = s.find(marker)
        if i != -1:
            cut = min(cut, i)
    s = s[:cut]
    lt = s.rfind("<")
    if lt != -1:
        tail = s[lt:]
        if any(m.startswith(tail) for m in _VISIBLE_MARKERS):
            s = s[:lt]
    return s


class ContentGate:
    """增量门：喂入累积 content，吐出本次新增的可见正文（已剥离标签）。"""

    def __init__(self) -> None:
        self.emitted_len = 0

    def feed(self, content_text: str) -> str:
        visible = clean_visible(content_text)
        if len(visible) <= self.emitted_len:
            return ""
        out = visible[self.emitted_len:]
        self.emitted_len = len(visible)
        return out


def extract_think(text: str) -> tuple[str, str]:
    """抽取 ``<think>...</think>``，返回 ``(reasoning, remaining_text)``。

    处理未闭合 ``<think>``（流式残留）：标签后内容全算 reasoning。
    """
    match = _THINK_RE.search(text)
    if match:
        reasoning = match.group(1).strip()
        remaining = (text[: match.start()] + text[match.end():]).strip()
        return reasoning, remaining

    open_idx = text.find(THINK_START)
    if open_idx != -1:
        remaining = text[:open_idx].strip()
        reasoning = text[open_idx + len(THINK_START):].strip()
        return reasoning, remaining

    return "", text
