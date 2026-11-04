"""Exercise service errors through real adapters, including SDK and recovery."""

import json
import logging
import ssl
import traceback
from types import SimpleNamespace

import httpx
import pytest

from ctx_weft import RuntimeConfig
from ctx_weft.core.loop.llm_gateway import stream_llm_resilient
from ctx_weft.protocols import LLMCallError, LLMRequest
from ctx_weft.providers.llm.adapters.anthropic import AnthropicAdapter
from ctx_weft.providers.llm.adapters.openai import OpenAIAdapter
from ctx_weft.providers.llm.adapters.openai_compatible import OpenAICompatibleAdapter

_SECRET = "private-key https://private.example/private-route?token=secret private-prompt"
_PROTOCOLS = ["openai-compatible", "anthropic", "openai-official"]


def _adapter(protocol, handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    if protocol == "openai-official":
        return OpenAIAdapter("private-key", base_url="https://private.example/private-route", timeout_sec=60, http_client=client)
    base = AnthropicAdapter if protocol == "anthropic" else OpenAICompatibleAdapter

    class Offline(base):
        def _make_client(self):
            return client

    return Offline("private-key", base_url="https://private.example/private-route", timeout_sec=60)


def _partial(protocol, text="partial"):
    if protocol == "anthropic":
        return {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}
    return {"id": "test", "object": "chat.completion.chunk", "created": 0, "model": "m",
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}


def _terminal(protocol):
    if protocol == "anthropic":
        return {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}
    return {"id": "test", "object": "chat.completion.chunk", "created": 0, "model": "m",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}


def _response(*events):
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          text="".join("data: " + json.dumps(event) + "\n\n" for event in events))


async def _close(adapter):
    if isinstance(adapter, OpenAIAdapter):
        await adapter.aclose()
    else:
        await adapter._client.aclose()


def _assert_message(error, caplog):
    formatted = "".join(traceback.format_exception(error))
    logging.getLogger("ctx_weft.core.runtime").error(
        "synthetic task failed", exc_info=(type(error), error, error.__traceback__),
    )
    assert _SECRET in str(error)
    assert _SECRET in formatted
    assert _SECRET in caplog.text


@pytest.mark.parametrize("protocol", _PROTOCOLS)
@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("field", ["code", "type"])
@pytest.mark.parametrize(("kind", "retriable"), [
    ("overloaded_error", True),
    ("rate_limit_error", True),
    ("rate_limit_exceeded", True),
    ("api_error", True),
    ("server_error", True),
    ("timeout_error", True),
    ("authentication_error", False),
    ("permission_error", False),
    ("invalid_request_error", False),
    ("context_length_exceeded", False),
    ("insufficient_quota", False),
    ("custom_gateway_error", False),
])
async def test_sse_errors_preserve_message_and_do_not_complete(
    caplog, protocol, partial, field, kind, retriable,
):
    error = {"type": "error", "error": {field: kind, "message": _SECRET}}
    events = [_partial(protocol), error] if partial else [error]
    adapter = _adapter(protocol, lambda _: _response(*events))
    chunks = []
    try:
        with pytest.raises(LLMCallError) as caught:
            async for chunk in adapter.complete(LLMRequest("m", "", [])):
                chunks.append(chunk)
        assert caught.value.retriable is retriable
        assert caught.value.outage is retriable
        assert caught.value.status_code == 0
        assert not any(chunk.kind == "done" for chunk in chunks)
        assert str(caught.value) == _SECRET
        _assert_message(caught.value, caplog)
    finally:
        await _close(adapter)


@pytest.mark.parametrize("protocol", _PROTOCOLS)
@pytest.mark.parametrize("discovery", [False, True])
@pytest.mark.parametrize(("status", "kind", "retriable"), [
    (401, "authentication_error", False), (503, "api_error", True),
    (529, "overloaded_error", True), (408, "timeout_error", True),
    (429, "insufficient_quota", True),
    (409, "conflict_error", True), (501, "api_error", True),
])
async def test_http_errors_and_discovery_preserve_message_and_classification(
    caplog, protocol, discovery, status, kind, retriable,
):
    adapter = _adapter(protocol, lambda _: httpx.Response(
        status, headers={"retry-after": "2"},
        json={"error": {"type": kind, "message": _SECRET}},
    ))
    try:
        with pytest.raises(LLMCallError) as caught:
            if discovery:
                await adapter.list_models()
            else:
                async for _ in adapter.complete(LLMRequest("m", "", [])):
                    pass
        assert caught.value.status_code == status
        assert str(caught.value) == _SECRET
        assert caught.value.retriable is retriable
        assert caught.value.outage is retriable
        assert caught.value.retry_after_sec == 2
        _assert_message(caught.value, caplog)
    finally:
        await _close(adapter)


@pytest.mark.parametrize("protocol", _PROTOCOLS)
async def test_discovery_timeout_reports_seconds_and_original_message(caplog, protocol):
    def handler(request):
        assert request.extensions["timeout"]["read"] == 60
        raise httpx.ReadTimeout(_SECRET, request=request)

    adapter = _adapter(protocol, handler)
    try:
        with pytest.raises(LLMCallError) as caught:
            await adapter.list_models()
        assert str(caught.value) == _SECRET
        assert "httpx.ReadTimeout" in caplog.text
        assert "read_timeout_sec=60" in caplog.text
        assert caught.value.retriable and caught.value.outage
        _assert_message(caught.value, caplog)
    finally:
        await _close(adapter)


@pytest.mark.parametrize("protocol", _PROTOCOLS)
@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("kind", [
    "overloaded_error", "rate_limit_error", "rate_limit_exceeded",
    "api_error", "server_error", "timeout_error",
])
async def test_transient_sse_errors_retry_and_discard_partial_output(protocol, partial, kind):
    calls = []

    def handler(request):
        calls.append(request.content)
        if len(calls) == 1:
            events = [_partial(protocol)] if partial else []
            return _response(*events, {"type": "error", "error": {"type": kind, "message": _SECRET}})
        return _response(_partial(protocol, "complete"), _terminal(protocol))

    adapter = _adapter(protocol, handler)
    ctx = SimpleNamespace(llm=adapter, cancel_token=None, event_bus=None,
                          config=RuntimeConfig(llm_self_heal_max_attempts=2, llm_self_heal_base_delay_sec=0))
    try:
        async with stream_llm_resilient(ctx, None, LLMRequest("m", "", [])) as stream:
            async for _ in stream:
                pass
            assert stream.get_final_result().text == "complete"
        assert len(calls) == 2
        assert calls[0] == calls[1]
    finally:
        await _close(adapter)


@pytest.mark.parametrize("protocol", _PROTOCOLS)
@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("kind", [
    "authentication_error", "permission_error", "invalid_request_error",
    "context_length_exceeded", "insufficient_quota", "custom_gateway_error",
])
async def test_other_sse_errors_are_not_retried_or_inferred_from_message(protocol, partial, kind):
    message = "overloaded_error rate_limit_error server_error timeout_error\n" + _SECRET
    calls = []

    def handler(request):
        calls.append(request.content)
        events = [_partial(protocol)] if partial else []
        return _response(*events, {"type": "error", "error": {"type": kind, "message": message}})

    adapter = _adapter(protocol, handler)
    ctx = SimpleNamespace(llm=adapter, cancel_token=None, event_bus=None,
                          config=RuntimeConfig(llm_self_heal_max_attempts=2, llm_self_heal_base_delay_sec=0))
    try:
        with pytest.raises(LLMCallError) as caught:
            async with stream_llm_resilient(ctx, None, LLMRequest("m", "", [])) as stream:
                async for _ in stream:
                    pass
        assert not caught.value.retriable and not caught.value.outage
        assert str(caught.value) == message
        assert len(calls) == 1
    finally:
        await _close(adapter)


@pytest.mark.parametrize("protocol", _PROTOCOLS)
async def test_partial_response_without_terminal_cannot_be_success(protocol):
    adapter = _adapter(protocol, lambda _: _response(_partial(protocol)))
    try:
        with pytest.raises(LLMCallError, match="terminal") as caught:
            async for _ in adapter.complete(LLMRequest("m", "", [])):
                pass
        assert caught.value.retriable and caught.value.outage
    finally:
        await _close(adapter)


async def test_anthropic_usage_only_delta_does_not_hide_a_following_error():
    adapter = _adapter("anthropic", lambda _: _response(
        _partial("anthropic"), {"type": "message_delta", "usage": {"output_tokens": 1}},
        {"type": "error", "error": {"type": "overloaded_error", "message": _SECRET}},
    ))
    try:
        with pytest.raises(LLMCallError) as caught:
            async for _ in adapter.complete(LLMRequest("m", "", [])):
                pass
        assert caught.value.retriable and caught.value.outage
        assert str(caught.value) == _SECRET
    finally:
        await _close(adapter)


@pytest.mark.parametrize("protocol", _PROTOCOLS)
@pytest.mark.parametrize("body", ["not JSON private-prompt", '{"data": "private-prompt"}'])
async def test_malformed_model_lists_report_invalid_response(protocol, body):
    adapter = _adapter(protocol, lambda _: httpx.Response(200, text=body))
    try:
        with pytest.raises(LLMCallError, match=r"invalid JSON|invalid model list") as caught:
            await adapter.list_models()
        assert caught.value.retriable is False
    finally:
        await _close(adapter)


@pytest.mark.parametrize("protocol", _PROTOCOLS)
async def test_model_discovery_error_in_http_200_is_not_an_empty_catalog(protocol):
    adapter = _adapter(protocol, lambda _: httpx.Response(
        200, json={"error": {"type": "overloaded_error", "message": _SECRET}},
    ))
    try:
        with pytest.raises(LLMCallError) as caught:
            await adapter.list_models()
        assert not caught.value.retriable and not caught.value.outage
        assert str(caught.value) == _SECRET
    finally:
        await _close(adapter)


async def test_explicit_done_marker_completes_text_without_finish_reason():
    text = "data: " + json.dumps(_partial("openai-compatible")) + "\n\ndata: [DONE]\n\n"
    adapter = _adapter("openai-compatible", lambda _: httpx.Response(200, text=text))
    try:
        chunks = [chunk async for chunk in adapter.complete(LLMRequest("m", "", []))]
        assert chunks[-1].kind == "done"
    finally:
        await _close(adapter)


@pytest.mark.parametrize("protocol", ["openai-compatible", "anthropic"])
async def test_streamed_http_error_keeps_status_when_body_read_fails(caplog, protocol):
    class ErrorBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b""
            raise httpx.ReadTimeout(_SECRET)

    adapter = _adapter(protocol, lambda _: httpx.Response(529, stream=ErrorBody()))
    try:
        with pytest.raises(LLMCallError) as caught:
            async for _ in adapter.complete(LLMRequest("m", "", [])):
                pass
        assert caught.value.status_code == 529
        assert caught.value.retriable and caught.value.outage
        assert "exception_type=ReadTimeout" in caplog.text
        _assert_message(caught.value, caplog)
    finally:
        await _close(adapter)


@pytest.mark.parametrize("protocol", _PROTOCOLS)
@pytest.mark.parametrize("discovery", [False, True])
@pytest.mark.parametrize("json_body", [False, True])
async def test_http_error_message_is_not_rewritten_or_truncated(protocol, discovery, json_body):
    message = "image input not supported; read timeout while receiving request body\n" + "原始错误 " * 5000
    response = (
        httpx.Response(400, json={"error": {"type": "invalid_request_error", "message": message}})
        if json_body else httpx.Response(400, text=message)
    )
    adapter = _adapter(protocol, lambda _: response)
    try:
        with pytest.raises(LLMCallError) as caught:
            if discovery:
                await adapter.list_models()
            else:
                async for _ in adapter.complete(LLMRequest("m", "", [])):
                    pass
        assert str(caught.value) == message
        assert caught.value.status_code == 400
        assert not caught.value.retriable
    finally:
        await _close(adapter)


@pytest.mark.parametrize("protocol", _PROTOCOLS)
@pytest.mark.parametrize(("status", "retry_header", "retriable"), [
    (409, None, True), (501, None, True), (429, None, True),
    (503, "false", False), (400, "true", True), (401, None, False),
])
async def test_http_retry_policy_controls_actual_gateway_requests(protocol, status, retry_header, retriable):
    calls = []

    def handler(request):
        calls.append(request.content)
        if len(calls) == 1:
            headers = {"x-should-retry": retry_header} if retry_header is not None else {}
            return httpx.Response(status, headers=headers, json={
                "error": {"code": "insufficient_quota", "message": _SECRET},
            })
        return _response(_partial(protocol, "complete"), _terminal(protocol))

    adapter = _adapter(protocol, handler)
    ctx = SimpleNamespace(llm=adapter, cancel_token=None, event_bus=None,
                          config=RuntimeConfig(llm_self_heal_max_attempts=2, llm_self_heal_base_delay_sec=0))

    async def run():
        async with stream_llm_resilient(ctx, None, LLMRequest("m", "", [])) as stream:
            async for _ in stream:
                pass
            return stream.get_final_result().text

    try:
        if retriable:
            assert await run() == "complete"
            assert len(calls) == 2
            assert calls[0] == calls[1]
        else:
            with pytest.raises(LLMCallError) as caught:
                await run()
            assert str(caught.value) == _SECRET
            assert not caught.value.retriable
            assert len(calls) == 1
    finally:
        await _close(adapter)


@pytest.mark.parametrize("fault", ["certificate", "protocol", "oserror"])
async def test_sdk_connection_failures_retain_sdk_retry_decision(caplog, fault):
    calls = []

    def handler(request):
        calls.append(request.content)
        if len(calls) == 1:
            if fault == "certificate":
                raise httpx.ConnectError(_SECRET) from ssl.SSLCertVerificationError(_SECRET)
            if fault == "protocol":
                raise httpx.UnsupportedProtocol(_SECRET)
            raise OSError(_SECRET)
        return _response(_partial("openai-official", "complete"), _terminal("openai-official"))

    adapter = _adapter("openai-official", handler)
    ctx = SimpleNamespace(llm=adapter, cancel_token=None, event_bus=None,
                          config=RuntimeConfig(llm_self_heal_max_attempts=2, llm_self_heal_base_delay_sec=0))
    try:
        async with stream_llm_resilient(ctx, None, LLMRequest("m", "", [])) as stream:
            async for _ in stream:
                pass
            assert stream.get_final_result().text == "complete"
        assert len(calls) == 2
        assert calls[0] == calls[1]
        assert "retriable=True" in caplog.text
        assert _SECRET in caplog.text
    finally:
        await _close(adapter)
