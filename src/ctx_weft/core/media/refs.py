"""L0.5 图片占位的编解码——**本仓唯一知道 L0.5 占位长什么样的地方**（子设计 §7）。

`_history.py` 不解析占位，`composer` 不感知取回，`reducers` / `control/types` 一个字不改。
Task 2 的降级（写占位）与 Task 4 的 `media:get_image`（读占位取 ref）都只经由本模块。

════════════════════════════════════════════════════════════════════════════
本仓所有「图片占位」清单（L6 收口，controller 裁定 R1）
════════════════════════════════════════════════════════════════════════════

占位文案曾经中英混杂、四处各写各的、无单一真源。裁定：**只有 L0.5 的占位需要被解析
回来**（`get_image` 要从占位里取出 ref），其余四种都是**单向渲染、永不回读**——故它们
**保持在原处**（`content.py` 是归一层、`openai.py` 是 wire 格式，各归其位），不搬进
`core/media/`（那会把 wire 格式知识拖进 core 模块），只各加一行注释指向本清单。

| # | 文案 | 落点 | 语义 | 需解析？ |
|---|---|---|---|---|
| 1 | ``[image {ref} media_type={mt} — dropped to save context; call media:get_image("{ref}") to bring it back]`` | 本模块 | **L0.5 降级**：重写 memory 记录、**落库**、可被取回 | **是** |
| 2 | ``[image {media_type}]`` | `core/content.py::_IMAGE_PLACEHOLDER_TMPL`，产出方二：`providers/llm/_modality.py::downgrade_for_text_only`（经 `AnthropicAdapter` / `OpenAIAdapter` 的 `_prepare_messages`），两者共用同一常量与同一底层函数（`downgrade_images_to_text`） | **per-purpose 降级**（Phase 3c）：**不落库**，只影响本次 prompt | 否 |
| 3 | ``[image unavailable: {media_type}]`` | `core/content.py::_IMAGE_UNAVAILABLE_TMPL` | 出网 rehydrate 取不回 blob 时的降级 | 否 |
| 4 | ``[image see the following message]`` | `providers/llm/openai.py::_TOOL_IMAGE_NOTICE` | OpenAI `role="tool"` 只收文本，图重定位到随后的 user 消息 | 否 |
| 5 | ``[image unavailable]`` | `providers/memory/sql/*.py` docstring | 仅文档举例，**不是活代码** | 否 |

⚠️ 2 与 1 **并存、互不替代**：per-purpose 是「这次不发」，L0.5 是「从记忆里收起来」。
改 1 不要顺手动 2。

════════════════════════════════════════════════════════════════════════════
两条硬约束
════════════════════════════════════════════════════════════════════════════

**往返无损** —— `decode(encode(ref, mt)) == (ref, mt)`。这条决定了占位里放的是
**完整 ref**（`blob:<sha>`）而不是短 sha：短 sha 解不回完整 ref，`get_image` 就得自己
维护一张短 sha → ref 的映射表（多一份可失配的状态），且短 sha 在同一 task 内可能撞车。
完整 ref 约 69 字符、出现两次（标识 + 调用示例），代价约几十 token；被它换掉的那张图
最低也按 1600 token 计（`utils.image_tokens`），量级差两个数量级，不值得为此引入映射表。

**逐字节确定性** —— 同一 (ref, media_type) 每次产出完全相同的文本，不得含随机 id /
时间戳 / 跨调用计数器（台账 Global Constraints，同用户裁定 D2 的缓存理由）：占位处在
prompt 前缀里，每次不同会砸掉其后整段自动前缀缓存——而 L0.5 降级恰恰发生在上下文最紧张、
最需要命中缓存的时刻。

**措辞待实测校准**（子设计 §12 未决）：占位文本决定模型会不会主动调 `get_image`，故
措辞收在模块级常量 `IMAGE_PLACEHOLDER_TEMPLATE` 里、不散进 f-string 逻辑，且正则由模板
**推导**而来（`compile_placeholder_pattern`）而非手写第二份真源——改措辞不会漏改解析。
"""

from __future__ import annotations

import re

__all__ = [
    "IMAGE_PLACEHOLDER_TEMPLATE",
    "compile_placeholder_pattern",
    "decode_image_placeholder",
    "encode_image_placeholder",
    "find_image_placeholders",
]

# 单行（子设计 §4.1 示例里的换行只是文档折行）：占位要作为**一个** TextPart 落库，
# 含换行会被按行处理的路径切开，也会让「一段文本里数占位个数」变得不可靠。
IMAGE_PLACEHOLDER_TEMPLATE = (
    "[image {ref} media_type={media_type} — dropped to save context; "
    'call media:get_image("{ref}") to bring it back]'
)

#: media_type 缺失时的兜底 token。与 `content.py::downgrade_images_to_text` 同口径。
#: 不能留空——``media_type=`` 后面没有 token 的占位解不回来，往返即有损。
_UNKNOWN_MEDIA_TYPE = "image"

# 占位内的字段是「token」：不含空白、不含 ``]``（``]`` 是默认模板的收尾字符）。
# 用**非贪婪**量词：贪婪匹配在「字段后面紧跟非空白分隔符」的模板下会把分隔符一起吞掉。
_REF_GROUP = r"(?P<ref>[^\s\]]+?)"
_MT_GROUP = r"(?P<media_type>[^\s\]]+?)"

# 纯字母的哨兵：`re.escape` 对字母数字不做任何改写，故 escape 之后仍能原样替换回去。
_REF_SENTINEL = "ZqREFSENTINELqZ"
_MT_SENTINEL = "ZqMTSENTINELqZ"


def compile_placeholder_pattern(
    template: str = IMAGE_PLACEHOLDER_TEMPLATE,
) -> re.Pattern[str]:
    """由模板**推导**出解析用正则——模板是唯一真源，改措辞不会漏改解析。

    ``{ref}`` 在模板里出现两次（标识 + 调用示例）：第一次编译成捕获组，其后的编译成
    **反向引用**。于是两处 ref 不一致的文本（被改写过 / 拼接出来的赝品）解不出结果，
    而不是解出一个来路不明的 ref 交给 `get_image`。
    """
    if "{ref}" not in template or "{media_type}" not in template:
        raise ValueError(f"占位模板必须同时含 {{ref}} 与 {{media_type}}：{template!r}")
    literal = template.format(ref=_REF_SENTINEL, media_type=_MT_SENTINEL)
    pattern = re.escape(literal)
    if _REF_SENTINEL not in pattern or _MT_SENTINEL not in pattern:
        # re.escape 改写了哨兵 → 下面的 replace 会静默失配，产出一个永不匹配的正则。
        raise RuntimeError("占位哨兵被 re.escape 改写，无法由模板推导正则")
    pattern = pattern.replace(_REF_SENTINEL, _REF_GROUP, 1)
    pattern = pattern.replace(_REF_SENTINEL, "(?P=ref)")
    pattern = pattern.replace(_MT_SENTINEL, _MT_GROUP, 1)
    pattern = pattern.replace(_MT_SENTINEL, "(?P=media_type)")
    return re.compile(pattern)


_PLACEHOLDER_RE = compile_placeholder_pattern()


def _token(value: str, fallback: str = "") -> str:
    """字段值必须是可解析回来的 token；不合格时落到 fallback（空 fallback 表示不接受）。"""
    text = str(value or "").strip()
    if text and not any(ch.isspace() for ch in text) and "]" not in text:
        return text
    return fallback


def encode_image_placeholder(
    ref: str,
    media_type: str,
    *,
    template: str = IMAGE_PLACEHOLDER_TEMPLATE,
) -> str:
    """把 (ref, media_type) 编成 L0.5 占位文本。逐字节确定；同一输入恒等。

    ``ref`` 必须是可解析回来的 token（非空、无空白、无 ``]``）——不满足就是调用方的
    bug（Task 2 只会传 `ImagePart.data` 里的 ``blob:<sha>``），此时 **抛 ValueError**
    而不是产出一个解不回来的占位：占位落库之后就是永久的，静默产出坏占位等于把图弄丢。
    ``media_type`` 缺失则落到 ``image``（与 per-purpose 降级同口径），不抛。
    """
    token = _token(ref)
    if not token:
        raise ValueError(f"ref 不是可解析的 token，拒绝产出占位：{ref!r}")
    return template.format(
        ref=token, media_type=_token(media_type, _UNKNOWN_MEDIA_TYPE))


def decode_image_placeholder(
    text: str,
    *,
    pattern: re.Pattern[str] | None = None,
) -> tuple[str, str] | None:
    """从文本里解出**第一个** L0.5 占位的 ``(ref, media_type)``；不是占位则返回 ``None``。

    **不抛**——调用方喂进来的是 memory 里的任意历史文本（含另外四种占位、含用户原文），
    「不是占位」是常态而非异常。非字符串入参同样返回 ``None``。
    一段文本里的**全部**占位用 `find_image_placeholders`。
    """
    if not isinstance(text, str) or not text:
        return None
    m = (pattern or _PLACEHOLDER_RE).search(text)
    if m is None:
        return None
    return m.group("ref"), m.group("media_type")


def find_image_placeholders(
    text: str,
    *,
    pattern: re.Pattern[str] | None = None,
) -> list[tuple[str, str]]:
    """按**文档顺序**提取文本里的所有 L0.5 占位。**不去重**——同一 ref 出现两次就返回两条。

    一条记录的 content 被拍平成一段文本时可能含多个占位（多图消息 / L1、L3 折叠后 ref
    随摘要留存），`get_image` 要在其中找特定 ref，故必须能全部拿到、且顺序稳定
    （顺序即「第几张图」，Task 4 生成位置信息要用）。
    """
    if not isinstance(text, str) or not text:
        return []
    return [(m.group("ref"), m.group("media_type"))
            for m in (pattern or _PLACEHOLDER_RE).finditer(text)]
