import pytest
from ctx_weft.core.runtime import SessionStartParams


def test_session_start_has_no_budget_configuration():
    params = SessionStartParams.create(template_id="t", user_prompt="p")
    for field in ("context_limit", "reserved_output_tokens", "token_budget"):
        assert not hasattr(params, field)
        with pytest.raises(TypeError):
            SessionStartParams.create(template_id="t", user_prompt="p", **{field: 100})
