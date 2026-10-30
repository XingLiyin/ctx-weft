"""Exercise real adapters and the gateway together using injected HTTP faults."""

import asyncio
import json
import ssl
from types import SimpleNamespace

import httpx
import pytest

from ctx_weft.core.control.tokens import CancelToken
from ctx_weft.core.loop import llm_gateway
from ctx_weft.core.loop.llm_gateway import stream_llm_resilient
from ctx_weft.core.models.config import RuntimeConfig
from ctx_weft.protocols import LLMCallError, LLMOutageError, LLMRequest
from ctx_weft.providers.llm.adapters.anthropic import AnthropicAdapter
from ctx_weft.providers.llm.adapters.openai_compatible import OpenAICompatibleAdapter


def _sse(event):
    return ("data: " + json.dumps(event) + "\n\n").encode()


def _success(protocol):
    if protocol == "openai":
        return _sse({"choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}]})
    return (
        _sse({"type": "content_block_delta", "index": 0,
              "delta": {"type": "text_delta", "text": "answer"}})
        + _sse({"type": "message_delta", "delta": {"stop_reason": "end_turn"}})
        + _sse({"type": "message_stop"})
    )


def _adapter(protocol, handler):
    base = OpenAICompatibleAdapter if protocol == "openai" else AnthropicAdapter

    class Offline(base):
        def _make_client(self):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return Offline(api_key="test-key")


def _ctx(adapter, *, token=None, **config):
    return SimpleNamespace(
        llm=adapter, cancel_token=token, event_bus=None,
        config=RuntimeConfig(llm_self_heal_base_delay_sec=0, **config),
    )


async def _drain(ctx):
    stream = stream_llm_resilient(ctx, None, LLMRequest(model="m", system="", messages=[]))
    async with stream:
        return [chunk async for chunk in stream]


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
@pytest.mark.parametrize("fault", [httpx.ConnectError, httpx.ReadTimeout, httpx.WriteError,
                                    httpx.RemoteProtocolError, 429, 503])
async def test_network_and_http_faults_share_one_attempt_budget(protocol, fault):
    requests = []

    def respond(request):
        requests.append(request.content)
        if len(requests) < 3:
            if isinstance(fault, int):
                return httpx.Response(fault, text="sensitive upstream body")
            raise fault("sensitive upstream error")
        return httpx.Response(200, content=_success(protocol))

    adapter = _adapter(protocol, respond)
    try:
        chunks = await _drain(_ctx(adapter, llm_self_heal_max_attempts=3))
        assert "".join(c.text for c in chunks if c.kind == "token") == "answer"
        assert len(requests) == 3
        assert requests[0] == requests[1] == requests[2]
    finally:
        await adapter._client.aclose()


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
@pytest.mark.parametrize("fault", [httpx.ConnectError, 503])
async def test_exhausted_budget_is_not_multiplied_by_adapter(protocol, fault):
    calls = []

    def respond(request):
        calls.append(request)
        if isinstance(fault, int):
            return httpx.Response(fault, text="sensitive upstream body")
        raise fault("sensitive upstream error")

    adapter = _adapter(protocol, respond)
    try:
        with pytest.raises(LLMOutageError) as error:
            await _drain(_ctx(adapter, llm_self_heal_max_attempts=3))
        assert len(calls) == 3
        assert "sensitive" not in str(error.value)
    finally:
        await adapter._client.aclose()


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
@pytest.mark.parametrize("fault", [400, 401, 403, 404, "certificate", "protocol"])
async def test_permanent_failure_is_single_attempt_and_redacted(protocol, fault):
    calls = []

    def respond(request):
        calls.append(request)
        if isinstance(fault, int):
            return httpx.Response(fault, text="secret-key sensitive body")
        if fault == "protocol":
            raise httpx.UnsupportedProtocol("sensitive endpoint")
        raise httpx.ConnectError("secret-key") from ssl.SSLCertVerificationError("secret-key")

    adapter = _adapter(protocol, respond)
    try:
        with pytest.raises(LLMCallError) as error:
            await _drain(_ctx(adapter))
        assert error.value.retriable is False
        assert error.value.outage is False
        assert len(calls) == 1
        assert "secret-key" not in str(error.value)
        assert "sensitive" not in str(error.value)
        if not isinstance(fault, int):
            assert error.value.__cause__ is not None
    finally:
        await adapter._client.aclose()


class _BlockedBody(httpx.AsyncByteStream):
    def __init__(self, *, prefix=b""):
        self.prefix = prefix
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        if self.prefix:
            yield self.prefix
        self.started.set()
        await self.release.wait()
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
async def test_deadline_closes_a_request_stuck_before_first_chunk(protocol):
    body = _BlockedBody()
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, stream=body)

    adapter = _adapter(protocol, respond)
    try:
        with pytest.raises(LLMOutageError, match="deadline"):
            await asyncio.wait_for(_drain(_ctx(adapter, llm_self_heal_max_duration_sec=0.05)), 2)
        assert len(calls) == 1
        assert body.closed
    finally:
        await adapter._client.aclose()


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
async def test_cancel_closes_a_request_without_waiting_for_first_chunk(protocol):
    body = _BlockedBody()
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, stream=body)

    adapter = _adapter(protocol, respond)
    token = CancelToken()
    task = asyncio.create_task(_drain(_ctx(adapter, token=token)))
    try:
        await asyncio.wait_for(body.started.wait(), 2)
        token.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert len(calls) == 1
        assert body.closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await adapter._client.aclose()


async def test_first_chunk_disarms_deadline_for_a_healthy_long_stream():
    body = _BlockedBody(prefix=_success("openai"))
    adapter = _adapter("openai", lambda _: httpx.Response(200, stream=body))
    task = asyncio.create_task(_drain(_ctx(adapter, llm_self_heal_max_duration_sec=0.05)))
    try:
        await asyncio.wait_for(body.started.wait(), 2)
        await asyncio.sleep(0.08)
        assert not task.done()
        body.release.set()
        chunks = await asyncio.wait_for(task, 2)
        assert "".join(c.text for c in chunks if c.kind == "token") == "answer"
        assert body.closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await adapter._client.aclose()


async def test_act_pause_closes_a_request_while_waiting_for_first_chunk():
    from ctx_weft.core.control.tokens import PauseToken
    from ctx_weft.core.loop.signals import HitlPark
    from ctx_weft.core.loop.steps.act import ActStep
    from tests.integration.test_interactive_task import _act_state_ctx

    body, calls = _BlockedBody(), []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, stream=body)

    adapter, pause = _adapter("openai", respond), PauseToken()
    state, ctx, _, _, _ = _act_state_ctx(False, adapter)
    ctx.pause_token = pause
    task = asyncio.create_task(ActStep().execute(state, ctx))
    try:
        await asyncio.wait_for(body.started.wait(), 2)
        pause.pause()
        with pytest.raises(HitlPark):
            await asyncio.wait_for(task, 2)
        assert len(calls) == 1
        assert body.closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await adapter._client.aclose()


async def test_retry_after_beyond_deadline_waits_without_sending_early(monkeypatch):
    now, delays, calls = [0.0], [], []
    monkeypatch.setattr(llm_gateway, "monotonic", lambda: now[0])
    monkeypatch.setattr(llm_gateway.random, "uniform", lambda *_: 0)

    async def sleep(delay, token):
        delays.append(delay)
        now[0] += delay

    monkeypatch.setattr(llm_gateway, "_sleep_cancellable", sleep)

    def respond(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})

    adapter = _adapter("openai", respond)
    try:
        with pytest.raises(LLMOutageError) as error:
            await _drain(_ctx(adapter, llm_self_heal_max_duration_sec=10))
        assert error.value.status_code == 429
        assert delays == [10]
        assert len(calls) == 1
    finally:
        await adapter._client.aclose()


async def test_cancel_during_real_backoff_prevents_next_request(monkeypatch):
    waiting, token, calls = asyncio.Event(), CancelToken(), []
    real_sleep = llm_gateway._sleep_cancellable

    async def sleep(delay, tok):
        waiting.set()
        await real_sleep(60, tok)

    monkeypatch.setattr(llm_gateway, "_sleep_cancellable", sleep)

    def respond(request):
        calls.append(request)
        return httpx.Response(503)

    adapter = _adapter("openai", respond)
    task = asyncio.create_task(_drain(_ctx(adapter, token=token)))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        token.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert len(calls) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await adapter._client.aclose()


async def test_act_retries_current_model_turn_without_repeating_completed_tool():
    from ctx_weft.core.loop.capability_gateway import InvocationResult
    from ctx_weft.core.loop.steps.act import ActStep
    from ctx_weft.protocols import EventType
    from tests.integration.test_interactive_task import _act_state_ctx

    class CounterGateway:
        calls = 0

        async def invoke(self, **kwargs):
            self.calls += 1
            return InvocationResult(
                invocation_id="effect", tool_name=kwargs["tool_name"],
                content="effect already completed", is_error=False,
            )

    payloads = []

    def respond(request):
        payloads.append(json.loads(request.content))
        if len(payloads) == 1:
            return httpx.Response(200, content=_sse({"choices": [{
                "delta": {"tool_calls": [{"index": 0, "id": "wire_call", "function": {
                    "name": "counter", "arguments": "{}",
                }}]}, "finish_reason": "tool_calls",
            }]}))
        if len(payloads) < 4:
            raise httpx.ReadTimeout("offline")
        return httpx.Response(200, content=_success("openai"))

    adapter, gateway = _adapter("openai", respond), CounterGateway()
    state, ctx, task, _, _ = _act_state_ctx(True, adapter, gateway=gateway)
    ctx.config = RuntimeConfig(llm_self_heal_max_attempts=3, llm_self_heal_base_delay_sec=0)
    events = []
    original_emit = ctx.event_bus.emit

    async def emit(event):
        events.append(event)
        await original_emit(event)

    ctx.event_bus.emit = emit
    try:
        outcome = await ActStep().execute(state, ctx)
        assert outcome.next_step == "observe"
        assert len(payloads) == 4
        assert gateway.calls == 1
        assert payloads[1] == payloads[2] == payloads[3]
        assert any(m["role"] == "tool" and m["content"] == "effect already completed"
                   for m in payloads[-1]["messages"])
        assert task.retry_count == 0
        assert state.session.failure_counter == 0
        assert sum(e.type == EventType.LLM_RETRY_TRIGGERED for e in events) == 2
        assert not any(e.type in {EventType.TASK_FAILED, EventType.TASK_REQUEUED} for e in events)
    finally:
        await adapter._client.aclose()


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
@pytest.mark.parametrize("kind", ["token", "reasoning", "tool_call_partial"])
async def test_exhausted_midstream_fault_does_not_execute_incomplete_output(protocol, kind):
    from ctx_weft.core.loop.steps.act import ActStep
    from tests.integration.test_interactive_task import _act_state_ctx

    if protocol == "openai":
        delta = {
            "token": {"content": "partial"},
            "reasoning": {"reasoning_content": "partial"},
            "tool_call_partial": {"tool_calls": [{"index": 0, "id": "wire_call", "function": {
                "name": "counter", "arguments": '{"unfinished":',
            }}]},
        }[kind]
        prefix = _sse({"choices": [{"delta": delta}]})
    elif kind == "tool_call_partial":
        prefix = _sse({"type": "content_block_start", "index": 0,
                       "content_block": {"type": "tool_use", "id": "wire_call", "name": "counter"}})
        prefix += _sse({"type": "content_block_delta", "index": 0,
                        "delta": {"type": "input_json_delta", "partial_json": '{"unfinished":'}})
    else:
        delta = ({"type": "text_delta", "text": "partial"} if kind == "token" else
                 {"type": "thinking_delta", "thinking": "partial"})
        prefix = _sse({"type": "content_block_delta", "index": 0, "delta": delta})

    class BrokenBody(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield prefix
            raise httpx.ReadError("offline")

        async def aclose(self):
            self.closed = True

    body, calls = BrokenBody(), []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, stream=body)

    class UnexpectedToolGateway:
        async def invoke(self, **kwargs):
            pytest.fail("Interrupted model output must not execute a tool")

    adapter = _adapter(protocol, respond)
    state, ctx, task, _, _ = _act_state_ctx(True, adapter, gateway=UnexpectedToolGateway())
    ctx.config = RuntimeConfig(llm_self_heal_max_attempts=1, llm_self_heal_base_delay_sec=0)
    try:
        with pytest.raises(LLMOutageError, match="exhausted after 1"):
            await ActStep().execute(state, ctx)
        assert len(calls) == 1
        assert body.closed
        assert task.retry_count == 0
    finally:
        await adapter._client.aclose()
