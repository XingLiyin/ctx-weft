import httpx

from ctx_weft.providers.llm.adapters.anthropic import AnthropicAdapter
from ctx_weft.providers.llm.adapters.openai_compatible import OpenAICompatibleAdapter


def test_openai_make_client_returns_async_client():
    a = OpenAICompatibleAdapter(api_key="k")
    assert isinstance(a._make_client(), httpx.AsyncClient)


def test_anthropic_make_client_returns_async_client():
    a = AnthropicAdapter(api_key="k")
    assert isinstance(a._make_client(), httpx.AsyncClient)
