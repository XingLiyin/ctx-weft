from dataclasses import asdict

import httpx
import pytest

from ctx_weft.providers.llm import (
    LLMAccount,
    LLMProvider,
    ModelCapabilities,
    ModelConfig,
    ModelLimits,
)


class Store:
    def save(self, account):
        self.saved = asdict(account)

    def delete(self, name):
        return True

    def list_all(self):
        return []


def model(name="m", protocol="openai", image=False):
    return ModelConfig(
        name,
        protocol,
        ModelLimits(200_000, 32_000),
        ModelCapabilities(["text", "image"] if image else ["text"]),
        0,
    )


def provider(*models):
    p = LLMProvider(Store())
    p.register_account(LLMAccount("gateway", "key", "https://gateway.test/prefix", list(models)))
    return p


@pytest.mark.parametrize(
    "protocol,image,expected",
    [
        ("openai", False, "OpenAIAdapter"),
        ("openai", True, "OpenAIMultimodalAdapter"),
        ("anthropic", False, "AnthropicAdapter"),
        ("anthropic", True, "AnthropicMultimodalAdapter"),
    ],
)
def test_dispatch_is_per_model(protocol, image, expected):
    p = provider(model(), model("other", protocol, image))
    client = p.get_client("gateway", "other")
    assert type(client._adapter).__name__ == expected
    assert client.context_limit == 200_000
    assert client.output_ceiling == 32_000
    assert client.output_reserve == 0
    assert (client.account, client.model) == ("gateway", "other")


def test_cache_reuses_type_but_keeps_model_tokenizers_separate():
    p = provider(model("a"), model("b"), model("c", "anthropic"))
    a, b, c = (p.get_client("gateway", name) for name in ("a", "b", "c"))
    assert a._adapter is b._adapter
    assert a.tokenizer is not b.tokenizer
    assert a._adapter is not c._adapter
    p.get_account("gateway").api_key = "replacement"
    assert p.get_client("gateway", "a")._adapter is not a._adapter


def test_config_query_is_lazy_and_unknown_model_is_rejected():
    p = provider(model())
    assert p.get_model_config().name == "m"
    assert not p._adapters
    with pytest.raises(ValueError, match="not in account"):
        p.get_client(model="missing")


def test_update_changes_next_client_and_preserves_previous_client():
    p = provider(model())
    old = p.get_client()
    p.update_model("gateway", model(protocol="anthropic", image=True))
    assert type(p.get_client()._adapter).__name__ == "AnthropicMultimodalAdapter"
    assert type(old._adapter).__name__ == "OpenAIAdapter"
    assert p._store.saved["models"][0]["limit"]["output"] == 32_000
    p.delete_account("gateway")
    assert not p._adapters and not p._adapter_settings


@pytest.mark.asyncio
async def test_candidate_probe_before_registration_uses_candidate_protocol():
    p = provider(model())
    candidate = model("candidate", "anthropic", True)
    adapter = p._get_adapter(p.get_account("gateway"), candidate)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            text='event: message_stop\ndata: {"type": "message_stop"}\n\n'
            'event: message_delta\ndata: {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}\n\n',
        )

    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await p.verify_model_for_account("gateway", candidate)
    finally:
        await adapter._client.aclose()
    assert str(seen[0].url).endswith("/prefix/v1/messages")
    assert seen[0].headers["x-api-key"] == "key"
    assert [m.name for m in p.get_account("gateway").models] == ["m"]
