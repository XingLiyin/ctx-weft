from loomex_core.core.control.converters import session_from_projection
from loomex_core.core.control.types import SessionView


def test_converter_carries_context_limit():
    proj = SessionView(id="s")
    proj.context_limit = 99_000
    s = session_from_projection(proj)
    assert s.context_limit == 99_000
