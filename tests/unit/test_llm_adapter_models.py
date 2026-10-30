"""adapter.list_models() —— GET {base}/v1/models，解析 data[].id。httpx.MockTransport 假后端。"""
from __future__ import annotations

import httpx
import pytest

from ctx_weft.protocols.llm import LLMMessage, LLMRequest
from ctx_weft.providers.llm.adapters.anthropic import AnthropicAdapter
from ctx_weft.providers.llm.adapters.openai_compatible import OpenAICompatibleAdapter


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_openai_list_models():
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["auth"] = req.headers.get("authorization", "")
        return httpx.Response(200, json={"object": "list", "data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}]})

    a = OpenAICompatibleAdapter(api_key="sk-x", base_url="https://api.openai.com")
    a._client = _mock_client(handler)
    assert await a.list_models() == ["gpt-4o", "gpt-4o-mini"]
    assert seen["url"] == "https://api.openai.com/v1/models"
    assert seen["auth"] == "Bearer sk-x"


async def test_anthropic_list_models():
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["key"] = req.headers.get("x-api-key", "")
        seen["ver"] = req.headers.get("anthropic-version", "")
        return httpx.Response(200, json={"data": [{"id": "claude-opus-4-8"}, {"id": "claude-sonnet-4-6"}]})

    a = AnthropicAdapter(api_key="sk-ant", base_url="https://api.anthropic.com")
    a._client = _mock_client(handler)
    assert await a.list_models() == ["claude-opus-4-8", "claude-sonnet-4-6"]
    assert seen["url"] == "https://api.anthropic.com/v1/models"
    assert seen["key"] == "sk-ant"
    assert seen["ver"] == "2023-06-01"


async def test_list_models_raises_on_http_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    a = OpenAICompatibleAdapter(api_key="bad", base_url="https://api.openai.com")
    a._client = _mock_client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await a.list_models()


@pytest.mark.parametrize("adapter_type,operation", [
    (OpenAICompatibleAdapter, "chat/completions"),
    (AnthropicAdapter, "messages"),
])
async def test_subclass_url_policy_applies_to_generation_and_discovery(adapter_type, operation):
    seen = []

    def handler(request):
        seen.append((request.method, str(request.url)))
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "m"}]})
        event = (
            '{"choices":[{"delta":{},"finish_reason":"stop"}]}'
            if operation == "chat/completions"
            else '{"type":"message_delta","delta":{"stop_reason":"end_turn"}}'
        )
        return httpx.Response(200, text=f"data: {event}\n\n")

    class CustomAdapter(adapter_type):
        def _api_url(self, operation):
            return f"{self._base_url}/custom/{operation}"

        def _make_client(self):
            return _mock_client(handler)

    adapter = CustomAdapter(api_key="k", base_url="https://gateway.test")
    try:
        assert await adapter.list_models() == ["m"]
        request = LLMRequest("m", "", [LLMMessage(role="user", content="hi")], max_tokens=32)
        chunks = [chunk async for chunk in adapter.complete(request)]
        assert any(chunk.kind == "done" for chunk in chunks)
    finally:
        await adapter._client.aclose()
    assert seen == [
        ("GET", "https://gateway.test/custom/models"),
        ("POST", f"https://gateway.test/custom/{operation}"),
    ]
