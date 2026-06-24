from ctx_weft.core.config import RuntimeConfig


def test_runtime_config_self_heal_defaults():
    c = RuntimeConfig()
    assert c.llm_self_heal_max_attempts == 8
    assert c.llm_self_heal_max_duration_sec == 300.0
    assert c.llm_self_heal_base_delay_sec == 2.0
    assert c.llm_self_heal_max_interval_sec == 60.0
# host-layer threading (Settings env -> RuntimeConfig) lives in the host test suite
# (tests/test_self_heal_config_host.py); core stays brand-neutral, no host import.
