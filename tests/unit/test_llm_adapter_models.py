"""adapter.list_models() —— GET {base}/v1/models，解析 data[].id。httpx.MockTransport 假后端。"""
from __future__ import annotations

import httpx
import pytest

from loomex_core.providers.llm.anthropic import AnthropicAdapter
from loomex_core.providers.llm.openai import OpenAIAdapter


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_openai_list_models():
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["auth"] = req.headers.get("authorization", "")
        return httpx.Response(200, json={"object": "list", "data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}]})

    a = OpenAIAdapter(api_key="sk-x", base_url="https://api.openai.com")
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

    a = OpenAIAdapter(api_key="bad", base_url="https://api.openai.com")
    a._client = _mock_client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await a.list_models()
