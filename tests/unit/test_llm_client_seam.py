import httpx

from loomex_core.providers.llm.anthropic import AnthropicAdapter
from loomex_core.providers.llm.openai import OpenAIAdapter


def test_openai_make_client_returns_async_client():
    a = OpenAIAdapter(api_key="k")
    assert isinstance(a._make_client(), httpx.AsyncClient)


def test_anthropic_make_client_returns_async_client():
    a = AnthropicAdapter(api_key="k")
    assert isinstance(a._make_client(), httpx.AsyncClient)
