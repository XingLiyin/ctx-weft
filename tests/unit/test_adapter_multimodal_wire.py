"""Task 5: LLM adapter 转 wire blocks —— Anthropic / OpenAI / Mock 三家。

纯文本 wire 形态必须逐字节不变（基准为 controller 实测值，见 brief）；
多模态输入（parts 含 ImagePart）必须转成各家 provider 的 wire block 形态。
"""

import asyncio

from ctx_weft.protocols import ImagePart, LLMMessage, LLMRequest, TextPart
from ctx_weft.providers.llm.anthropic import _serialize_messages as anth
from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
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
    """OpenAI 的 role="tool" 只接受文本——本 Phase 刻意拍扁，不得抛，且不得泄漏图片的
    base64/repr（fix round 1 finding 1/2：拍扁兜底若误用 str(p)，ImagePart 的完整 base64 +
    元数据会原样拼进发给模型的文本）。"""
    out = oai("", [LLMMessage(role="tool", content=[TextPart(text="r"), _img()],
                              tool_call_id="tc1")])
    content = out[0]["content"]
    assert isinstance(content, str)
    assert "r" in content
    assert "ZGF0YQ==" not in content, "不得把图片 base64 拍进拍扁后的文本"
    assert "ImagePart" not in content, "不得把 dataclass repr 拍进拍扁后的文本"


def test_openai_assistant_tool_calls_image_becomes_image_url():
    """assistant-with-tool_calls 分支（openai.py:380 附近）与 user 分支走同一
    _parts_to_blocks，此前没有多模态测试覆盖（fix round 1 finding 4）。"""
    out = oai("", [LLMMessage(
        role="assistant", content=[TextPart(text="看图"), _img()],
        tool_calls=[{"id": "tc1", "name": "noop", "arguments": {}}],
    )])
    parts = out[0]["content"]
    assert isinstance(parts, list)
    assert {"type": "text", "text": "看图"} in parts
    url = next(p["image_url"]["url"] for p in parts if p["type"] == "image_url")
    assert url == "data:image/png;base64,ZGF0YQ=="
    assert out[0]["tool_calls"][0]["id"] == "tc1"


# ── I1: 空文本 part 挨着 ImagePart 时不得出网（Anthropic 会因空文本块整条 400）──────

def test_anthropic_empty_text_part_next_to_image_dropped():
    out = anth([LLMMessage(role="user", content=[TextPart(text=""), _img()])])
    blocks = out[0]["content"]
    assert {"type": "text", "text": ""} not in blocks, (
        "空 TextPart 不得产出空文本 block——Anthropic 对空/纯空白文本块整条 400"
    )
    assert any(b.get("type") == "image" for b in blocks)


def test_openai_empty_text_part_next_to_image_dropped():
    out = oai("", [LLMMessage(role="user", content=[TextPart(text=""), _img()])])
    parts = out[0]["content"]
    assert {"type": "text", "text": ""} not in parts, (
        "空 TextPart 不得产出空文本 block"
    )
    assert any(p.get("type") == "image_url" for p in parts)


# ── anthropic.py:360 的 `content_blocks or ""` 兜底路径 ─────────────────────
# assistant 带 tool_calls 且 content 为空字符串：content_blocks 因 tool_use block 非空，
# 不得落进 `or ""` 分支被兜底成空字符串（会把 tool_use 一并丢掉）。此前无测试覆盖。

def test_anthropic_assistant_empty_content_with_tool_calls_keeps_tool_use_block():
    out = anth([LLMMessage(
        role="assistant", content="", tool_calls=[{"id": "tc1", "name": "noop", "arguments": {}}],
    )])
    content = out[0]["content"]
    assert isinstance(content, list), "带 tool_calls 时不得被兜底成空字符串"
    assert any(b.get("type") == "tool_use" and b.get("id") == "tc1" for b in content)


# ── Mock 多模态 ──────────────────────────────────────────────────────────
# mock.py:101 用 (m.content if isinstance(m.content, str) else "") 拍扁 usage 估算用的
# prompt_text；多模态消息会变空串。改为 content_to_text(m.content) 后仍能取到文本部分。
# MockLLMAdapter._stream 不是可脱离 request/response 直接调用的纯函数，这里走一次完整
# complete() 驱动，断言 usage 的 prompt_tokens 因为含图片文本部分而 > 0（不因图片拍扁成空串
# 而归零），且过程不抛。


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


# ── Q7: 纯空白文本块与空文本块同属一类——Anthropic 对两者都返回 400 ────────

def test_anthropic_whitespace_text_part_next_to_image_dropped():
    """纯空白文本块与空文本块同属一类：Anthropic 对两者都返回 400。"""
    out = anth([LLMMessage(role="user", content=[TextPart(text="   "), _img()])])
    blocks = out[0]["content"]
    assert not any(b.get("type") == "text" and not b.get("text", "").strip()
                   for b in blocks), "空白文本块不得出网"
    assert any(b.get("type") == "image" for b in blocks), "图片必须保留"


def test_openai_whitespace_text_part_next_to_image_dropped():
    out = oai("", [LLMMessage(role="user", content=[TextPart(text="   "), _img()])])
    parts = out[0]["content"]
    assert not any(p.get("type") == "text" and not p.get("text", "").strip()
                   for p in parts), "空白文本块不得出网"
    assert any(p.get("type") == "image_url" for p in parts), "图片必须保留"


def test_anthropic_meaningful_text_with_leading_space_preserved():
    """收紧的是「纯空白」，不是「带空白」——有实义的文本一字不改。"""
    out = anth([LLMMessage(role="user", content=[TextPart(text="  hi  "), _img()])])
    texts = [b["text"] for b in out[0]["content"] if b.get("type") == "text"]
    assert texts == ["  hi  "], "有实义的文本必须原样保留，含首尾空白"


# ── I1（评审 2026-08-24 fix wave）：`.strip()` 收紧对 text=None 不再容忍 ────────
#
# 原 `if text:` 对 text=None 是容忍的（跳过该 part）；I1 之前的 `.strip()` 收紧对
# None 直接 AttributeError。
#
# Phase 3c Task E 后两家 adapter 的 dict 分支已删（dict 是协议违规，已在
# ``MemoryRecord.__post_init__`` 边界归一），dict part 因此落到「无 .text 属性」
# 的最后一支被跳过——**外部可见行为不变**，这两条仍钉住「adapter 不得因 dict
# 形态输入抛未捕获异常」这一契约。


def test_anthropic_dict_part_with_none_text_does_not_raise():
    """dict part（如 JSON 往返产出的畸形/容忍输入）不得让 adapter 内部抛未捕获的
    AttributeError——应像原 `if text:` 语义一样跳过该 part。"""
    out = anth([LLMMessage(role="user", content=[{"type": "text", "text": None}])])
    assert out[0]["content"] == [], "text=None 的 part 应被跳过，不产出任何 block"


def test_openai_dict_part_with_none_text_does_not_raise():
    out = oai("", [LLMMessage(role="user", content=[{"type": "text", "text": None}])])
    assert out[0]["content"] == [], "text=None 的 part 应被跳过，不产出任何 block"


def test_anthropic_dataclass_part_without_text_attr_does_not_leak_repr():
    """dataclass 分支的兜底不能改成 `or str(p)`——那会把无 .text 属性的对象整个 repr
    当文本泄漏进发给模型的文本（Phase 2 修过的同类泄漏）。用一个没有 .text 属性、
    也不是 ImagePart 的裸对象验证：不抛，且不产出带 repr 内容的 text block。"""
    class _Weird:
        type = "something_else"

    out = anth([LLMMessage(role="user", content=[_Weird()])])
    for block in out[0]["content"]:
        if block.get("type") == "text":
            assert "_Weird" not in block["text"] and "object at 0x" not in block["text"]


# ── I2（评审 2026-08-24 fix wave）：纯空白 tool result 的 str/parts 形态需一致 ──
#
# 修复前：tool + parts [TextPart("   ")] → content: []（Anthropic 大概率因空数组
# 400）；tool + str "   " → content: "   "（同一份"空白工具输出"两种形态产出不一致
# 的 wire）。查证：Anthropic 对 tool_result.content 为空字符串/空数组同样拒绝
# （与其对纯空/纯空白 text block 的拒绝同源，"text content blocks must be
# non-empty" 系列报告一致），故不能兜底成 "" 或 []，必须是非空占位 block。


def test_anthropic_tool_result_whitespace_parts_not_empty_list():
    out = anth([LLMMessage(role="tool", content=[TextPart(text="   ")], tool_call_id="t1")])
    tr = out[0]["content"][0]
    assert tr == {
        "type": "tool_result", "tool_use_id": "t1",
        "content": [{"type": "text", "text": "(empty)"}],
    }, "纯空白 parts 形态的 tool result 不得产出空 content 列表"


def test_anthropic_tool_result_whitespace_str_matches_parts_form():
    """str 形态与 parts 形态的纯空白工具输出必须产出一致的占位内容——不是各自兜底
    出不同的 wire 形态。"""
    out_str = anth([LLMMessage(role="tool", content="   ", tool_call_id="t1")])
    out_parts = anth([LLMMessage(role="tool", content=[TextPart(text="   ")], tool_call_id="t1")])
    assert out_str[0]["content"][0]["content"] == out_parts[0]["content"][0]["content"]


def test_anthropic_tool_result_nonblank_str_unchanged():
    """非空白 str 工具结果的 wire 形态必须逐字节不变（纯文本行为不受 I2 影响）。"""
    out = anth([LLMMessage(role="tool", content="result", tool_call_id="tc1")])
    assert out[0]["content"][0]["content"] == "result"
