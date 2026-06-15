from loomex_core.providers.llm.anthropic import AnthropicAdapter
from loomex_core.providers.llm.openai import OpenAIAdapter


def test_adapters_hold_retry():
    a = AnthropicAdapter(api_key="x", max_http_retries=5)
    o = OpenAIAdapter(api_key="x", max_http_retries=7)
    assert a._max_http_retries == 5
    assert o._max_http_retries == 7


def test_adapters_retry_default():
    assert AnthropicAdapter(api_key="x")._max_http_retries == 3
    assert OpenAIAdapter(api_key="x")._max_http_retries == 3
