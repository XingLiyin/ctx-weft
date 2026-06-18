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
