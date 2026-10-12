import pytest
from ctx_weft.core.loop.background import recap as recap_mod
from ctx_weft.protocols.events import EventType


def _recap_types(bus_events):
    return [e.type for e in bus_events if e.type in (
        EventType.TASK_RECAP_STARTED, EventType.TASK_RECAP_DONE)]


@pytest.mark.asyncio
async def test_started_and_done_emitted_on_success(fake_state_ctx, monkeypatch):
    state, ctx = fake_state_ctx  # ctx.event_bus 收集 emit 到 ctx.event_bus.emitted
    async def _fake_react(*a, **k):
        from ctx_weft.core.capabilities.control_tools import ControlResult
        return ControlResult(content="recap text", metadata={"task_summary": "sum"}), ""
    monkeypatch.setattr(recap_mod, "run_observe_react", _fake_react)

    await recap_mod._run_recap(state, ctx, boundary="interrupt")

    kinds = _recap_types(ctx.event_bus.emitted)
    assert kinds == [EventType.TASK_RECAP_STARTED, EventType.TASK_RECAP_DONE]


@pytest.mark.asyncio
async def test_done_emitted_even_on_exception(fake_state_ctx, monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("observe blew up")
    monkeypatch.setattr(recap_mod, "run_observe_react", _boom)

    await recap_mod._run_recap(state=fake_state_ctx[0], ctx=fake_state_ctx[1], boundary="interrupt")

    kinds = _recap_types(fake_state_ctx[1].event_bus.emitted)
    assert EventType.TASK_RECAP_STARTED in kinds
    assert EventType.TASK_RECAP_DONE in kinds  # 异常出口也发 DONE
