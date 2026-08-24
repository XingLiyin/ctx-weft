"""内容形态归一层（多模态 Phase 1）。

全仓唯一允许做「内容形态转换」的地方：拼接、JSON 往返、事件脱敏。其余模块只调
本模块，不各自写 isinstance 分支——这是把改动从「N 处散点」收成「1 个模块 +
N 处替换」的关键（spec 2026-08-20-multimodal-design §3①）。

不变量：所有函数对 ``str`` 输入的行为与改造前的朴素字符串操作**逐字节相同**。
"""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING, Any

from ctx_weft.core.utils import content_to_text

if TYPE_CHECKING:
    from ctx_weft.protocols import ContentPart

logger = logging.getLogger(__name__)

__all__ = [
    "content_to_text",
    "content_with_prefix",
    "content_with_suffix",
    "content_to_jsonable",
    "content_from_jsonable",
    "redact_content_for_event",
]


def _is_text_part(part: Any) -> bool:
    """非文本 part 的判据与 utils.content_to_text / image_part_count 一致。

    刻意用 duck-type 而非 isinstance：测试桩与第三方 provider 可能给出等价的
    鸭子类型对象。（已知局限见 spec §13 的 dict-shaped part 隐患。）
    """
    return hasattr(part, "text")


def content_with_prefix(
    content: "str | list[ContentPart] | None", text: str
) -> "str | list[ContentPart]":
    """把 text 拼到内容开头。

    str → 朴素拼接（与改造前逐字节相同）。list → 返回**新列表**，并入首个
    TextPart；首个不是 TextPart 时新插一个。空 text 原样返回（含 list 的同一
    对象，供调用方的 no-op 判定）。None 按空文本处理。
    """
    if not text:
        return content if content is not None else ""
    if content is None:
        return text
    if isinstance(content, str):
        return f"{text}{content}"
    from ctx_weft.protocols import TextPart
    if content and _is_text_part(content[0]):
        head = dataclasses.replace(content[0], text=f"{text}{content[0].text}")
        return [head, *content[1:]]
    return [TextPart(text=text), *content]


def content_with_suffix(
    content: "str | list[ContentPart] | None", text: str
) -> "str | list[ContentPart]":
    """把 text 拼到内容末尾。语义与 content_with_prefix 对称。"""
    if not text:
        return content if content is not None else ""
    if content is None:
        return text
    if isinstance(content, str):
        return f"{content}{text}"
    from ctx_weft.protocols import TextPart
    if content and _is_text_part(content[-1]):
        tail = dataclasses.replace(content[-1], text=f"{content[-1].text}{text}")
        return [*content[:-1], tail]
    return [*content, TextPart(text=text)]


# ── JSON 往返（事件 payload / 投影快照）─────────────────────────────────────


def content_to_jsonable(
    content: "str | list[ContentPart] | None",
) -> "str | list[dict] | None":
    """把内容转成可 json.dumps 的形态。

    ContentPart 是普通 dataclass，直接进 json.dumps 会 TypeError——事件与投影
    落库前必须过这一层（spec §6.2）。str / None 原样返回，纯文本路径零成本。
    """
    if content is None or isinstance(content, str):
        return content
    out: list[dict] = []
    for part in content:
        if _is_text_part(part):
            out.append({"type": "text", "text": part.text})
        else:
            out.append({
                "type": "image",
                "data": getattr(part, "data", ""),
                "media_type": getattr(part, "media_type", ""),
                "source_type": getattr(part, "source_type", "base64"),
            })
    return out


def content_from_jsonable(
    raw: "str | list[dict] | None",
) -> "str | list[ContentPart] | None":
    """content_to_jsonable 的逆变换。

    未知 type 的元素**跳过而不抛**：将来新增 part 类型时，旧版本读到新数据应当
    降级而非崩溃（事件流是只增的，回放会遇到比自己新的数据）。
    """
    if raw is None or isinstance(raw, str):
        return raw
    from ctx_weft.protocols import ImagePart, TextPart
    out: list[ContentPart] = []
    for item in raw:
        kind = item.get("type")
        if kind == "text":
            out.append(TextPart(text=item.get("text", "")))
        elif kind == "image":
            out.append(ImagePart(
                data=item.get("data", ""),
                media_type=item.get("media_type", ""),
                source_type=item.get("source_type", "base64"),
            ))
        else:
            # 未知类型：跳过而不抛（见 docstring），但留个信号——静默丢弃数据不该完全无声。
            logger.warning("content_from_jsonable: dropping unknown content part type %r", kind)
    return out


# ── 事件脱敏 ───────────────────────────────────────────────────────────────

_REDACT_DATA_PREVIEW = 12


def redact_content_for_event(content: "str | list[ContentPart] | None") -> str:
    """把内容渲染成适合进事件 payload 的字符串。

    图片渲染成短标记而非原始 base64——一张图几万字符，直接进 LLM_PROMPT_SENT
    会把事件库撑爆（spec §6.8）。

    Phase 1 尚无调用方——留给 Phase 2 的 LLM_PROMPT_SENT 脱敏用。不是死代码，
    删除前请先确认 Phase 2 的脱敏需求已挪到别处。
    """
    if not content:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if _is_text_part(part):
            parts.append(part.text)
        else:
            data = getattr(part, "data", "") or ""
            src = getattr(part, "source_type", "base64")
            parts.append(
                f"[image {getattr(part, 'media_type', '?')} "
                f"{src}:{data[:_REDACT_DATA_PREVIEW]}…]"
            )
    return "".join(parts)
