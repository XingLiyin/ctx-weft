from ctx_weft.core.models.session import Session


def test_session_keeps_usage_but_no_budget():
    session = Session(id="s", user_prompt="p")
    assert session.token_used == 0
    for field in ("context_limit", "reserved_output_tokens", "token_budget"):
        assert not hasattr(session, field)
