"""MCP provider via the official `mcp` SDK, exercised against an in-memory FastMCP server.

Verifies: initialize().instructions → provider.description, tools → ToolCapability,
call_tool result parsing, and session teardown on close().
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import anyio
from mcp import types as mcp_types
from mcp.server.fastmcp import Context, FastMCP
from mcp.shared.memory import create_client_server_memory_streams

from loomex_core.protocols.capability import ToolCapability
from loomex_core.protocols.context import ProviderContext
from loomex_core.providers.capability_mcp.provider import (
    MCPCapabilityProvider,
    MCPServerConfig,
)


def _ctx() -> ProviderContext:
    return ProviderContext(session_id="s", tenant_id="t")


def _build_server() -> FastMCP:
    server = FastMCP(name="demo", instructions="Always search before you fetch.")

    @server.tool()
    def search(query: str) -> str:
        """Search the knowledge base."""
        return f"results for {query}"

    @server.tool()
    def create_note(text: str) -> str:
        """Create a note."""
        return f"created: {text}"

    return server


class _InMemoryMCPProvider(MCPCapabilityProvider):
    """Provider whose transport is an in-process FastMCP server over memory streams."""

    def __init__(self, server: FastMCP, **kw) -> None:
        super().__init__(MCPServerConfig(name="demo", transport="stdio", command=["unused"]), **kw)
        self._server = server

    def _transport_cm(self):
        server = self._server

        @asynccontextmanager
        async def _cm():
            low = server._mcp_server
            init_opts = low.create_initialization_options()
            async with create_client_server_memory_streams() as (client_streams, server_streams):
                async with anyio.create_task_group() as tg:
                    tg.start_soon(
                        lambda: low.run(server_streams[0], server_streams[1], init_opts, raise_exceptions=True)
                    )
                    try:
                        yield (client_streams[0], client_streams[1])
                    finally:
                        tg.cancel_scope.cancel()

        return _cm()


async def test_start_prewarm_populates_description() -> None:
    p = _InMemoryMCPProvider(_build_server())
    try:
        assert await p.start() is True
        assert p.description == "Always search before you fetch."
        assert p.connection_status == "CONNECTED"
    finally:
        await p.close()


async def test_list_tools_become_tool_capabilities() -> None:
    p = _InMemoryMCPProvider(_build_server())
    try:
        assert await p.start() is True
        caps = await p.list(_ctx())
        assert all(isinstance(c, ToolCapability) for c in caps)
        by_id = {c.id: c for c in caps}
        assert "mcp:demo:search" in by_id
        assert by_id["mcp:demo:search"].description == "Search the knowledge base."
        # input schema carried through from the server
        assert by_id["mcp:demo:search"].input_schema.get("type") == "object"
        # 'create_note' matches a side-effect keyword; 'search' does not
        assert by_id["mcp:demo:create_note"].side_effects is True
        assert by_id["mcp:demo:search"].side_effects is False
    finally:
        await p.close()


async def test_invoke_calls_tool_and_parses_text() -> None:
    p = _InMemoryMCPProvider(_build_server())
    try:
        assert await p.start() is True
        events = [e async for e in p.invoke("mcp:demo:search", {"query": "cats"}, _ctx())]
        results = [e for e in events if e.kind == "result"]
        assert results and "results for cats" in results[0].payload["content"]
        assert results[0].payload["metadata"]["is_error"] is False
    finally:
        await p.close()


async def test_close_disconnects_session() -> None:
    p = _InMemoryMCPProvider(_build_server())
    assert await p.start() is True
    assert p.connection_status == "CONNECTED"
    await p.close()
    assert p.connection_status == "DISCONNECTED"


async def test_invoke_unknown_tool_flags_is_error() -> None:
    # MCP reports tool-level failures via isError in the result (not a protocol error).
    p = _InMemoryMCPProvider(_build_server())
    try:
        assert await p.start() is True
        events = [e async for e in p.invoke("mcp:demo:nope", {}, _ctx())]
        results = [e for e in events if e.kind == "result"]
        assert results and results[0].payload["metadata"]["is_error"] is True
    finally:
        await p.close()


# ── non-blocking + circuit breaker ─────────────────────────────────────────

class _FailingMCPProvider(MCPCapabilityProvider):
    """Transport that always fails to connect — exercises degrade + circuit breaker."""

    def __init__(self, **kw) -> None:
        super().__init__(MCPServerConfig(name="bad", transport="streamable_http", url="http://x"), **kw)
        self.attempts = 0

    def _transport_cm(self):
        self.attempts += 1

        @asynccontextmanager
        async def _cm():
            raise RuntimeError("connection refused")
            yield  # pragma: no cover

        return _cm()


async def test_list_degrades_without_blocking_and_opens_circuit() -> None:
    # long cooldown so the 2nd call lands inside the cooldown window
    p = _FailingMCPProvider(reconnect_base_delay_sec=60.0)
    try:
        caps = await p.list(_ctx())        # kicks a background attempt, returns immediately
        assert caps == []
        assert p._runner is not None
        await p._runner                    # let the one-shot attempt settle
        assert p.attempts == 1
        assert p.connection_status == "COOLDOWN"

        # second call within cooldown must NOT start another transport attempt
        assert await p.list(_ctx()) == []
        assert p.attempts == 1             # circuit open
    finally:
        await p.close()


async def test_invoke_reports_not_connected_when_unavailable() -> None:
    p = _FailingMCPProvider(reconnect_base_delay_sec=60.0)
    try:
        events = [e async for e in p.invoke("mcp:bad:whatever", {}, _ctx())]
        assert any(e.kind == "error" and e.payload.get("code") == "NOT_CONNECTED" for e in events)
    finally:
        await p.close()


async def test_start_returns_false_on_failure() -> None:
    p = _FailingMCPProvider()
    assert await p.start() is False
    assert p.connection_status in ("COOLDOWN", "DISCONNECTED")
    await p.close()


# ── tools/list_changed → cache invalidation ────────────────────────────────

async def test_on_server_message_invalidates_cache_on_list_changed() -> None:
    p = _InMemoryMCPProvider(_build_server())
    p._capabilities_cache = []  # pretend we have a cached snapshot
    notif = mcp_types.ServerNotification(
        mcp_types.ToolListChangedNotification(method="notifications/tools/list_changed")
    )
    await p._on_server_message(notif)
    assert p._capabilities_cache is None
    # unrelated input (e.g. a transport exception) must be ignored, not raise
    p._capabilities_cache = []
    await p._on_server_message(RuntimeError("boom"))
    assert p._capabilities_cache == []


def _build_dynamic_server() -> FastMCP:
    server = FastMCP(name="demo", instructions="dynamic")

    @server.tool()
    async def add_more(ctx: Context) -> str:
        """Register a new tool at runtime and notify clients."""
        @server.tool()
        def brand_new() -> str:
            """Freshly added tool."""
            return "hi"
        await ctx.session.send_tool_list_changed()
        return "added"

    return server


async def test_list_changed_notification_refreshes_tools_end_to_end() -> None:
    p = _InMemoryMCPProvider(_build_dynamic_server())
    try:
        assert await p.start() is True
        caps1 = await p.list(_ctx())
        assert "brand_new" not in {c.name for c in caps1}

        # calling add_more makes the server register a tool + emit tools/list_changed
        [e async for e in p.invoke("mcp:demo:add_more", {}, _ctx())]

        # let the client receive loop process the notification
        for _ in range(50):
            if p._capabilities_cache is None:
                break
            await asyncio.sleep(0.02)
        assert p._capabilities_cache is None  # handler invalidated the snapshot

        caps2 = await p.list(_ctx())
        assert "brand_new" in {c.name for c in caps2}
    finally:
        await p.close()
