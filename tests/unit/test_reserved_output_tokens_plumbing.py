
from tests.model_testkit import model_snapshot
from ctx_weft.core.models.agent import LoopGuard


def test_counters_have_no_budget():
    guard = LoopGuard()
    assert not hasattr(guard, "context_limit")
    assert not hasattr(guard, "reserved_output_tokens")


def test_budget_reserves_output_space():
    assert model_snapshot(100_000, 4096).effective_input_limit == 95_904
