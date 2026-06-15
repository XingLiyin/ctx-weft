from ctx_weft.core.control.types import SessionView


def test_projection_has_context_limit_default():
    p = SessionView(id="test-session")
    assert p.context_limit == 180_000
