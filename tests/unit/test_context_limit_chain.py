from ctx_weft.core.control.converters import session_from_projection
from ctx_weft.core.control.types import SessionView


def test_converter_carries_context_limit():
    proj = SessionView(id="s")
    proj.context_limit = 99_000
    s = session_from_projection(proj)
    assert s.context_limit == 99_000
