"""OpenAI adapter streaming finalize behavior (fake SSE)."""

import json

import pytest

from ctx_weft.protocols import LLMCallError, LLMMessage, LLMRequest, LLMTool
from ctx_weft.providers.llm.openai import OpenAIAdapter


# ── Fake httpx streaming client ────────────────────────────────────────────────


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
    def __init__(self, lines, status=200):
        self._lines = lines
        self._status = status

    def stream(self, *a, **k):
        return _FakeStreamCtx(_FakeResp(self._lines, self._status))


def _data(obj) -> str:
    return "data: " + json.dumps(obj)


def _delta(delta, finish_reason=None):
    return _data({"choices": [{"delta": delta, "finish_reason": finish_reason}]})


def _adapter(lines):
    a = OpenAIAdapter(api_key="k")
    a._client = _FakeClient(lines)
    return a


async def _collect(adapter):
    req = LLMRequest(model="m", system="", messages=[LLMMessage(role="user", content="hi")])
    return [c async for c in adapter.complete(req, stream=True)]


# ── Tests ──────────────────────────────────────────────────────────────────────


async def test_native_tool_call_normal():
    lines = [
        _delta({"tool_calls": [{"index": 0, "id": "t1",
                                "function": {"name": "read", "arguments": "{\"p\": \"/a\"}"}}]}),
        _delta({}, finish_reason="tool_calls"),
        "data: [DONE]",
    ]
    chunks = await _collect(_adapter(lines))
    tcs = [c for c in chunks if c.kind == "tool_call"]
    assert len(tcs) == 1
    assert tcs[0].tool_call.name == "read"
    assert tcs[0].tool_call.arguments == {"p": "/a"}


async def test_usage_after_finish_reason_is_captured():
    # OpenAI sends usage in a trailing choices=[] chunk AFTER finish_reason.
    lines = [
        _delta({"content": "hello"}, finish_reason="stop"),
        _data({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 3,
                                        "total_tokens": 13}}),
        "data: [DONE]",
    ]
    chunks = await _collect(_adapter(lines))
    usage = [c for c in chunks if c.kind == "usage"]
    assert len(usage) == 1
    assert usage[0].usage.prompt_tokens == 10


async def test_truncated_tool_call_raises_retriable():
    # tool_calls delta started, then stream ends with no finish_reason / no [DONE].
    lines = [
        _delta({"tool_calls": [{"index": 0, "id": "t1",
                                "function": {"name": "read", "arguments": "{\"p\":"}}]}),
    ]
    with pytest.raises(LLMCallError) as exc:
        await _collect(_adapter(lines))
    assert exc.value.retriable is True


async def test_text_embedded_tool_call_recovered():
    payload = '<tool_call>{"name": "write", "arguments": {"p": "/a"}}</tool_call>'
    lines = [
        _delta({"content": "ok" + payload}),
        _delta({}, finish_reason="stop"),
        "data: [DONE]",
    ]
    chunks = await _collect(_adapter(lines))
    tcs = [c for c in chunks if c.kind == "tool_call"]
    assert len(tcs) == 1
    assert tcs[0].tool_call.name == "write"


async def test_malformed_tool_call_delta_is_filtered():
    # null id + empty name → dropped; terminal seen so no raise; no tool_call emitted.
    lines = [
        _delta({"tool_calls": [{"index": 0, "id": None,
                                "function": {"name": "", "arguments": ""}}]}),
        _delta({}, finish_reason="stop"),
        "data: [DONE]",
    ]
    chunks = await _collect(_adapter(lines))
    assert [c for c in chunks if c.kind == "tool_call"] == []
    assert any(c.kind == "done" for c in chunks)


async def test_inline_think_emits_reasoning():
    lines = [
        _delta({"content": "<think>reasoning</think>answer"}),
        _delta({}, finish_reason="stop"),
        "data: [DONE]",
    ]
    chunks = await _collect(_adapter(lines))
    reasoning = [c for c in chunks if c.kind == "reasoning"]
    assert any("reasoning" in c.text for c in reasoning)


async def test_empty_stream_raises_retriable():
    with pytest.raises(LLMCallError) as exc:
        await _collect(_adapter([]))
    assert exc.value.retriable is True


def test_payload_omits_tool_choice_auto_by_default():
    a = OpenAIAdapter(api_key="k")
    req = LLMRequest(
        model="m", system="", messages=[LLMMessage(role="user", content="hi")],
        tools=[LLMTool(name="read", description="", input_schema={"type": "object"})],
    )
    payload = a._build_payload(req)
    assert "tools" in payload
    assert "tool_choice" not in payload
