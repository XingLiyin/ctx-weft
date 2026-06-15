from ctx_weft.core.state.models import Session


def test_session_carries_context_limit():
    s = Session(id="s", user_prompt="p", status="RUNNING", context_limit=64_000)
    assert s.context_limit == 64_000
