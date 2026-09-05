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

    # 2026-09-04（Task 12，events-v2 §5）起 TM 不再报 TaskQueueDrained（其消费者，
    # 会话状态机，早已退役）——结论直接写在 `tm.session.status` 上，不再对外广播。
    assert "TaskQueueDrained" not in [str(e.type) for e in bus.emitted]
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

    assert done["bg"] is True  # 落定终态、触发收尾回调前 gather 了后台 recap
