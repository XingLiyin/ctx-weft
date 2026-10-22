"""Session containers expose identity/configuration, never host display status."""
from dataclasses import fields

from ctx_weft.core.control.converters import session_from_projection
from ctx_weft.core.control.reducers import deserialize_view, serialize_view
from ctx_weft.core.control.types import RunStateView, SessionView
from ctx_weft.core.models.session import Session
from ctx_weft.protocols.runtime import SessionDetail, SessionStateSummary


def test_session_status_is_absent_from_models_and_public_queries():
    for model, name in ((Session, "status"), (SessionView, "status"),
                        (SessionDetail, "status"), (RunStateView, "session_status"),
                        (SessionStateSummary, "session_status")):
        assert name not in {field.name for field in fields(model)}


def test_old_snapshot_status_is_ignored_and_never_written_back():
    legacy = {
        "run_id": "run", "session_id": "s", "task_id": "", "agent_id": "root",
        "session_status": "RUNNING",
        "sessions": {"s": {"id": "s", "status": "RUNNING", "root_agent_id": "root"}},
    }
    view = deserialize_view(legacy)
    session = session_from_projection(view.sessions["s"])
    assert session.root_agent_id == "root"
    assert not hasattr(session, "status")
    current = serialize_view(view)
    assert "session_status" not in current
    assert "status" not in current["sessions"]["s"]
