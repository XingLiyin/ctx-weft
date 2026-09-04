"""agent 侧的按需自愈入口（2026-09-04 spec §6.2）。

与 HITL 的 rebuild_hitl / rebuild_all_pending_hitl 一一对称：recover() 没跑过的
进程（测试、嵌入场景）也要能把 agent 面装填回来。

搭台手法照抄 `tests/unit/test_recover_loads_agents.py`——直接把事件写进 event
store，不跑一个真会话再模拟进程重启。
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


async def _crashed_session(rt, sid: str = "S1", root_agent_id: str = "agt_root") -> tuple[str, str]:
    """照 `test_recover_loads_agents.py::_seed_crashed_session` 的夹具：SessionCreated +
    一个 root agent（`AGENT_INSTANTIATED` 装填 template_id + `AGENT_WAITING_HUMAN` 装填
    status），带一条未决 HITL，让折出来的状态不是巧合默认值。
    """
    store = rt.event_store
    await store.append(_ev(1, sid, EventType.SESSION_CREATED,
                            template_id="tpl_x", root_agent_id=root_agent_id))
    await store.append(_ev(2, sid, EventType.AGENT_INSTANTIATED, agent_id=root_agent_id,
                            template_id="tpl_x"))
    await store.append(_ev(3, sid, EventType.HITL_REQUIRED, hitl_id=f"h_{sid}", form="question"))
    await store.append(_ev(4, sid, EventType.AGENT_WAITING_HUMAN, agent_id=root_agent_id))
    return sid, root_agent_id


async def test_rebuild_agent_populates_one_agent():
    rt = make_runtime(agent_provider=InlineAgentTemplateProvider())
    session_id, root_agent_id = await _crashed_session(rt)
    assert rt.list_agents() == []
    assert await rt.rebuild_agent(root_agent_id) is True
    assert rt.get_agent(root_agent_id).session_id == session_id


async def test_rebuild_agent_unknown_returns_false():
    """事件流里也找不到这个 agent —— 不抛，返回 False，调用方自己决定怎么报。

    种一个真实存在的 agent（root_agent_id），再问一个不在其中的 id，确保 False
    是「扫过、确实没有」而非「扫了个寂寞」。
    """
    rt = make_runtime(agent_provider=InlineAgentTemplateProvider())
    _, root_agent_id = await _crashed_session(rt)
    assert await rt.rebuild_agent("agt_nope") is False
    # 非空验证：扫描过程中真实找到了别的 agent，不是扫了个空事件流。
    assert any(a.agent_id == root_agent_id for a in rt.list_agents())


async def test_rebuild_agent_is_idempotent():
    rt = make_runtime(agent_provider=InlineAgentTemplateProvider())
    _, root_agent_id = await _crashed_session(rt)
    assert await rt.rebuild_agent(root_agent_id) is True
    assert await rt.rebuild_agent(root_agent_id) is True
    assert len([a for a in rt.list_agents() if a.agent_id == root_agent_id]) == 1


async def test_rebuild_all_agents_returns_total():
    rt = make_runtime(agent_provider=InlineAgentTemplateProvider())
    await _crashed_session(rt)
    n = await rt.rebuild_all_agents()
    assert n == len(rt.list_agents())
