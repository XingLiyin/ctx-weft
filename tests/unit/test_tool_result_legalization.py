"""`legalize_tool_result_parts`：工具返图这条路的入口补课。

三个执行入口与 HITL 应答都走 `runtime._validate_and_normalize_content`（validate →
双侧外部化）；工具结果绕开了它。在 `media:get_image` 是唯一生产者的年代无所谓——它交
出来的本就是 ``source_type="ref"``。第三方 provider 交 inline base64 之后，缺的这一课
就是三个洞：白名单/尺寸一次不跑、裸 base64 落进 memory、宿主接了 blob store 也用不上。

本文件钉三条不变量：
1. **纯文本 / ref 形态零影响**（`media:get_image` 那条路逐字节不变）；
2. **不合格的图换确定性占位，恒不抛**（工具循环上不许掀桌）；
3. **接了 store 就外部化，put 失败降级成占位并记 error**（不偷偷退回 inline base64）。
"""

from __future__ import annotations

import base64
import logging

import pytest

from ctx_weft.core.utils.content import legalize_tool_result_parts
from ctx_weft.protocols import ImagePart, ProviderContext, TextPart
from ctx_weft.protocols.memory import MemoryBlobStore

_LOGGER = "ctx_weft.core.utils.content"

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"payload" * 4
PNG_B64 = base64.b64encode(PNG_BYTES).decode()


def _ctx() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default",
                           task_id="tsk_1", agent_id="agt_1")


class _Store(MemoryBlobStore):
    """内容寻址的假 store；`fail=True` 时 put 抛错（模拟存储不可用）。"""

    def __init__(self, *, can: bool = True, fail: bool = False) -> None:
        self._can = can
        self._fail = fail
        self.puts: list[bytes] = []

    @property
    def can_externalize(self) -> bool:
        return self._can

    async def put(self, data: bytes, media_type: str, ctx) -> str:
        if self._fail:
            raise RuntimeError("blob store down")
        self.puts.append(data)
        return f"blob:{len(data):08d}"

    async def get(self, ref: str, ctx):
        return None


# ── 1. 零影响面 ───────────────────────────────────────────────────────────────


async def test_text_parts_pass_through_untouched():
    t = TextPart(text="hello")
    out = await legalize_tool_result_parts([t], blob_store=_Store(), ctx=_ctx())
    assert out == [t]
    assert out[0] is t, "文本 part 不该被重建"


async def test_ref_parts_are_never_revalidated():
    """`media:get_image` 交出来的就是这个形状，且 media_type 允许回落成 ``image``
    （`media/refs.py` 的 _UNKNOWN_MEDIA_TYPE）——那个值不在白名单里，跑一遍校验就会
    把一张刚取回来的图误伤成占位。"""
    ref = ImagePart(data="blob:abc", media_type="image", source_type="ref")
    out = await legalize_tool_result_parts([ref], blob_store=_Store(), ctx=_ctx())
    assert out == [ref]
    assert out[0] is ref


async def test_empty_input_returns_empty_list():
    assert await legalize_tool_result_parts(None, blob_store=_Store(), ctx=_ctx()) == []
    assert await legalize_tool_result_parts([], blob_store=_Store(), ctx=_ctx()) == []


async def test_no_blob_store_keeps_inline_bytes():
    """不接 blob 的宿主：inline base64 原样跑（多模态设计 Phase 2 的既定形态），
    不因为多了这道合法化就把图丢掉。"""
    img = ImagePart(data=PNG_B64, media_type="image/png")
    for store in (None, _Store(can=False)):
        out = await legalize_tool_result_parts([img], blob_store=store, ctx=_ctx())
        assert out == [img]
        assert out[0] is img


# ── 2. 外部化 ────────────────────────────────────────────────────────────────


async def test_inline_image_is_externalized_when_store_present():
    store = _Store()
    img = ImagePart(data=PNG_B64, media_type="image/png")
    out = await legalize_tool_result_parts([img], blob_store=store, ctx=_ctx())

    assert len(out) == 1
    assert out[0].source_type == "ref"
    assert out[0].data.startswith("blob:")
    assert out[0].media_type == "image/png"
    # byte_size 必须在这里记下：外部化之后 data 是 ref，体积信息就此丢失，
    # 而 image_tokens 是同步的、取不回来。
    assert out[0].byte_size == len(PNG_BYTES)
    assert store.puts == [PNG_BYTES]
    assert img.source_type == "base64", "不得就地改写调用方的 part"


async def test_one_bad_image_does_not_block_the_others():
    store = _Store()
    good = ImagePart(data=PNG_B64, media_type="image/png")
    bad = ImagePart(data=PNG_B64, media_type="image/bmp")
    out = await legalize_tool_result_parts(
        [good, bad, good], blob_store=store, ctx=_ctx())

    assert [type(p).__name__ for p in out] == ["ImagePart", "TextPart", "ImagePart"]
    assert len(store.puts) == 2


# ── 3. 降级：恒不抛，占位确定 ─────────────────────────────────────────────────


@pytest.mark.parametrize("part, why", [
    (ImagePart(data=PNG_B64, media_type="image/bmp"), "白名单外的 media_type"),
    (ImagePart(data="not base64!!", media_type="image/png"), "畸形 base64"),
    (ImagePart(data=base64.b64encode(b"x" * (5 * 1024 * 1024 + 1)).decode(),
               media_type="image/png"), "超 5 MiB"),
    (ImagePart(data="http://x/y.png", media_type="image/png", source_type="url"), None),
])
async def test_illegal_images_become_placeholders_not_exceptions(part, why):
    """`url` 形态那条是对照：它走「非 base64 → 原样透传」分支，不该被降级。"""
    out = await legalize_tool_result_parts([part], blob_store=_Store(), ctx=_ctx())
    assert len(out) == 1
    if why is None:
        assert out[0] is part
        return
    assert isinstance(out[0], TextPart), why
    assert out[0].text.startswith("[image dropped: "), why
    assert "rejected by content validation" in out[0].text, why


async def test_store_failure_downgrades_and_logs_error(caplog):
    """宿主**接了** store 就是明确表态「字节不进记录行」——put 失败不能偷偷退回
    inline base64（一张图几 MB 直接进 memory 行），降级成占位并响亮记 error。"""
    img = ImagePart(data=PNG_B64, media_type="image/png")
    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        out = await legalize_tool_result_parts(
            [img], blob_store=_Store(fail=True), ctx=_ctx())

    assert isinstance(out[0], TextPart)
    assert out[0].text == "[image dropped: image/png could not be stored]"
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_placeholder_is_byte_for_byte_deterministic():
    """占位处在 prompt 前缀里，每次不同会砸掉其后整段自动前缀缓存（同
    `downgrade_images_to_text` 的硬约束）。"""
    bad = ImagePart(data=PNG_B64, media_type="image/bmp")
    a = await legalize_tool_result_parts([bad], blob_store=_Store(), ctx=_ctx())
    b = await legalize_tool_result_parts([bad], blob_store=_Store(), ctx=_ctx())
    assert a[0].text == b[0].text
    assert a[0].text == "[image dropped: image/bmp rejected by content validation]"


async def test_dict_shaped_parts_are_normalized_before_validation():
    """宿主 provider 可能给 dict 形态（JSON 往返）。先过归一层，否则校验对它是瞎的。"""
    store = _Store()
    out = await legalize_tool_result_parts(
        [{"type": "image", "data": PNG_B64, "media_type": "image/png"}],
        blob_store=store, ctx=_ctx())
    assert out[0].source_type == "ref"
    assert store.puts == [PNG_BYTES]
