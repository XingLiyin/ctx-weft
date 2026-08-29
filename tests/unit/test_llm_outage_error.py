from ctx_weft.protocols import LLMCallError, LLMOutageError
from ctx_weft.protocols.events import EVENT_TYPES, EventType, TRANSIENT_EVENT_TYPES


def test_llmcallerror_defaults_no_outage():
    e = LLMCallError("boom")
    assert e.retriable is True
    assert e.outage is False
    assert e.retry_after_sec is None
    assert e.status_code == 0


def test_llmcallerror_outage_flag_settable():
    e = LLMCallError("503", status_code=503, retriable=True, outage=True, retry_after_sec=12.0)
    assert e.outage is True
    assert e.retry_after_sec == 12.0


def test_llmoutageerror_is_llmcallerror_and_marks_outage():
    e = LLMOutageError("exhausted")
    assert isinstance(e, LLMCallError)
    assert e.retriable is True
    assert e.outage is True


def test_llmoutageerror_preserves_status_and_retry_after():
    e = LLMOutageError("x", status_code=429, retry_after_sec=5.0)
    assert e.status_code == 429
    assert e.retry_after_sec == 5.0


def test_llm_retry_triggered_is_known_and_transient():
    assert EventType.LLM_RETRY_TRIGGERED in EVENT_TYPES
    assert EventType.LLM_RETRY_TRIGGERED in TRANSIENT_EVENT_TYPES
