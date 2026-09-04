from ctx_weft.core.models.errors import ContextOverflowError


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
    assert str(e) == "boom"


def test_fields_only_build_actionable_message():
    e = ContextOverflowError(
        required=200_000, effective_limit=171_808,
        context_limit=180_000, reserved_output_tokens=8192,
    )
    msg = str(e)
    assert "请改用更大上下文窗口的模型" in msg
    assert "171808" in msg
