"""LLMClient.tokenizer 硬契约：三 adapter + _FixedModelClient 符合性；count_tokens 已删。"""
from __future__ import annotations

from ctx_weft.protocols.llm import LLMClient, Tokenizer
from ctx_weft.providers.llm.anthropic import AnthropicAdapter
from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
from ctx_weft.providers.llm.openai import OpenAIAdapter
from ctx_weft.providers.llm.provider import _FixedModelClient


def test_adapters_expose_tokenizer():
    for client in (
        OpenAIAdapter(api_key="k", model="m"),
        AnthropicAdapter(api_key="k", model="m"),
        MockLLMAdapter(responses=[]),
    ):
        assert isinstance(client.tokenizer, Tokenizer)


def test_tokenizer_per_model_isolated():
    adapter = OpenAIAdapter(api_key="k", model="m1")
    t1 = adapter.tokenizer_for("m1")
    t2 = adapter.tokenizer_for("m2")
    assert t1 is not t2
    assert adapter.tokenizer_for("m1") is t1        # 同 model 稳定同一实例
    t1.observe(1000, 2000)
    assert t1.factor != t2.factor                   # 各学各的


def test_fixed_model_client_binds_model_tokenizer():
    adapter = MockLLMAdapter(responses=[])
    client = _FixedModelClient(adapter, "mx", context_limit=100_000, output_reserve=4096)
    assert client.tokenizer is adapter.tokenizer_for("mx")


def test_count_tokens_removed_from_protocol():
    assert not hasattr(LLMClient, "count_tokens")


async def test_mock_usage_via_own_tokenizer():
    adapter = MockLLMAdapter(responses=[MockResponse(text="ok")])
    from ctx_weft.protocols import LLMMessage, LLMRequest
    req = LLMRequest(model="mock", system="SYS",
                     messages=[LLMMessage(role="user", content="hello world")])
    usage = None
    async for ch in adapter.complete(req):
        if ch.kind == "usage":
            usage = ch.usage
    expected = adapter.tokenizer.count("SYS" + "\n".join(["hello world"]))
    assert usage.prompt_tokens == expected
