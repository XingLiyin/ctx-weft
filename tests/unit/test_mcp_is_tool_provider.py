"""Regression: MCPCapabilityProvider must be a ToolCapabilityProvider.

The gateway indexes routable providers via `isinstance(p, ToolCapabilityProvider)`
(see CapabilityGateway._provider_index). If the MCP provider only inherits bare
`CapabilityProvider`, it is silently dropped from the router while the cache still
advertises its tools to the LLM — every MCP tool call then fails with
"no provider found for 'mcp:...'" and emits no CAPABILITY_INVOKED event.
"""
from ctx_weft.protocols.capability import ToolCapabilityProvider
from ctx_weft.providers.capability_mcp.provider import MCPCapabilityProvider, MCPServerConfig


def test_mcp_provider_is_routable_tool_provider() -> None:
    p = MCPCapabilityProvider(MCPServerConfig(name="web-search"))
    # The gateway uses exactly this check to build its router index.
    assert isinstance(p, ToolCapabilityProvider)
    # Provider name must equal the cap.id prefix the gateway routes on
    # (cap.id "mcp:web-search:glob" → prefix "mcp:web-search").
    assert p.name == "mcp:web-search"
