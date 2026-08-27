"""Phase 4 Task 1：L0.5 占位的编解码（`core/media/refs.py`）。

本仓有五种图片占位（清单见 `core/media/refs.py` 模块 docstring），**只有 L0.5 这一种
需要被解析回来**（controller 裁定 R1）——`media:get_image` 要从占位文本里把 ref 取出来。
其余四种单向渲染、永不回读，故不在本文件覆盖范围内。

两条硬约束在这里钉死：

- **往返无损**：`decode(encode(ref, mt)) == (ref, mt)`。这条直接决定了占位里必须放
  **完整 ref** 而不是短 sha——短 sha 解不回完整 ref，往返就是有损的。
- **逐字节确定性**（台账 Global Constraints）：同一输入两次调用产出必须完全相同。
  这是「某件事没有发生」型断言（没有随机成分），最易写成永真，故 Step 6 对它做了
  变异验证。
"""

from __future__ import annotations

from ctx_weft.core.media.refs import (
    IMAGE_PLACEHOLDER_TEMPLATE,
    compile_placeholder_pattern,
    decode_image_placeholder,
    encode_image_placeholder,
    find_image_placeholders,
)
from ctx_weft.protocols import BLOB_REF_PREFIX

_SHA_A = "ab12cd34" * 8          # 64 hex，与 SqlMemoryProvider 产出的 sha 同形
_SHA_B = "0f9e8d7c" * 8
_REF_A = f"{BLOB_REF_PREFIX}{_SHA_A}"
_REF_B = f"{BLOB_REF_PREFIX}{_SHA_B}"

_CASES = [
    (_REF_A, "image/png"),
    (_REF_B, "image/jpeg"),
    (f"{BLOB_REF_PREFIX}deadbeef", "image/svg+xml"),
    (f"{BLOB_REF_PREFIX}0", "image/webp"),
]


# ── 1. 编解码往返无损 ────────────────────────────────────────────────────────


def test_round_trip_is_lossless() -> None:
    for ref, media_type in _CASES:
        text = encode_image_placeholder(ref, media_type)
        assert decode_image_placeholder(text) == (ref, media_type), \
            f"往返有损：{text!r}"


def test_placeholder_carries_the_complete_ref_not_a_short_sha() -> None:
    """占位里必须是完整 ref（含 ``blob:`` 前缀），模型才能原样传回 ``get_image(ref)``。

    这条是往返无损的**结构性前提**：占位里只留短 sha 的话，`decode` 无从还原出
    `ImagePart.data` 那个完整 ref，Task 4 的 `get_image` 就得自己维护一张
    短 sha → ref 的映射表（多一份可失配的状态）。
    """
    text = encode_image_placeholder(_REF_A, "image/png")
    assert _REF_A in text, f"占位里没有完整 ref：{text!r}"
    ref, _ = decode_image_placeholder(text) or ("", "")
    assert ref.startswith(BLOB_REF_PREFIX), f"解出来的不是完整 ref：{ref!r}"


def test_round_trip_survives_a_different_template() -> None:
    """措辞是可配置的（子设计 §12 标为未决，待实测校准）——换了模板仍须往返无损。

    正则由模板推导而来，不是手写的第二份真源；这条用例守的就是「模板改了正则不会漏改」。
    """
    tmpl = 'IMG<{ref}|{media_type}> gone; media:get_image("{ref}")'
    pattern = compile_placeholder_pattern(tmpl)
    text = encode_image_placeholder(_REF_A, "image/png", template=tmpl)
    assert text == 'IMG<%s|image/png> gone; media:get_image("%s")' % (_REF_A, _REF_A)
    assert decode_image_placeholder(text, pattern=pattern) == (_REF_A, "image/png")


# ── 2. 逐字节确定性（无随机成分）─────────────────────────────────────────────


def test_encode_is_byte_for_byte_deterministic() -> None:
    """同一 (ref, media_type) 每次产出完全相同的文本。

    缓存前缀约束：占位处在 prompt 前缀里，每次不同会把其后的整段自动前缀缓存砸掉——
    而 L0.5 降级恰恰发生在上下文最紧张、最需要命中缓存的时刻。
    """
    for ref, media_type in _CASES:
        first = encode_image_placeholder(ref, media_type)
        second = encode_image_placeholder(ref, media_type)
        assert first == second, f"占位不确定：{first!r} != {second!r}"


def test_placeholder_has_no_random_component_across_process_state() -> None:
    """占位不含随机 id / 时间戳 / 计数器。

    「两次调用相等」是必要条件；这里再加两条独立的钉子，免得实现里塞进一个
    「每进程固定但每次运行不同」的成分（两次调用相等仍成立，缓存却在跨进程时失效）：

    - 交叉调用其他输入之后再编码同一输入，结果不变（排除跨调用计数器）；
    - 占位里出现的数字**全部来自 ref 与 media_type 本身**（排除时间戳 / 序号）。
    """
    first = encode_image_placeholder(_REF_A, "image/png")
    for other_ref, other_mt in _CASES:
        encode_image_placeholder(other_ref, other_mt)
    assert encode_image_placeholder(_REF_A, "image/png") == first, \
        "交叉调用后产出变了——占位里有跨调用状态（计数器 / 缓存）"

    ref = f"{BLOB_REF_PREFIX}abcdef"          # 无数字的 ref
    text = encode_image_placeholder(ref, "image/png")
    surplus = text.replace(ref, "").replace("image/png", "")
    assert not any(ch.isdigit() for ch in surplus), \
        f"占位的固定部分含数字（时间戳 / 序号 / sha 的征兆）：{surplus!r}"


def test_template_constant_is_not_built_at_import_time_from_volatile_state() -> None:
    """措辞是模块级常量，不是 f-string 散在逻辑里（便于日后按实测校准措辞）。"""
    assert "{ref}" in IMAGE_PLACEHOLDER_TEMPLATE
    assert "{media_type}" in IMAGE_PLACEHOLDER_TEMPLATE
    assert encode_image_placeholder(_REF_A, "image/png") == \
        IMAGE_PLACEHOLDER_TEMPLATE.format(ref=_REF_A, media_type="image/png")


# ── 3. decode 对非占位文本返回 None，不抛 ───────────────────────────────────


def test_decode_returns_none_for_non_placeholder_text() -> None:
    for text in [
        "",
        "hello world",
        "[image image/png]",                       # per-purpose 降级的占位（另一种）
        "[image unavailable: image/png]",          # rehydrate 取不回的占位（另一种）
        "[image see the following message]",       # openai tool 重定位标记（另一种）
        "[image blob:abc media_type=image/png]",   # 形似但措辞不全
        _REF_A,
        "[image  media_type=image/png — dropped to save context; "
        'call media:get_image("") to bring it back]',   # 空 ref
        None,                                      # type: ignore[list-item]
        1234,                                      # type: ignore[list-item]
    ]:
        assert decode_image_placeholder(text) is None, f"不该解出东西：{text!r}"


def test_decode_rejects_a_placeholder_whose_two_refs_disagree() -> None:
    """占位里 ref 出现两次（标识 + 调用示例），两处不一致说明文本被改写过 → 不解析。"""
    text = encode_image_placeholder(_REF_A, "image/png").replace(_REF_A, _REF_B, 1)
    assert decode_image_placeholder(text) is None


# ── 4. 一段文本含多个占位 → 全部提取，顺序稳定 ───────────────────────────────


def test_find_extracts_every_placeholder_in_document_order() -> None:
    text = (
        "before "
        + encode_image_placeholder(_REF_A, "image/png")
        + " middle "
        + encode_image_placeholder(_REF_B, "image/jpeg")
        + " after"
    )
    assert find_image_placeholders(text) == [
        (_REF_A, "image/png"), (_REF_B, "image/jpeg")]
    assert find_image_placeholders(text) == find_image_placeholders(text), "顺序须稳定"


def test_find_returns_empty_for_text_without_placeholders() -> None:
    assert find_image_placeholders("plain text") == []
    assert find_image_placeholders("") == []
    assert find_image_placeholders(None) == []          # type: ignore[arg-type]


def test_find_keeps_duplicates_of_the_same_ref() -> None:
    """同一 ref 的两个占位（同一记录里的同一张图被引两次）不去重——调用方自己决定。"""
    text = "%s %s" % (encode_image_placeholder(_REF_A, "image/png"),
                      encode_image_placeholder(_REF_A, "image/png"))
    assert find_image_placeholders(text) == [
        (_REF_A, "image/png"), (_REF_A, "image/png")]


def test_decode_returns_the_first_placeholder_when_several_are_present() -> None:
    text = "x %s y %s" % (encode_image_placeholder(_REF_A, "image/png"),
                          encode_image_placeholder(_REF_B, "image/jpeg"))
    assert decode_image_placeholder(text) == (_REF_A, "image/png")


# ── 5. 边界：空 media_type ──────────────────────────────────────────────────


def test_empty_media_type_falls_back_to_a_parsable_token() -> None:
    """media_type 缺失时不得产出 ``media_type=`` 这种解不回来的形态。

    与 `content.py::downgrade_images_to_text` 的兜底口径一致（落到 ``image``）。
    """
    text = encode_image_placeholder(_REF_A, "")
    assert decode_image_placeholder(text) == (_REF_A, "image")


def test_placeholder_is_single_line() -> None:
    """占位不得含换行——它要作为**一个** TextPart 落库，且不能被按行处理的路径切开。

    （子设计 §4.1 的示例里那个换行只是文档折行。）
    """
    assert "\n" not in encode_image_placeholder(_REF_A, "image/png")
