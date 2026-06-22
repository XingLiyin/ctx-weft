"""Mid-stream failures must raise retriable (not retry internally → no dup)."""

import json

import httpx
import pytest

from ctx_weft.protocols import LLMCallError, LLMMessage, LLMRequest
from ctx_weft.providers.llm.openai import OpenAIAdapter
from ctx_weft.providers.llm.anthropic import AnthropicAdapter


class _RaiseAfterResp:
    """Yields some lines, then raises mid-stream (after content was emitted)."""

    def __init__(self, lines, exc):
        self._lines = lines
        self._exc = exc
        self.status_code = 200

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln
        raise self._exc

    async def aread(self):
        return b"err"


class _Ctx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class _CountingClient:
    """Counts how many times .stream() is called (== HTTP attempts)."""

    def __init__(self, lines, exc):
        self._lines = lines
        self._exc = exc
        self.calls = 0

    def stream(self, *a, **k):
        self.calls += 1
        return _Ctx(_RaiseAfterResp(self._lines, self._exc))


async def _drive(adapter):
    req = LLMRequest(model="m", system="", messages=[LLMMessage(role="user", content="hi")])
    out = []
    async for c in adapter.complete(req, stream=True):
        out.append(c)
    return out


async def test_openai_midstream_failure_raises_retriable_without_internal_retry():
    line = "data: " + json.dumps({"choices": [{"delta": {"content": "hello"}, "finish_reason": None}]})
    client = _CountingClient([line], httpx.ReadTimeout("boom"))
    a = OpenAIAdapter(api_key="k", max_http_retries=3)
    a._client = client
    with pytest.raises(LLMCallError) as exc:
        await _drive(a)
    assert exc.value.retriable is True
    # produced a chunk already → must NOT silently retry the whole request.
    assert client.calls == 1


async def test_anthropic_midstream_failure_raises_retriable_without_internal_retry():
    line = "data: " + json.dumps({
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "text_delta", "text": "hello"},
    })
    client = _CountingClient([line], httpx.ReadTimeout("boom"))
    a = AnthropicAdapter(api_key="k", max_http_retries=3)
    a._client = client
    with pytest.raises(LLMCallError) as exc:
        await _drive(a)
    assert exc.value.retriable is True
    assert client.calls == 1


async def test_openai_midtoolcall_failure_raises_retriable_without_internal_retry():
    # 工具调用参数流式中途断流（无可见文本）：现在 produced 计入工具调用进度，
    # 必须走 retriable（任务层干净整跑），而非静默 inline 重发整个长工具调用。
    line = "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "t1", "function": {"name": "read", "arguments": "{\"p\":"}}]},
        "finish_reason": None}]})
    client = _CountingClient([line], httpx.ReadTimeout("boom"))
    a = OpenAIAdapter(api_key="k", max_http_retries=3)
    a._client = client
    with pytest.raises(LLMCallError) as exc:
        await _drive(a)
    assert exc.value.retriable is True
    assert client.calls == 1


async def test_anthropic_midtoolcall_failure_raises_retriable_without_internal_retry():
    lines = [
        "data: " + json.dumps({"type": "content_block_start", "index": 0,
                               "content_block": {"type": "tool_use", "id": "t1", "name": "read"}}),
        "data: " + json.dumps({"type": "content_block_delta", "index": 0,
                               "delta": {"type": "input_json_delta", "partial_json": "{\"p\":"}}),
    ]
    client = _CountingClient(lines, httpx.ReadTimeout("boom"))
    a = AnthropicAdapter(api_key="k", max_http_retries=3)
    a._client = client
    with pytest.raises(LLMCallError) as exc:
        await _drive(a)
    assert exc.value.retriable is True
    assert client.calls == 1
