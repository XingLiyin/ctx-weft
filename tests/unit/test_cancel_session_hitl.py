"""用户取消会话时，未决的 ask_user 必须一并终局（总账 A10）。

不终局的代价不在当下——当下那个 task 已经被 cancel_all 清掉了——
而在**重启之后**：`rebuild_hitl` 按「有 HitlOpened 无终局事件」折 pending，
于是一个已取消会话的提问会被当成未决恢复出来。
"""

from __future__ import annotations

import pytest

from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.state.models import Session, Task
from ctx_weft.core.utils import now_utc
from ctx_weft.protocols.events import EventType
from ctx_weft.protocols.hitl import HITL_OUTCOME_CANCELLED, HitlAsk, UserTurnDelivery
from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_runtime

pytestmark = pytest.mark.asyncio


class _RecordingBus:
    """挂在 runtime 真实事件总线上的记录桩——不替换总线，只旁听。"""

    def __init__(self) -> None:
        self.events: list = []

    async def _record(self, ev) -> None:
        self.events.append(ev)


def _runtime():
    return make_runtime(agent_provider=InlineAgentTemplateProvider())


async def _wire_pending_session(rt, session_id: str, task_id: str) -> None:
    """手搭一个「有 task 正等 ask_user」的会话：不真跑 TaskManager.drain，只登记
    runtime 侧的两处状态（`_task_managers` 映射 + SessionManager 的会话状态），
    使 `cancel_session` 的守卫（`per` 或 `task_manager` 非空）与 `cancel_all`
    内部对 `_session_manager` 的透传都落在真实组件上。
    """
    session = Session(
        id=session_id, user_prompt="hi", status="RUNNING",
        tenant_id="default", created_at=now_utc(),
    )
    tm = TaskManager(session_id=session_id, event_bus=rt._event_bus, max_concurrent=0)
    tm.set_session(session)
    tm.set_session_manager(rt._session_manager)
    task = Task(id=task_id, session_id=session_id, status="SUSPENDED", tenant_id="default")
    tm.register_task(task)
    rt._task_managers[session_id] = tm
    rt._session_manager.register_session(session_id, tenant_id="default")


@pytest.fixture
async def runtime_with_pending_hitl():
    rt = _runtime()
    bus = _RecordingBus()
    rt._event_bus.subscribe(None, bus._record)  # type: ignore[attr-defined]

    session_id, task_id = "s1", "t1"
    await _wire_pending_session(rt, session_id, task_id)

    req = await rt.hitl.open(
        HitlAsk(form="wait", delivery=UserTurnDelivery(task_id=task_id)),
        session_id=session_id, task_id=task_id, stage="tool",
    )
    return rt, bus, session_id, req.id


@pytest.fixture
async def runtime_without_hitl():
    rt = _runtime()
    bus = _RecordingBus()
    rt._event_bus.subscribe(None, bus._record)  # type: ignore[attr-defined]

    session_id, task_id = "s1", "t1"
    await _wire_pending_session(rt, session_id, task_id)
    return rt, bus, session_id


async def test_cancel_session_resolves_pending_hitl(runtime_with_pending_hitl):
    rt, bus, session_id, hitl_id = runtime_with_pending_hitl

    await rt.cancel_session(session_id)

    resolved = [e for e in bus.events if e.type == EventType.HITL_RESOLVED]
    assert len(resolved) == 1, "未决的 ask_user 没有被终局"
    assert resolved[0].payload["hitl_id"] == hitl_id
    assert resolved[0].payload["outcome"] == HITL_OUTCOME_CANCELLED
    assert rt.hitl_registry.list_pending(session_id=session_id) == []


@pytest.mark.skip(
    reason=(
        "依赖 Task 20 才会实现的 cancel_session 逐 agent 广播（Task 16 R20/进度台账）。"
        "本测试原意是钉住『HitlResolved 必须先于会话终态事件』的顺序纪律，与熔断 trip "
        "序列同一条约束；但会话状态机随 SessionManager 降格于 Task 15 整体退役后，"
        "TaskManager.cancel_all() 已不再发任何『会话终态』事件可供排序——它只发 "
        "TASK_CANCELED 并直接改写内存态 `self._session.status`（既不走 "
        "announce_queue_state()/TaskQueueDrained，也没有 SessionFinished 的下游消费者了；"
        "见 task_manager.py:cancel_all）。这条纪律要等 Task 20 给 cancel_session 补上"
        "逐 agent 的终局广播（AgentTerminated 之类）之后，才有正确的事件可以拿来重新"
        "钉这条『HITL 先于终局』的顺序——现在硬改会退化成『没抛异常』，验证强度不能接受。"
    )
)
async def test_hitl_cancelled_before_session_terminal(runtime_with_pending_hitl):
    """HitlResolved 必须先于 SessionFinished——与熔断 trip 序列同一条纪律。"""
    rt, bus, session_id, _ = runtime_with_pending_hitl

    await rt.cancel_session(session_id)

    types = [e.type for e in bus.events]
    assert EventType.HITL_RESOLVED in types
    assert EventType.SESSION_FINISHED in types
    assert types.index(EventType.HITL_RESOLVED) < types.index(EventType.SESSION_FINISHED), (
        "已取消会话的 HITL 终局事件晚于会话终态"
    )


async def test_cancel_session_without_pending_hitl_is_noop(runtime_without_hitl):
    """没有未决 HITL 时不该多发任何 HITL 事件。"""
    rt, bus, session_id = runtime_without_hitl
    await rt.cancel_session(session_id)
    assert [e for e in bus.events if e.type == EventType.HITL_RESOLVED] == []
