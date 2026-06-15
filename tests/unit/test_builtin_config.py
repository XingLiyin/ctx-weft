from ctx_weft.providers.capability_builtin.provider import (
    BuiltinToolsConfig, BuiltinToolsCapabilityProvider,
)


def test_builtin_config_defaults():
    c = BuiltinToolsConfig()
    assert c.http_timeout_sec == 30
    assert c.http_max_response_bytes == 1_000_000


def test_builtin_provider_holds_config():
    p = BuiltinToolsCapabilityProvider(BuiltinToolsConfig(http_timeout_sec=5))
    assert p._cfg.http_timeout_sec == 5
