"""Sync unit tests for _compute_delay — kept in a separate file without asyncio pytestmark."""
from ctx_weft.core.loop.llm_gateway import _compute_delay


def test_compute_delay_exponential_and_capped():
    assert _compute_delay(1, base=2.0, max_interval=60.0, retry_after=None) == 2.0
    assert _compute_delay(2, base=2.0, max_interval=60.0, retry_after=None) == 4.0
    assert _compute_delay(10, base=2.0, max_interval=60.0, retry_after=None) == 60.0


def test_compute_delay_prefers_retry_after_capped():
    assert _compute_delay(1, base=2.0, max_interval=60.0, retry_after=5.0) == 5.0
    assert _compute_delay(1, base=2.0, max_interval=60.0, retry_after=999.0) == 60.0
