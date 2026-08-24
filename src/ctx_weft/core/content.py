"""内容形态归一层（多模态 Phase 1）。

全仓唯一允许做「内容形态转换」的地方：拼接、JSON 往返、事件脱敏。其余模块只调
本模块，不各自写 isinstance 分支——这是把改动从「N 处散点」收成「1 个模块 +
N 处替换」的关键（spec 2026-08-20-multimodal-design §3①）。

不变量：所有函数对 ``str`` 输入的行为与改造前的朴素字符串操作**逐字节相同**。
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from ctx_weft.core.utils import content_to_text

if TYPE_CHECKING:
    from ctx_weft.protocols import ContentPart

__all__ = [
    "content_to_text",
    "content_with_prefix",
    "content_with_suffix",
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
