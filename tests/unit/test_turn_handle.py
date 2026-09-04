"""TurnHandle：句柄以 agent + task 为轴（2026-09-04 spec §3）。

run 是引擎内部一轮循环的相关性 id，句柄的职责是「指着一个外部可寻址的对象」。
agent + task 已经够定位，events() 与 wait_for_finish() 都不需要 run_id——
把它放进句柄只会多一个无法诚实填写的字段。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from ctx_weft.core.runtime import SessionStartParams, TurnHandle
from ctx_weft.protocols import ToolCall
from ctx_weft.protocols.events import Event
from ctx_weft.providers.events.bus.in_process.bus import InProcessEventBus
from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from tests.integration.test_minimal_loop import (
    InlineAgentTemplateProvider,
    make_echo_template,
    make_runtime,
)

pytestmark = pytest.mark.asyncio


def _ev(**kw) -> Event:
    base = dict(
        id="evt_1", run_id="run_1", sequence=0, session_id="s1",
        type="TaskStarted", timestamp=datetime.now(UTC), origin="runtime",
        agent_id="agt_1", task_id="tsk_1",
    )
    base.update(kw)
    return Event(**base)


async def test_handle_has_no_run_id_field():
    """结构性守卫：run_id 不在对外句柄上。"""
    assert "run_id" not in TurnHandle.__dataclass_fields__


async def test_events_filters_by_agent_and_task():
    bus = InProcessEventBus()
    h = TurnHandle(session_id="s1", agent_id="agt_1", task_id="tsk_1",
                   template_id="tpl", event_bus=bus)
    got: list[str] = []

    async def _consume():
        async for ev in h.events():
            got.append(ev.id)
            if len(got) == 2:
                return

    t = asyncio.create_task(_consume())
    await asyncio.sleep(0)
    await bus.emit(_ev(id="e1"))
    await bus.emit(_ev(id="e2", agent_id="agt_2"))          # 别的 agent
    await bus.emit(_ev(id="e3", task_id="tsk_2"))           # 同 agent 别的 task
    await bus.emit(_ev(id="e4"))
    await asyncio.wait_for(t, timeout=2.0)

    assert got == ["e1", "e4"]


async def test_wait_for_finish_returns_on_task_terminal_event():
    bus = InProcessEventBus()
    h = TurnHandle(session_id="s1", agent_id="agt_1", task_id="tsk_1",
                   template_id="tpl", event_bus=bus, _state=None)
    waiter = asyncio.create_task(h.wait_for_finish(timeout=2.0))
    await asyncio.sleep(0)
    await bus.emit(_ev(id="e1", type="TaskFinished"))
    await asyncio.wait_for(waiter, timeout=2.0)


async def test_wait_for_finish_ignores_run_finished():
    """RunFinished 不再是判据——一轮 run 结束不等于这条 task 结束（可能还要 finalize）。"""
    bus = InProcessEventBus()
    h = TurnHandle(session_id="s1", agent_id="agt_1", task_id="tsk_1",
                   template_id="tpl", event_bus=bus)
    waiter = asyncio.create_task(h.wait_for_finish(timeout=0.3))
    await asyncio.sleep(0)
    await bus.emit(_ev(id="e1", type="RunFinished"))
    await asyncio.wait_for(waiter, timeout=2.0)   # 靠超时返回，不是靠 RunFinished


async def test_start_session_returns_turn_handle():
    """裁定 A（控制方 2026-09-04）：brief 原版 setup（`template_id="echo"` + 未注册
    memory provider）在本仓库跑不起来——`start_session` 会在模板解析阶段先炸。改用
    `test_runtime_agent_api.py::test_start_session_agent_id_is_addressable_root_agent`
    验证过的搭台手法：`template_id="agent:tpl_echo"`、注册 memory provider、LLM 回
    `control__finish_task` 让 run 能终结。
    """
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())
    llm = MockLLMAdapter(responses=[MockResponse(tool_calls=[
        ToolCall(id="tc1", name="control__finish_task", arguments={"result": "done"})])])
    rt = make_runtime(llm=llm, agent_provider=resolver)
    rt.providers.register_memory(InMemoryMemoryProvider())

    handle = await rt.start_session(SessionStartParams.create(
        template_id="agent:tpl_echo", user_prompt="hi", context_limit=100_000,
    ))
    await handle.wait_for_finish(timeout=5.0)

    assert isinstance(handle, TurnHandle)
    assert handle.agent_id and handle.task_id and handle.session_id and handle.template_id
