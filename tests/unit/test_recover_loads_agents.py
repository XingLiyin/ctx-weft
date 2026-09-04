"""冷启动装填 ALM + 恢复期 AGENT_* 现状广播（2026-09-04 spec §6.3 / §6.4）。

「恢复不是一种状态」的纪律保持：发的是从事件折出来的**现状**，不是新状态，
不引入 RECOVERING 之类的值域。

搭台手法照抄 `tests/unit/test_recover_routing.py`——直接把事件写进 event store
再调 `recover()`，不去跑一个真会话再模拟进程重启（`recover()` 的契约就是「只读
事件日志」，唯一需要搭的台就是事件日志本身，见该文件模块 docstring）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ctx_weft.protocols.events import Event, EventType
from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_runtime

pytestmark = pytest.mark.asyncio

_TS = datetime(2026, 6, 13, tzinfo=timezone.utc)


def _ev(seq: int, sid: str, type_: EventType, *, agent_id: str | None = None, **payload) -> Event:
    return Event(id=f"evt_{sid}_{seq:04d}", run_id="r1", sequence=seq, session_id=sid,
                 type=type_, timestamp=_TS, task_id="t1", agent_id=agent_id, payload=payload)


async def _seed_crashed_session(store, sid: str, root_agent_id: str) -> None:
    """写一个可 `recover()` 的会话事件流：SessionCreated + 一个 root agent，带一条未决 HITL。

    reducer 折 `AgentView` 靠的是 `AGENT_INSTANTIATED`（装填 template_id）+ 五态机
    状态事件（装填 status）——`_rebuild_agents` 只推算树形字段，两者都要种，否则
    `view.agents` 折不出这个 agent，见 `core/control/reducers.py`。这里额外种一条
    `HITL_REQUIRED`，让 root agent 折出来的 `waiting_human` 状态不是巧合：有一条真
    未决 HITL 挂着。
    """
    await store.append(_ev(1, sid, EventType.SESSION_CREATED,
                            template_id="tpl_x", root_agent_id=root_agent_id))
    await store.append(_ev(2, sid, EventType.AGENT_INSTANTIATED, agent_id=root_agent_id,
                            template_id="tpl_x"))
    await store.append(_ev(3, sid, EventType.HITL_REQUIRED, hitl_id=f"h_{sid}", form="question"))
    await store.append(_ev(4, sid, EventType.AGENT_WAITING_HUMAN, agent_id=root_agent_id))


async def test_recover_populates_agent_registry() -> None:
    """核心回归：重启后 list_agents 立刻看得见，不必等某条冷应答。"""
    rt = make_runtime(agent_provider=InlineAgentTemplateProvider())
    await _seed_crashed_session(rt.event_store, "S1", "agt_root")

    assert rt.list_agents(session_id="S1") == []      # 装填前
    await rt.recover()
    ids = {a.agent_id for a in rt.list_agents(session_id="S1")}
    assert "agt_root" in ids


async def test_recover_returns_agent_count_not_session_count() -> None:
    """报告单位换成 agent（spec §6.2）。

    两个 session：S1 挂 1 个 agent，S2 挂 2 个 agent（root + 一个额外子 agent）——
    agent 总数 3 与 session 数 2 刻意不相等，防止巧合数值让断言退化成没意义的检查。
    """
    rt = make_runtime(agent_provider=InlineAgentTemplateProvider())
    store = rt.event_store
    await _seed_crashed_session(store, "S1", "agt_root_1")
    await _seed_crashed_session(store, "S2", "agt_root_2")
    await store.append(_ev(5, "S2", EventType.AGENT_INSTANTIATED, agent_id="agt_child_2",
                            template_id="tpl_x"))
    await store.append(_ev(6, "S2", EventType.AGENT_IDLE, agent_id="agt_child_2"))

    n = await rt.recover()

    assert n == 3
    assert n == len(rt.list_agents())


async def test_get_agent_works_right_after_recover() -> None:
    rt = make_runtime(agent_provider=InlineAgentTemplateProvider())
    await _seed_crashed_session(rt.event_store, "S1", "agt_root")
    await rt.recover()
    d = rt.get_agent("agt_root")
    assert d.session_id == "S1"


async def test_load_broadcasts_current_status() -> None:
    """装填完按折出来的现状发 AGENT_*，host 投影因此不会停在崩溃前的状态。"""
    seen: list[Event] = []

    async def recorder(ev: Event) -> None:
        seen.append(ev)

    rt = make_runtime(agent_provider=InlineAgentTemplateProvider())
    rt.event_bus.subscribe(None, recorder)
    await _seed_crashed_session(rt.event_store, "S1", "agt_root")

    await rt.recover()

    agent_events = [e.type for e in seen if str(e.type).startswith("Agent")]
    assert any(t in agent_events for t in (
        EventType.AGENT_IDLE, EventType.AGENT_WAITING_HUMAN, EventType.AGENT_INTERRUPTED,
    )), f"恢复期没有 AGENT_* 现状广播: {agent_events}"


async def test_broadcast_carries_the_folded_status_not_a_default() -> None:
    """有未决 HITL 的 agent 恢复后必须是 waiting_human，不是被字段默认值重置成 idle。"""
    rt = make_runtime(agent_provider=InlineAgentTemplateProvider())
    await _seed_crashed_session(rt.event_store, "S1", "agt_root")
    await rt.recover()
    assert rt.get_agent("agt_root").status == "waiting_human"
