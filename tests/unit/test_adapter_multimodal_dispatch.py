"""内置 adapter 的模态分流：类型即声明（spec 2026-08-28-multimodal-adapter-dispatch）。

纯文本 adapter 把图降级成占位 + warning；多模态子类原样透传。
"""

from __future__ import annotations

import base64
import logging

import pytest

from ctx_weft.protocols import ImagePart, LLMMessage, LLMRequest, TextPart

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
_PNG_B64 = base64.b64encode(_PNG_BYTES).decode()


def _img(media_type: str = "image/png") -> ImagePart:
    return ImagePart(data=_PNG_B64, media_type=media_type)


def _req(*messages: LLMMessage) -> LLMRequest:
    return LLMRequest(model="m-1", system="", messages=list(messages))


# ── Task 1：降级辅助 ──────────────────────────────────────────────────────


def test_downgrade_replaces_image_with_deterministic_placeholder():
    from ctx_weft.providers.llm._modality import downgrade_for_text_only

    req = _req(LLMMessage(role="user", content=[TextPart(text="看图"), _img()]))
    out = downgrade_for_text_only(req, adapter_hint="AnthropicMultimodalAdapter")

    assert [p.text for p in out[0].content] == ["看图", "[image image/png]"]


def test_downgrade_is_identity_for_text_only_messages():
    """纯文本路径逐字节不变：返回同一个 list 对象，不重建。"""
    from ctx_weft.providers.llm._modality import downgrade_for_text_only

    req = _req(LLMMessage(role="user", content="纯文本"))
    assert downgrade_for_text_only(req, adapter_hint="X") is req.messages


def test_downgrade_warns_once_with_model_count_and_fix_hint(caplog):
    from ctx_weft.providers.llm._modality import downgrade_for_text_only

    req = _req(
        LLMMessage(role="user", content=[_img(), _img()]),
        LLMMessage(role="tool", content=[_img()], tool_call_id="tc1"),
    )
    with caplog.at_level(logging.WARNING):
        downgrade_for_text_only(req, adapter_hint="OpenAIMultimodalAdapter")

    records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(records) == 1, "每次请求最多一条 warning"
    msg = records[0].getMessage()
    assert "m-1" in msg and "3" in msg and "OpenAIMultimodalAdapter" in msg


def test_downgrade_emits_no_log_for_text_only(caplog):
    from ctx_weft.providers.llm._modality import downgrade_for_text_only

    req = _req(LLMMessage(role="user", content="纯文本"))
    with caplog.at_level(logging.WARNING):
        downgrade_for_text_only(req, adapter_hint="X")
    assert caplog.records == []


def test_downgrade_does_not_mutate_input_messages():
    from ctx_weft.providers.llm._modality import downgrade_for_text_only

    original = LLMMessage(role="user", content=[_img()])
    req = _req(original)
    downgrade_for_text_only(req, adapter_hint="X")
    assert isinstance(original.content[0], ImagePart), "入参消息不得被就地改写"


# ── Task 2：Anthropic 分流 ────────────────────────────────────────────────


def _anth(multimodal: bool):
    from ctx_weft.providers.llm.anthropic import (
        AnthropicAdapter,
        AnthropicMultimodalAdapter,
    )
    cls = AnthropicMultimodalAdapter if multimodal else AnthropicAdapter
    return cls(api_key="k")


def test_anthropic_text_only_downgrades_user_image():
    payload = _anth(multimodal=False)._build_payload(
        _req(LLMMessage(role="user", content=[TextPart(text="看图"), _img()])))
    blocks = payload["messages"][0]["content"]
    assert [b["type"] for b in blocks] == ["text", "text"]
    assert blocks[1]["text"] == "[image image/png]"


def test_anthropic_multimodal_keeps_user_image():
    payload = _anth(multimodal=True)._build_payload(
        _req(LLMMessage(role="user", content=[TextPart(text="看图"), _img()])))
    blocks = payload["messages"][0]["content"]
    assert [b["type"] for b in blocks] == ["text", "image"]
    assert blocks[1]["source"]["data"] == _PNG_B64


def test_anthropic_text_only_downgrades_assistant_image():
    payload = _anth(multimodal=False)._build_payload(
        _req(LLMMessage(role="assistant", content=[_img()])))
    blocks = payload["messages"][0]["content"]
    assert [b["type"] for b in blocks] == ["text"]


def test_anthropic_text_only_downgrades_tool_result_image():
    payload = _anth(multimodal=False)._build_payload(_req(
        LLMMessage(role="user", content="q"),
        LLMMessage(role="tool", content=[_img()], tool_call_id="tc1"),
    ))
    tool_msg = payload["messages"][-1]["content"][0]
    assert [b["type"] for b in tool_msg["content"]] == ["text"]


def test_anthropic_multimodal_keeps_tool_result_image():
    payload = _anth(multimodal=True)._build_payload(_req(
        LLMMessage(role="user", content="q"),
        LLMMessage(role="tool", content=[_img()], tool_call_id="tc1"),
    ))
    tool_msg = payload["messages"][-1]["content"][0]
    assert [b["type"] for b in tool_msg["content"]] == ["image"]


def test_anthropic_plain_text_payload_unchanged_between_classes():
    """纯文本会话在两个类上产出完全相同的 wire。"""
    req = _req(LLMMessage(role="user", content="纯文本"))
    assert _anth(multimodal=False)._build_payload(req) == \
        _anth(multimodal=True)._build_payload(req)
