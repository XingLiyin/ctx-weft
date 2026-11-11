"""Settings are request-local and translate to each adapter's native payload."""

import asyncio
import json

import pytest

from ctx_weft.protocols import LLMCallError, LLMMessage, LLMRequest, ModelCallConfig
from ctx_weft.providers.llm import AnthropicCallConfig
from ctx_weft.providers.llm.adapters.anthropic import AnthropicAdapter
from ctx_weft.providers.llm.adapters.openai_compatible import OpenAICompatibleAdapter
from tests.unit.test_openai_official import adapter, event, sse


@pytest.mark.asyncio
async def test_official_shared_adapter_keeps_concurrent_configs_separate():
    payloads = []

    async def handler(req):
        payloads.append(json.loads(req.content))
        await asyncio.sleep(0)
        return sse(event({"content": "ok"}), event({}, "stop"))

    client = adapter(handler)
    configs = [ModelCallConfig(reasoning_effort="high", temperature=0.4, top_p=0.7,
                               max_tokens=3000, stop=("END",)), ModelCallConfig()]

    async def run(config):
        return [c async for c in client.complete(LLMRequest("m", "", [], config=config))]

    try:
        await asyncio.gather(*(run(c) for c in configs))
    finally:
        await client.aclose()
    explicit = next(p for p in payloads if "reasoning_effort" in p)
    default = next(p for p in payloads if "reasoning_effort" not in p)
    assert explicit["reasoning_effort"] == "high"
    assert explicit["max_completion_tokens"] == 3000
    assert explicit["temperature"] == 0.4 and explicit["top_p"] == 0.7
    assert explicit["stop"] == ["END"]
    assert not {"temperature", "top_p", "max_completion_tokens", "stop"} & default.keys()


@pytest.mark.asyncio
async def test_compatible_payload_uses_request_config_and_explicit_temperature_one():
    client = OpenAICompatibleAdapter("key")
    try:
        req = LLMRequest("m", "", [], config=ModelCallConfig(
            reasoning_effort="none", temperature=1.0, top_p=0.5, max_tokens=1234, stop=("END",),
        ))
        payload = client._build_payload(req)
        assert {k: payload[k] for k in ("reasoning_effort", "temperature", "top_p", "max_tokens", "stop")} == {
            "reasoning_effort": "none", "temperature": 1.0, "top_p": 0.5,
            "max_tokens": 1234, "stop": ["END"],
        }
        default = client._build_payload(LLMRequest("m", "", []))
        assert not {"reasoning_effort", "temperature", "max_tokens"} & default.keys()
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("config,thinking", [
    (ModelCallConfig(), None),
    (ModelCallConfig(reasoning_effort="none"), {"type": "disabled"}),
    (ModelCallConfig(reasoning_effort="low"), {"type": "enabled", "budget_tokens": 1024}),
    (AnthropicCallConfig(reasoning_effort="high", thinking_budget_tokens=2048),
     {"type": "enabled", "budget_tokens": 2048}),
])
async def test_anthropic_config_and_subclass_budget(config, thinking):
    client = AnthropicAdapter("key")
    try:
        config.max_tokens = 20000
        config.top_p = 0.8
        config.stop = ("END",)
        payload = client._build_payload(LLMRequest("m", "", [], config=config))
        assert payload.get("thinking") == thinking
        assert payload["top_p"] == 0.8 and payload["stop_sequences"] == ["END"]
        assert payload["max_tokens"] == 20000
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("config,messages", [
    (ModelCallConfig(reasoning_effort="unknown"), []),
    (ModelCallConfig(reasoning_effort="high", max_tokens=1000), []),
    (ModelCallConfig(reasoning_effort="low", max_tokens=4000), [LLMMessage("tool", "result", tool_call_id="c")]),
])
async def test_anthropic_invalid_settings_fail_without_retry_or_silent_downgrade(config, messages):
    client = AnthropicAdapter("key")
    try:
        with pytest.raises(LLMCallError) as error:
            client._build_payload(LLMRequest("m", "", messages, config=config))
        assert not error.value.retriable
    finally:
        await client._client.aclose()


def test_builtin_anthropic_config_is_registered_without_importing_http_adapters():
    from ctx_weft.protocols import deserialize_call_config, serialize_call_config

    config = AnthropicCallConfig(thinking_budget_tokens=2048)
    assert deserialize_call_config(serialize_call_config(config)) == config
