"""A mid-stream network stall must raise an outage error, not hang forever.

Reproduces the real bug: the LLM starts responding, then the connection silently
goes dead (no bytes, no RST). With the streaming read timeout disabled the adapter
awaits the next byte forever. With a finite idle (read) timeout, httpx raises
ReadTimeout (a TransportError); since chunks were already produced, the adapter
raises LLMCallError(outage=True) -> the runtime routes it to INTERRUPTED.

The test uses a tiny local HTTP server that streams one SSE token then stalls,
keeping the socket open. We bound the call with asyncio.wait_for so that a
regression (the hang) fails loudly instead of blocking the suite.
"""
from __future__ import annotations

import asyncio

import pytest

from ctx_weft.protocols import LLMCallError, LLMMessage, LLMRequest
from ctx_weft.providers.llm.anthropic import AnthropicAdapter
from ctx_weft.providers.llm.openai import OpenAIAdapter

pytestmark = pytest.mark.asyncio

# One SSE event that yields a visible token (-> produced=True) in each provider's
# wire shape, then the handler stalls without sending anything else or closing.
_ANTHROPIC_TOKEN = (
    b'data: {"type":"content_block_delta","index":0,'
    b'"delta":{"type":"text_delta","text":"hi"}}\n\n'
)
_OPENAI_TOKEN = b'data: {"choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n'


async def _stalling_server(token: bytes):
    """Serve HTTP/1.1: one SSE token, then hold the connection open in silence."""
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            await reader.read(65536)  # consume the request (headers + small body)
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"\r\n"
                + token
            )
            await writer.drain()
            await asyncio.sleep(30)  # stall: connection stays open, no more bytes
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def _assert_stall_raises_outage(adapter):
    req = LLMRequest(model="m", system="", messages=[LLMMessage(role="user", content="hi")])

    async def _drain():
        return [c async for c in adapter.complete(req, stream=True)]

    # 8s budget >> the 1s idle timeout; a hang (regression) trips wait_for instead.
    with pytest.raises(LLMCallError) as exc:
        await asyncio.wait_for(_drain(), timeout=8)
    assert exc.value.outage is True, "mid-stream stall must be tagged outage"


async def test_anthropic_midstream_stall_raises_outage():
    server, port = await _stalling_server(_ANTHROPIC_TOKEN)
    try:
        adapter = AnthropicAdapter(api_key="k", base_url=f"http://127.0.0.1:{port}", timeout_sec=1)
        await _assert_stall_raises_outage(adapter)
    finally:
        server.close()
        await server.wait_closed()


async def test_openai_midstream_stall_raises_outage():
    server, port = await _stalling_server(_OPENAI_TOKEN)
    try:
        adapter = OpenAIAdapter(api_key="k", base_url=f"http://127.0.0.1:{port}", timeout_sec=1)
        await _assert_stall_raises_outage(adapter)
    finally:
        server.close()
        await server.wait_closed()
