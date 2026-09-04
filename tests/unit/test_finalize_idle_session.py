import pytest
from ctx_weft.core.orchestrator.task.hooks import TaskManagerHooks
from ctx_weft.core.orchestrator.task.manager import TaskManager
from ctx_weft.core.models.session import Session
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
    tm.set_hooks(TaskManagerHooks(is_current=lambda: True))

    await tm.finalize_idle_session("SUCCEEDED")

    # Task 6：TM 只报「我这边空了、结论是 SUCCEEDED」，SessionFinished 由 SM 发。
    kinds = [(e.type, (e.payload or {}).get("final_status")) for e in bus.emitted]
    assert (EventType.TASK_QUEUE_DRAINED, "SUCCEEDED") in kinds
    assert EventType.SESSION_STATUS_CHANGED not in [e.type for e in bus.emitted]
    assert tm.session.status == "SUCCEEDED"


@pytest.mark.asyncio
async def test_finalize_idle_session_gathers_background_recap():
    import asyncio
    bus = _Bus()
    tm = TaskManager(session_id="ses1", event_bus=bus)
    tm.set_session(Session(id="ses1", user_prompt="", status="RUNNING", tenant_id="default"))
    tm.set_hooks(TaskManagerHooks(is_current=lambda: True))
    done = {"bg": False}
    async def _bg():
        await asyncio.sleep(0.01)
        done["bg"] = True
    tm.track_background(asyncio.create_task(_bg()))

    await tm.finalize_idle_session("SUCCEEDED")

    assert done["bg"] is True  # 报队列状态（→ SessionFinished）前 gather 了后台 recap
