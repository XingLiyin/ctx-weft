from ctx_weft.core.estimate import effective_limit


def test_subtracts_reserve():
    assert effective_limit(180_000, 8192) == 180_000 - 8192


def test_zero_reserve_is_full_window():
    assert effective_limit(180_000, 0) == 180_000


def test_reserve_exceeding_context_clamps_to_zero():
    assert effective_limit(4_000, 8192) == 0


def test_negative_reserve_treated_as_zero():
    assert effective_limit(180_000, -5) == 180_000
