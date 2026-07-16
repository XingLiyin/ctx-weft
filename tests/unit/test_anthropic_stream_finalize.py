"""Anthropic adapter streaming finalize behavior (fake SSE)."""

import json

import pytest

from ctx_weft.protocols import LLMCallError, LLMMessage, LLMRequest
from ctx_weft.providers.llm.anthropic import AnthropicAdapter


class _FakeResp:
    def __init__(self, lines, status=200):
        self._lines = lines
        self.status_code = status

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln

    async def aread(self):
        return b"err"


class _FakeStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, lines):
        self._lines = lines

    def stream(self, *a, **k):
        return _FakeStreamCtx(_FakeResp(self._lines))


def _data(obj) -> str:
    return "data: " + json.dumps(obj)


def _adapter(lines):
    a = AnthropicAdapter(api_key="k")
    a._client = _FakeClient(lines)
    return a


async def _collect(adapter):
    req = LLMRequest(model="m", system="", messages=[LLMMessage(role="user", content="hi")])
    return [c async for c in adapter.complete(req, stream=True)]


async def test_native_tool_call_normal():
    lines = [
        _data({"type": "message_start", "message": {"usage": {"input_tokens": 7}}}),
        _data({"type": "content_block_start", "index": 0,
               "content_block": {"type": "tool_use", "id": "t1", "name": "read"}}),
        _data({"type": "content_block_delta", "index": 0,
               "delta": {"type": "input_json_delta", "partial_json": "{\"p\": \"/a\"}"}}),
        _data({"type": "message_delta", "delta": {"stop_reason": "tool_use"},
               "usage": {"output_tokens": 3}}),
    ]
    chunks = await _collect(_adapter(lines))
    tcs = [c for c in chunks if c.kind == "tool_call"]
    assert len(tcs) == 1
    assert tcs[0].tool_call.name == "read"
    assert tcs[0].tool_call.arguments == {"p": "/a"}
    usage = [c for c in chunks if c.kind == "usage"]
    assert usage and usage[0].usage.prompt_tokens == 7


async def test_tool_call_streaming_emits_partial_heartbeat():
    # input_json_delta 流式累积期间发 tool_call_partial 心跳；最终完整 tool_call 仍在收尾产出。
    lines = [
        _data({"type": "message_start", "message": {"usage": {"input_tokens": 7}}}),
        _data({"type": "content_block_start", "index": 0,
               "content_block": {"type": "tool_use", "id": "t1", "name": "read"}}),
        _data({"type": "content_block_delta", "index": 0,
               "delta": {"type": "input_json_delta", "partial_json": "{\"p\":"}}),
        _data({"type": "content_block_delta", "index": 0,
               "delta": {"type": "input_json_delta", "partial_json": " \"/a\"}"}}),
        _data({"type": "message_delta", "delta": {"stop_reason": "tool_use"},
               "usage": {"output_tokens": 3}}),
    ]
    chunks = await _collect(_adapter(lines))
    assert any(c.kind == "tool_call_partial" for c in chunks)
    tcs = [c for c in chunks if c.kind == "tool_call"]
    assert len(tcs) == 1
    assert tcs[0].tool_call.arguments == {"p": "/a"}


async def test_truncated_tool_call_raises_retriable():
    # tool_use started + partial json, but no message_delta before stream ends.
    lines = [
        _data({"type": "message_start", "message": {"usage": {"input_tokens": 7}}}),
        _data({"type": "content_block_start", "index": 0,
               "content_block": {"type": "tool_use", "id": "t1", "name": "read"}}),
        _data({"type": "content_block_delta", "index": 0,
               "delta": {"type": "input_json_delta", "partial_json": "{\"p\":"}}),
    ]
    with pytest.raises(LLMCallError) as exc:
        await _collect(_adapter(lines))
    assert exc.value.retriable is True


async def test_text_embedded_tool_call_recovered():
    payload = '<tool_call>{"name": "write", "arguments": {"p": "/a"}}</tool_call>'
    lines = [
        _data({"type": "content_block_delta", "index": 0,
               "delta": {"type": "text_delta", "text": "ok" + payload}}),
        _data({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
               "usage": {"output_tokens": 3}}),
    ]
    chunks = await _collect(_adapter(lines))
    tcs = [c for c in chunks if c.kind == "tool_call"]
    assert len(tcs) == 1
    assert tcs[0].tool_call.name == "write"


async def test_inline_think_emits_reasoning():
    lines = [
        _data({"type": "content_block_delta", "index": 0,
               "delta": {"type": "text_delta", "text": "<think>reasoning</think>answer"}}),
        _data({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
               "usage": {"output_tokens": 3}}),
    ]
    chunks = await _collect(_adapter(lines))
    assert any(c.kind == "reasoning" and "reasoning" in c.text for c in chunks)


async def test_empty_stream_raises_retriable():
    with pytest.raises(LLMCallError) as exc:
        await _collect(_adapter([]))
    assert exc.value.retriable is True


async def test_plain_text_with_message_delta_does_not_raise():
    lines = [
        _data({"type": "content_block_delta", "index": 0,
               "delta": {"type": "text_delta", "text": "just an answer"}}),
        _data({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
               "usage": {"output_tokens": 3}}),
    ]
    chunks = await _collect(_adapter(lines))
    assert [c for c in chunks if c.kind == "tool_call"] == []
    assert any(c.kind == "done" for c in chunks)


async def test_usage_cache_split_normalized():
    # Anthropic 的 input_tokens 不含缓存部分：prompt 归一为三者之和，input 显式记原值。
    lines = [
        _data({"type": "message_start", "message": {"usage": {
            "input_tokens": 7, "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 20}}}),
        _data({"type": "content_block_delta", "index": 0,
               "delta": {"type": "text_delta", "text": "hi"}}),
        _data({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
               "usage": {"output_tokens": 3}}),
    ]
    chunks = await _collect(_adapter(lines))
    u = [c for c in chunks if c.kind == "usage"][0].usage
    assert u.prompt_tokens == 127
    assert u.cache_read_tokens == 100
    assert u.cache_write_tokens == 20
    assert u.input_tokens == 7
    assert u.total_tokens == 130
    assert u.reasoning_tokens == 0


async def test_usage_no_cache_fields_regression():
    # 不带缓存字段的现状流：prompt == input，cache 全 0，与旧行为全等。
    lines = [
        _data({"type": "message_start", "message": {"usage": {"input_tokens": 7}}}),
        _data({"type": "content_block_delta", "index": 0,
               "delta": {"type": "text_delta", "text": "hi"}}),
        _data({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
               "usage": {"output_tokens": 3}}),
    ]
    chunks = await _collect(_adapter(lines))
    u = [c for c in chunks if c.kind == "usage"][0].usage
    assert u.prompt_tokens == 7 and u.input_tokens == 7
    assert u.cache_read_tokens == 0 and u.cache_write_tokens == 0


async def test_message_delta_overrides_input_side():
    # 部分代理在尾包重发输入侧字段 → 以尾包为准。
    lines = [
        _data({"type": "message_start", "message": {"usage": {"input_tokens": 1}}}),
        _data({"type": "content_block_delta", "index": 0,
               "delta": {"type": "text_delta", "text": "hi"}}),
        _data({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
               "usage": {"output_tokens": 3, "input_tokens": 7,
                         "cache_read_input_tokens": 100}}),
    ]
    chunks = await _collect(_adapter(lines))
    u = [c for c in chunks if c.kind == "usage"][0].usage
    assert u.prompt_tokens == 107
    assert u.input_tokens == 7
    assert u.cache_read_tokens == 100
