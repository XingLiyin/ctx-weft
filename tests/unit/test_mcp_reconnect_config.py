"""TDD: MCPCapabilityProvider reconnect params via ctor (Task 1.3)."""
from ctx_weft.providers.capability_mcp.provider import MCPCapabilityProvider, MCPServerConfig


def _cfg() -> MCPServerConfig:
    # Only `name` is required; all other fields have defaults.
    return MCPServerConfig(name="test-server")


def test_mcp_reconnect_defaults():
    p = MCPCapabilityProvider(_cfg())
    assert p._max_reconnect_attempts == 3
    assert p._reconnect_base_delay_sec == 1.0


def test_mcp_reconnect_override():
    p = MCPCapabilityProvider(_cfg(), max_reconnect_attempts=5, reconnect_base_delay_sec=0.5)
    assert p._max_reconnect_attempts == 5
    assert p._reconnect_base_delay_sec == 0.5
