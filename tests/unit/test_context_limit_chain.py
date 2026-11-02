from ctx_weft.core.control.converters import session_from_projection
from ctx_weft.core.control.types import SessionView


def test_converter_does_not_restore_legacy_session_limits():
    projection = SessionView(id="s")
    projection.context_limit = 99_000
    session = session_from_projection(projection)
    assert not hasattr(session, "context_limit")
