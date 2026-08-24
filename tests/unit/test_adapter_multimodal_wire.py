"""Task 5: LLM adapter 转 wire blocks —— Anthropic / OpenAI / Mock 三家。

纯文本 wire 形态必须逐字节不变（基准为 controller 实测值，见 brief）；
多模态输入（parts 含 ImagePart）必须转成各家 provider 的 wire block 形态。
"""

from ctx_weft.protocols import ImagePart, LLMMessage, TextPart
from ctx_weft.providers.llm.anthropic import _serialize_messages as anth
from ctx_weft.providers.llm.openai import _serialize_messages as oai


def _img():
    return ImagePart(data="ZGF0YQ==", media_type="image/png")


def _plain():
    return [LLMMessage(role="user", content="hello"),
            LLMMessage(role="assistant", content="hi", tool_calls=[]),
            LLMMessage(role="tool", content="result", tool_call_id="tc1")]


# ── 纯文本 wire 形态逐字节不变（基准为 controller 实测值）────────────────

def test_anthropic_plain_text_wire_unchanged():
    assert anth(_plain()) == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tc1", "content": "result"}]},
    ]


def test_openai_plain_text_wire_unchanged():
    assert oai("sys", _plain()) == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "tool", "tool_call_id": "tc1", "content": "result"},
    ]


# ── Anthropic 多模态 ─────────────────────────────────────────────────────

def test_anthropic_user_image_becomes_image_block():
    out = anth([LLMMessage(role="user", content=[TextPart(text="看图"), _img()])])
    blocks = out[0]["content"]
    assert isinstance(blocks, list), "parts 输入必须产出 block 列表"
    assert {"type": "text", "text": "看图"} in blocks
    assert {"type": "image",
            "source": {"type": "base64", "media_type": "image/png",
                       "data": "ZGF0YQ=="}} in blocks


def test_anthropic_tool_result_accepts_blocks():
    out = anth([LLMMessage(role="tool", content=[TextPart(text="r"), _img()],
                           tool_call_id="tc1")])
    tr = out[0]["content"][0]
    assert tr["type"] == "tool_result" and tr["tool_use_id"] == "tc1"
    assert isinstance(tr["content"], list)
    assert any(b.get("type") == "image" for b in tr["content"])


# ── OpenAI 多模态 ────────────────────────────────────────────────────────

def test_openai_user_image_becomes_image_url():
    out = oai("", [LLMMessage(role="user", content=[TextPart(text="看图"), _img()])])
    parts = out[0]["content"]
    assert isinstance(parts, list)
    assert {"type": "text", "text": "看图"} in parts
    url = next(p["image_url"]["url"] for p in parts if p["type"] == "image_url")
    assert url == "data:image/png;base64,ZGF0YQ=="


def test_openai_tool_message_flattens_to_text():
    """OpenAI 的 role="tool" 只接受文本——本 Phase 刻意拍扁，不得抛。"""
    out = oai("", [LLMMessage(role="tool", content=[TextPart(text="r"), _img()],
                              tool_call_id="tc1")])
    assert isinstance(out[0]["content"], str)
    assert "r" in out[0]["content"]


# ── Mock 多模态 ──────────────────────────────────────────────────────────
# mock.py:101 用 (m.content if isinstance(m.content, str) else "") 拍扁 usage 估算用的
# prompt_text；多模态消息会变空串。改为 content_to_text(m.content) 后仍能取到文本部分。
# MockLLMAdapter._stream 不是可脱离 request/response 直接调用的纯函数，这里走一次完整
# complete() 驱动，断言 usage 的 prompt_tokens 因为含图片文本部分而 > 0（不因图片拍扁成空串
# 而归零），且过程不抛。

import asyncio

from ctx_weft.protocols import LLMRequest
from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse


def test_mock_multimodal_prompt_text_not_dropped():
    adapter = MockLLMAdapter(responses=[MockResponse(text="ok")])
    request = LLMRequest(
        model="mock",
        system="",
        messages=[LLMMessage(role="user", content=[TextPart(text="看图"), _img()])],
        tools=[],
    )

    async def _run():
        chunks = []
        async for ch in adapter.complete(request):
            chunks.append(ch)
        return chunks

    chunks = asyncio.run(_run())
    usage_chunks = [c for c in chunks if c.kind == "usage"]
    assert usage_chunks, "mock 应产出 usage chunk"
    assert usage_chunks[0].usage.prompt_tokens > 0, (
        "多模态消息拍扁成空串会导致 prompt_tokens 归零——"
        "content_to_text 必须能取到文本 part"
    )
