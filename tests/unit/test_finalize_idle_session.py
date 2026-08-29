import pytest
from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.state.models import Session
from ctx_weft.protocols.events import EventType


class _Bus:
    def __init__(self):
        self.emitted = []
    async def emit(self, ev):
        self.emitted.append(ev)


@pytest.mark.asyncio
async def test_finalize_idle_session_emits_status_and_finished():
    bus = _Bus()
    tm = TaskManager(session_id="ses1", event_bus=bus)
    tm.set_session(Session(id="ses1", user_prompt="", status="RUNNING", tenant_id="default"))
    tm.set_is_current(lambda: True)

    await tm.finalize_idle_session("SUCCEEDED")

    kinds = [(e.type, (e.payload or {}).get("new_status") or (e.payload or {}).get("final_status"))
             for e in bus.emitted]
    assert (EventType.SESSION_STATUS_CHANGED, "SUCCEEDED") in kinds
    assert (EventType.SESSION_FINISHED, "SUCCEEDED") in kinds
    assert tm.session.status == "SUCCEEDED"


@pytest.mark.asyncio
async def test_finalize_idle_session_gathers_background_recap():
    import asyncio
    bus = _Bus()
    tm = TaskManager(session_id="ses1", event_bus=bus)
    tm.set_session(Session(id="ses1", user_prompt="", status="RUNNING", tenant_id="default"))
    tm.set_is_current(lambda: True)
    done = {"bg": False}
    async def _bg():
        await asyncio.sleep(0.01)
        done["bg"] = True
    tm.track_background(asyncio.create_task(_bg()))

    await tm.finalize_idle_session("SUCCEEDED")

    assert done["bg"] is True  # SESSION_FINISHED 前 gather 了后台 recap
