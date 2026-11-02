from ctx_weft.core.control.types import SessionView


def test_projection_has_no_session_budgets():
    projection = SessionView(id="s")
    for field in ("context_limit", "reserved_output_tokens", "token_budget"):
        assert not hasattr(projection, field)
