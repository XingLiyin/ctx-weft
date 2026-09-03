from __future__ import annotations

import pytest

from ctx_weft.core.control.types import AgentView
from ctx_weft.core.orchestrator.agent_registry import AgentRegistry, _AgentRecord
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


def _reg() -> AgentRegistry:
    return AgentRegistry(
        template_lookup=None, event_bus=_SpyBus(), model_resolver=lambda a, m: None
    )


def _plant(reg: AgentRegistry, agent_id: str, parent: str | None, session_id: str = "s1") -> None:
    """直接种记录，绕开 instantiate 的模板依赖。"""
    reg._agents[agent_id] = _AgentRecord(
        session_id=session_id, tenant_id="default", template_id="tpl",
        parent_agent_id=parent, spawn_depth=0 if parent is None else 1,
        memory_config=None, loop_config=None,
    )
    if parent is not None:
        reg._children.setdefault(parent, set()).add(agent_id)


def test_record_defaults_to_idle_with_no_task():
    reg = _reg()
    _plant(reg, "root", None)
    assert reg.status_of("root") == "idle"
    assert reg._agents["root"].current_task_id is None


def test_children_and_descendants():
    reg = _reg()
    _plant(reg, "root", None)
    _plant(reg, "kid1", "root")
    _plant(reg, "kid2", "root")
    _plant(reg, "grandkid", "kid1")

    assert reg.children_of("root") == {"kid1", "kid2"}
    assert set(reg.descendants_of("root")) == {"kid1", "kid2", "grandkid"}
    assert reg.descendants_of("grandkid") == []


def test_descendants_tolerates_cycle():
    """防御性：父子关系理论上无环，索引损坏时也不能死循环。"""
    reg = _reg()
    _plant(reg, "a", None)
    _plant(reg, "b", "a")
    reg._children.setdefault("b", set()).add("a")
    assert set(reg.descendants_of("a")) == {"b"}


def test_agent_ids_of_session():
    reg = _reg()
    _plant(reg, "a", None, session_id="s1")
    _plant(reg, "b", None, session_id="s2")
    assert reg.agent_ids_of_session("s1") == ["a"]


def test_release_session_cleans_children_index():
    """release_session 之后 _children 不能留悬垂键，也不能留悬垂值。

    root/kid1/kid2/grandkid 都在 s1；额外种一个 s2 的 other，其 _children
    指向 s1 的 kid1（模拟索引本不该出现、但要能被安全清理的悬垂引用来源）。
    释放 s1 后：s1 的 agent 不能再作为键出现在 _children 里；也不能作为值
    残留在任何还活着的 agent（other）的 children 集合里。
    """
    reg = _reg()
    _plant(reg, "root", None, session_id="s1")
    _plant(reg, "kid1", "root", session_id="s1")
    _plant(reg, "kid2", "root", session_id="s1")
    _plant(reg, "grandkid", "kid1", session_id="s1")
    _plant(reg, "other", None, session_id="s2")
    reg._children.setdefault("other", set()).add("kid1")

    reg.release_session("s1")

    removed = {"root", "kid1", "kid2", "grandkid"}
    assert not (removed & set(reg._children.keys()))
    for children in reg._children.values():
        assert not (removed & children)
    assert reg.children_of("other") == set()
    assert reg.agent_ids_of_session("s1") == []
    assert reg.agent_ids_of_session("s2") == ["other"]


async def test_load_rebuilds_children_index_for_cold_recovery():
    """冷恢复（load）必须在没有任何 instantiate() 调用的情况下重建 _children。

    这钉住的性质：进程重启后，级联 cancel/pause（Task 19/20）依赖 _children
    对父子树做遍历——如果恢复期不重建索引，父被 load() 灌回来之后 cascade
    就是静默失效的（父存在、子存在，但父找不到子）。
    """
    reg = _reg()
    views = {
        "root": AgentView(id="root"),
        "kid1": AgentView(id="kid1", parent_agent_id="root", spawn_depth=1),
        "kid2": AgentView(id="kid2", parent_agent_id="root", spawn_depth=1),
        "grandkid": AgentView(id="grandkid", parent_agent_id="kid1", spawn_depth=2),
    }

    n = await reg.load(views, session_id="s1", tenant_id="default", fallback_template_id="tpl")

    assert n == 4
    assert reg.children_of("root") == {"kid1", "kid2"}
    assert set(reg.descendants_of("root")) == {"kid1", "kid2", "grandkid"}
    assert reg.descendants_of("grandkid") == []


async def test_load_tolerates_parent_outside_batch():
    """view.parent_agent_id 指向本次 load() 批次之外的 id（幽灵父/另一 session
    尚未加载）——不该崩，也不该让 descendants_of 在真实 agent 上查出脏结果。
    """
    reg = _reg()
    views = {"orphan": AgentView(id="orphan", parent_agent_id="not-in-this-batch", spawn_depth=1)}

    n = await reg.load(views, session_id="s1", tenant_id="default", fallback_template_id="tpl")

    assert n == 1
    assert reg.has("orphan")
    assert reg.children_of("not-in-this-batch") == {"orphan"}
    assert reg.descendants_of("orphan") == []


async def test_load_is_idempotent_for_children_index():
    """load() 对同一批 views 重复调用（比如 recover() 内部的重试路径），
    _children 不应重复计数或产生副本——set 语义天然幂等。
    """
    reg = _reg()
    views = {
        "root": AgentView(id="root"),
        "kid1": AgentView(id="kid1", parent_agent_id="root", spawn_depth=1),
    }
    await reg.load(views, session_id="s1", tenant_id="default", fallback_template_id="tpl")
    await reg.load(views, session_id="s1", tenant_id="default", fallback_template_id="tpl")

    assert reg.children_of("root") == {"kid1"}
