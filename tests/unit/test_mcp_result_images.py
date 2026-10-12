"""MCP `ImageContent` → `ImagePart`：改造前这条路把图**静默丢弃**。

`_parse_tool_result` 只收 ``item.text``，MCP 的 image content block 连一条占位都不留
——模型不知道自己少拿了东西（这比"不支持"更坏：不支持至少是可见的）。

两个不变量：
1. **文本口径逐字节不变**——图片的有无不影响 text/structuredContent 的取法；
2. 图片直接进 result 的 ``content``（`str | list[ContentPart]`，与三个执行入口同一个
   联合类型），provider 自己不校验、不外部化、不抛。
"""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.protocols import ImagePart, TextPart
from ctx_weft.providers.capability_mcp.provider import (
    MCPCapabilityProvider,
    MCPServerConfig,
    _parse_tool_result,
)

B64 = "QUJDREVG" * 4


def _text(s: str):
    return SimpleNamespace(type="text", text=s)


def _image(data: str = B64, mime: str = "image/png"):
    # 官方 SDK 的 ImageContent 形状：type/data/mimeType，没有 .text
    return SimpleNamespace(type="image", data=data, mimeType=mime)


def _result(content, *, structured=None, is_error=False):
    return SimpleNamespace(content=content, structuredContent=structured,
                           isError=is_error)


# ── 1. 文本口径不变 ───────────────────────────────────────────────────────────


def test_text_only_result_is_unchanged():
    text, images = _parse_tool_result(_result([_text("a"), _text("b")]))
    assert text == "a\nb"
    assert images == []


def test_structured_fallback_is_unchanged():
    text, images = _parse_tool_result(_result([], structured={"k": "v"}))
    assert text == '{"k": "v"}'
    assert images == []


def test_empty_result_is_unchanged():
    assert _parse_tool_result(_result([])) == ("", [])
    assert _parse_tool_result(_result(None)) == ("", [])


def test_structured_still_returned_alongside_images():
    """两样都该到模型手上——图片的有无不参与文本的取法。"""
    text, images = _parse_tool_result(_result([_image()], structured={"k": "v"}))
    assert text == '{"k": "v"}'
    assert len(images) == 1


# ── 2. 图片不再被丢 ───────────────────────────────────────────────────────────


def test_image_content_becomes_image_part():
    text, images = _parse_tool_result(_result([_text("看图"), _image()]))
    assert text == "看图"
    assert images == [ImagePart(data=B64, media_type="image/png",
                                source_type="base64")]


def test_image_only_result_yields_empty_text_and_one_part():
    """gateway 见流里有非文本 part 就不写 "(no output)"，故空文本是对的。"""
    text, images = _parse_tool_result(_result([_image()]))
    assert text == ""
    assert len(images) == 1


def test_mime_type_is_passed_through_verbatim():
    """归一（大小写 / ``; charset=`` / ``image/jpg``）归白名单匹配那侧；
    wire 序列化要的是 server 报的原值。"""
    _, images = _parse_tool_result(_result([_image(mime="IMAGE/JPG")]))
    assert images[0].media_type == "IMAGE/JPG"


def test_unknown_block_kinds_are_skipped_like_before():
    """EmbeddedResource / AudioContent 不在本期范围：既无 .text 也非 image，跳过。"""
    blob = SimpleNamespace(type="resource", resource=SimpleNamespace(blob="…"))
    audio = SimpleNamespace(type="audio", data=B64, mimeType="audio/wav")
    assert _parse_tool_result(_result([blob, audio])) == ("", [])


def test_image_block_without_data_is_skipped():
    assert _parse_tool_result(_result([_image(data="")])) == ("", [])


# ── 3. invoke 把图挂进 metadata ───────────────────────────────────────────────


async def test_invoke_puts_images_into_the_result_content():
    p = MCPCapabilityProvider(MCPServerConfig(name="shot"))

    class _Session:
        async def call_tool(self, name, arguments=None):
            return _result([_text("done"), _image()])

    p._session = _Session()
    async def _noop() -> None: return None
    p._ensure_connected = _noop                      # type: ignore[method-assign]

    events = [ev async for ev in p.invoke("mcp:shot:screenshot", {}, None)]
    assert len(events) == 1
    payload = events[0].payload
    assert payload["content"] == [TextPart(text="done"),
                                  ImagePart(data=B64, media_type="image/png",
                                            source_type="base64")]
    assert payload["metadata"] == {"is_error": False}


async def test_invoke_omits_the_channel_key_when_there_is_no_image():
    """无图时 content 仍是 **str**、metadata 只有 is_error——逐字节同改造前。"""
    p = MCPCapabilityProvider(MCPServerConfig(name="shot"))

    class _Session:
        async def call_tool(self, name, arguments=None):
            return _result([_text("plain")])

    p._session = _Session()
    async def _noop() -> None: return None
    p._ensure_connected = _noop                      # type: ignore[method-assign]

    events = [ev async for ev in p.invoke("mcp:shot:t", {}, None)]
    assert events[0].payload == {"content": "plain", "metadata": {"is_error": False}}
    assert type(events[0].payload["content"]) is str
