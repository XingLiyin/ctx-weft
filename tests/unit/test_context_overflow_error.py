from ctx_weft.core.errors import ContextOverflowError


def test_carries_fields_and_non_retriable():
    e = ContextOverflowError(
        "overflow", required=200_000, effective_limit=171_808,
        context_limit=180_000, reserved_output_tokens=8192,
    )
    assert e.retriable is False
    assert e.required == 200_000
    assert e.effective_limit == 171_808
    assert e.context_limit == 180_000
    assert e.reserved_output_tokens == 8192
    assert e.code == "CONTEXT_OVERFLOW"


def test_message_only_still_works():
    e = ContextOverflowError("boom")
    assert e.required == 0 and e.retriable is False
