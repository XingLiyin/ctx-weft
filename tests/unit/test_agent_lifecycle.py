from __future__ import annotations

import pytest

from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.state.models import Task
from ctx_weft.protocols.events import (
    EVENT_TYPES,
    L_TIER_EVENT_TYPES,
    EventType,
)

pytestmark = pytest.mark.asyncio

_NEW_AGENT_TYPES = {
    EventType.AGENT_RUNNING,
    EventType.AGENT_IDLE,
    EventType.AGENT_WAITING_HUMAN,
    EventType.AGENT_INTERRUPTED,
    EventType.AGENT_TERMINATED,
}


class _SpyBus:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, ev) -> None:
        self.events.append(ev)

    def subscribe(self, _flt, _handler) -> None:
        pass


async def test_task_events_carry_agent_id_on_envelope():
    """V2 §0：envelope 管身份。ALM 靠 ev.agent_id 定位 agent。"""
    bus = _SpyBus()
    tm = TaskManager("s1", event_bus=bus)
    task = Task(id="t1", session_id="s1", status="PENDING", assigned_agent_id="a1")
    await tm.push_task(task)

    created = [e for e in bus.events if e.type == EventType.TASK_CREATED]
    assert created, "没有 TaskCreated"
    assert created[0].agent_id == "a1"


async def test_task_event_agent_id_prefers_running_agent_over_assigned():
    """在跑登记（真实执行者）优先于 task 自己的 assigned_agent_id。"""
    bus = _SpyBus()
    tm = TaskManager("s1", event_bus=bus)
    task = Task(id="t1", session_id="s1", status="PENDING", assigned_agent_id="a1")
    await tm.push_task(task)
    # 模拟：另一个 agent 实际在跑这个 task（登记与 assigned 不一致）
    tm._running_agents["t1"] = "a2"

    await tm._emit(EventType.TASK_STARTED, task_id="t1", payload={})

    started = [e for e in bus.events if e.type == EventType.TASK_STARTED]
    assert started
    assert started[-1].agent_id == "a2"


async def test_queue_level_event_has_no_agent_id():
    """TASK_QUEUE_* 是队列级聚合信号，无 task_id，不应乱填 agent_id。"""
    bus = _SpyBus()
    tm = TaskManager("s1", event_bus=bus)
    await tm._emit(EventType.TASK_QUEUE_DRAINED, payload={})

    drained = [e for e in bus.events if e.type == EventType.TASK_QUEUE_DRAINED]
    assert drained
    assert drained[0].agent_id is None
    assert drained[0].task_id is None


def test_new_agent_types_registered():
    """新增的 5 个 AGENT_* 状态事件应在 EVENT_TYPES 中注册。"""
    assert _NEW_AGENT_TYPES <= set(EVENT_TYPES)


def test_new_agent_types_are_s_tier():
    """不变式 2：加一个枚举值就得显式选边。ALM 状态被 reducer 折叠 -> S 档。"""
    assert not (_NEW_AGENT_TYPES & set(L_TIER_EVENT_TYPES))


def test_agent_event_wire_values_are_pascal_case():
    """验证 agent 状态事件的字符串值为 PascalCase。"""
    assert EventType.AGENT_RUNNING == "AgentRunning"
    assert EventType.AGENT_IDLE == "AgentIdle"
    assert EventType.AGENT_WAITING_HUMAN == "AgentWaitingHuman"
    assert EventType.AGENT_INTERRUPTED == "AgentInterrupted"
    assert EventType.AGENT_TERMINATED == "AgentTerminated"
