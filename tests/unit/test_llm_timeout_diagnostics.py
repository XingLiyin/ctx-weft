"""Report the timeout sent over HTTP, including SDK and midstream failures."""

import json
import logging
import ssl

import httpx
import pytest

from ctx_weft.protocols import LLMCallError, LLMRequest
from ctx_weft.providers.llm import LLMAccount, LLMProvider, ModelConfig, ModelLimits
from ctx_weft.providers.llm.adapters._errors import transport_error
from ctx_weft.providers.llm.adapters.anthropic import AnthropicAdapter
from ctx_weft.providers.llm.adapters.openai import OpenAIAdapter
from ctx_weft.providers.llm.adapters.openai_compatible import OpenAICompatibleAdapter

_SECRET = "private-key https://private.example/?token=secret private-prompt"


@pytest.mark.parametrize("fault", [
    httpx.ReadTimeout, httpx.WriteTimeout, httpx.ConnectTimeout,
    httpx.PoolTimeout, httpx.TimeoutException,
])
@pytest.mark.parametrize("configured", [False, True])
def test_timeout_preserves_message_and_logs_type_and_configuration(
    caplog, fault, configured,
):
    exc = fault(_SECRET)
    timeout = httpx.Timeout(60, connect=10, write=25, pool=5) if configured else None

    error = transport_error("OpenAI", exc, produced=False, timeout=timeout)

    assert str(error) == _SECRET
    assert fault.__name__ in caplog.text
    assert error.retriable and error.outage
    assert _SECRET in str(error)
    assert _SECRET in caplog.text
    if configured:
        assert "read_timeout_sec=60" in caplog.text
        assert "connect_timeout_sec=10" in caplog.text
        assert "write_timeout_sec=25" in caplog.text
        assert "pool_timeout_sec=5" in caplog.text
    else:
        assert "_timeout_sec=" not in str(error)


def test_read_failure_is_not_described_as_a_timeout(caplog):
    error = transport_error(
        "OpenAI", httpx.ReadError(_SECRET), produced=True, timeout=httpx.Timeout(60),
    )
    assert str(error) == _SECRET
    assert "httpx.ReadError" in caplog.text
    assert error.retriable and error.outage


def _adapter(protocol, handler, timeout_sec):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5)
    if protocol == "openai-official":
        return OpenAIAdapter(_SECRET, http_client=client, timeout_sec=timeout_sec)
    base = AnthropicAdapter if protocol == "anthropic" else OpenAICompatibleAdapter

    class Offline(base):
        def _make_client(self):
            return client

    return Offline(_SECRET, timeout_sec=timeout_sec)


@pytest.mark.parametrize("protocol", ["openai-compatible", "anthropic", "openai-official"])
@pytest.mark.parametrize("timeout_sec", [60, 120])
@pytest.mark.parametrize("midstream", [False, True])
async def test_failure_logs_report_the_actual_request_timeout(caplog, protocol, timeout_sec, midstream):
    seen = []
    event = (
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "partial"}}
        if protocol == "anthropic" else
        {"id": "test", "object": "chat.completion.chunk", "created": 0, "model": "m",
         "choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}]}
    )

    def fail():
        try:
            raise ssl.SSLWantReadError(_SECRET)
        except ssl.SSLError:
            raise httpx.ReadTimeout(_SECRET) from None

    class BrokenBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield ("data: " + json.dumps(event) + "\n\n").encode()
            fail()

    def handler(request):
        seen.append(request.extensions["timeout"])
        if not midstream:
            fail()
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=BrokenBody())

    adapter = _adapter(protocol, handler, timeout_sec)
    try:
        with caplog.at_level(logging.WARNING), pytest.raises(LLMCallError) as caught:
            async for _ in adapter.complete(LLMRequest("m", "", [])):
                pass
        assert seen == [{name: timeout_sec for name in ("connect", "read", "write", "pool")}]
        assert caught.value.retriable and caught.value.outage
        assert str(caught.value) == _SECRET
        assert "httpx.ReadTimeout" in caplog.text
        assert f"stream_started={midstream}" in caplog.text
        for field, value in seen[0].items():
            assert f"{field}_timeout_sec={value}" in caplog.text
        assert "certificate" not in str(caught.value)
        assert _SECRET in caplog.text
        assert _SECRET in str(caught.value)
    finally:
        if protocol == "openai-official":
            await adapter.aclose()
        else:
            await adapter._client.aclose()


async def test_sdk_timeout_override_is_logged_instead_of_http_client_default(caplog):
    seen = []

    def handler(request):
        seen.append(request.extensions["timeout"])
        raise httpx.ReadTimeout(_SECRET)

    adapter = _adapter("openai-official", handler, 120)
    adapter._sdk.timeout = httpx.Timeout(61, connect=7, write=23, pool=4)
    try:
        with pytest.raises(LLMCallError) as caught:
            async for _ in adapter.complete(LLMRequest("m", "", [])):
                pass
        assert seen == [{"connect": 7, "read": 61, "write": 23, "pool": 4}]
        for field, value in seen[0].items():
            assert f"{field}_timeout_sec={value}" in caplog.text
        assert str(caught.value) == _SECRET
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("fault", [
    httpx.ReadTimeout, httpx.WriteTimeout, httpx.ConnectTimeout, httpx.PoolTimeout,
])
def test_empty_timeout_message_uses_original_exception_type(fault):
    error = transport_error("OpenAI", fault(""), produced=False)
    assert str(error) == fault.__name__
    assert error.retriable and error.outage


def test_registered_account_logs_its_configured_timeout_without_credentials(caplog):
    class Store:
        def save(self, account):
            pass

    provider = LLMProvider(Store())
    account = LLMAccount(
        name="gateway", api_key=_SECRET, base_url="https://private.example/",
        models=[ModelConfig("m", "openai", ModelLimits(128000))], timeout_sec=60,
    )
    with caplog.at_level(logging.INFO):
        provider.register_account(account)
    assert "account 'gateway' timeout_sec=60" in caplog.text
    for secret in ["private-key", "private.example", "token=secret", "private-prompt"]:
        assert secret not in caplog.text
