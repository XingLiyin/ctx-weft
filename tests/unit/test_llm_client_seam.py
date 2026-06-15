import httpx

from ctx_weft.providers.llm.anthropic import AnthropicAdapter
from ctx_weft.providers.llm.openai import OpenAIAdapter


def test_openai_make_client_returns_async_client():
    a = OpenAIAdapter(api_key="k")
    assert isinstance(a._make_client(), httpx.AsyncClient)


def test_anthropic_make_client_returns_async_client():
    a = AnthropicAdapter(api_key="k")
    assert isinstance(a._make_client(), httpx.AsyncClient)
