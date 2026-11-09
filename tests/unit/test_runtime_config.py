import pytest

from ctx_weft.core.models.config import RuntimeConfig


def test_defaults_match_legacy():
    c = RuntimeConfig()
    assert c.hitl_timeout_sec is None
    assert c.hitl_max_resolved == 1000
    assert c.task_max_concurrent == 4
    assert c.task_max_retries == 3
    assert c.observe_llm_idle_timeout_sec == 300.0
    assert not hasattr(c, "default_token_budget")


def test_overrides():
    c = RuntimeConfig(task_max_concurrent=8, hitl_timeout_sec=30)
    assert c.task_max_concurrent == 8
    assert c.hitl_timeout_sec == 30


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_observe_idle_timeout_cannot_disable_stall_protection(timeout):
    with pytest.raises(ValueError, match="observe_llm_idle_timeout_sec"):
        RuntimeConfig(observe_llm_idle_timeout_sec=timeout)


@pytest.mark.parametrize("options", [
    {"storage_commit_max_attempts": 0}, {"storage_commit_max_attempts": True},
    {"storage_recovery_max_attempts": -1}, {"storage_recovery_max_attempts": 1.5},
    {"storage_recovery_base_delay_sec": -1}, {"storage_recovery_base_delay_sec": float("nan")},
    {"storage_recovery_max_duration_sec": 0}, {"storage_recovery_max_duration_sec": float("inf")},
])
def test_storage_recovery_budgets_are_bounded(options):
    with pytest.raises(ValueError, match="storage"):
        RuntimeConfig(**options)
