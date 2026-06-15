import pytest
from ctx_weft.core.runtime import SessionStartParams


def test_context_limit_required():
    with pytest.raises(TypeError):
        SessionStartParams.create(template_id="t", user_prompt="p")  # missing context_limit


def test_context_limit_passed():
    p = SessionStartParams.create(template_id="t", user_prompt="p", context_limit=123)
    assert p.context_limit == 123
