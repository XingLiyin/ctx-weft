from __future__ import annotations

from datetime import UTC, datetime

from ctx_weft.protocols.events import Event, EventOrigin


def _ev(**kw) -> Event:
    base = dict(
        id="evt_1", run_id=None, sequence=0, session_id="s1",
        type="TaskStarted", timestamp=datetime.now(UTC),
    )
    base.update(kw)
    return Event(**base)


def test_origin_defaults_to_empty_string():
    """存量事件读出空串——§0 明说不做反推。"""
    assert _ev().origin == ""


def test_origin_round_trips():
    assert _ev(origin=EventOrigin.LOOP_ACT).origin == "loop.act"


def test_event_origin_has_17_values():
    assert len(EventOrigin.all()) == 17


def test_origin_values_are_two_level_dotted_or_bare():
    """§4：两级点号供 host 前缀匹配；分隔符用 . 不用 :（: 留给 capability id）。"""
    for v in EventOrigin.all():
        assert ":" not in v
        assert v.count(".") <= 1
        assert v == v.strip()


def test_loop_prefix_matches_all_loop_origins():
    loop = {v for v in EventOrigin.all() if v.startswith("loop.")}
    assert EventOrigin.LOOP_ACT in loop
    assert EventOrigin.LOOP_BACKGROUND_OBSERVE in loop
    assert EventOrigin.RUNTIME not in loop


# ── Task 2: LoopState.origin 管道与 make_event 默认取值 ──


from ctx_weft.core.loop.driver import make_event
from ctx_weft.protocols.events import EventType


class _FakeState:
    """make_event 只读这几个字段。"""
    def __init__(self, origin: str = ""):
        self.session_id = "s1"
        self.task_id = "t1"
        self.agent_id = "a1"
        self.run_id = "r1"
        self.tenant_id = "default"
        self.sequence_counter = 0
        self.origin = origin
        # 模拟 LoopState 中的 Session/Task/Agent 对象
        self.session = type('Session', (), {'id': 's1', 'tenant_id': 'default'})()
        self.task = type('Task', (), {'id': 't1'})()
        self.agent = type('Agent', (), {'id': 'a1'})()


def test_make_event_takes_origin_from_state():
    """§4 填充方式 1：40+ 个循环内发射点零改动。"""
    ev = make_event(_FakeState(origin=EventOrigin.LOOP_ACT), EventType.ACT_TURN_STARTED, {})
    assert ev.origin == "loop.act"


def test_make_event_explicit_origin_overrides_state():
    """§4 填充方式 2：给 background observe 这类脱离主 driver 序列的场景。"""
    ev = make_event(
        _FakeState(origin=EventOrigin.LOOP_OBSERVE),
        EventType.LLM_PROMPT_SENT,
        {},
        origin=EventOrigin.LOOP_BACKGROUND_OBSERVE,
    )
    assert ev.origin == "loop.background_observe"


# ── Task 3: 循环外发射者补 origin + 端到端不变式 ──

import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.runtime import SessionStartParams
from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from tests.integration.test_minimal_loop import (
    InlineAgentTemplateProvider, make_echo_template, make_runtime,
)


@pytest.mark.asyncio
async def test_every_emitted_event_has_nonempty_origin():
    """docs/events-v2.md §6 外加条：所有发射出的事件 origin 非空。

    本任务（Task 3）只管循环外 5 个发射者（session_manager/task_manager/
    hitl.service/agent_registry/runtime）；`start_session` 只同步跑到「登记 +
    起 drain 后台任务」为止就返回（真正的 loop 执行经
    `asyncio.create_task(task_manager.drain())` 甩到后台，本测试不等它），故这里
    收到的是 SessionCreated / AgentInstantiated / TaskCreated 一类会话建立期的
    事件——循环内 40+ 发射点（含尚未在本任务范围内收敛的 RecognizeIntent 等）由
    Task 2 与后续 Phase B 覆盖，不在本测试断言范围。
    """
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())
    llm = MockLLMAdapter(responses=[MockResponse(text="hi")])
    rt: CtxWeftRuntime = make_runtime(llm=llm, agent_provider=resolver)
    rt.providers.register_memory(InMemoryMemoryProvider())

    seen: list[tuple[str, str]] = []

    async def _spy(ev):
        seen.append((ev.type, ev.origin))

    rt._event_bus.subscribe(None, _spy)

    await rt.start_session(SessionStartParams.create(
        template_id="agent:tpl_echo",
        user_prompt="hi",
        initial_task=None,
        context_limit=8000,
    ))

    assert seen, "没有采集到任何事件"
    blank = sorted({t for t, o in seen if not o})
    assert blank == [], f"这些事件类型的 origin 为空：{blank}"


# ── Task 3 / R7: SQL EventStore 往返持久化 origin ──


async def test_sql_event_store_round_trips_origin(tmp_path):
    from ctx_weft.providers.events.store.sql import open_sqlite_event_store

    async with open_sqlite_event_store(tmp_path / "events.db") as store:
        ev = _ev(id="evt_a", type="TaskStarted", origin=EventOrigin.LOOP_ACT)
        await store.append(ev)
        loaded = await store.read_by_session("s1")
        assert len(loaded) == 1
        assert loaded[0].origin == "loop.act"


async def test_sql_event_store_round_trips_blank_origin(tmp_path):
    from ctx_weft.providers.events.store.sql import open_sqlite_event_store

    async with open_sqlite_event_store(tmp_path / "events.db") as store:
        ev = _ev(id="evt_b", type="TaskStarted")  # origin 默认 ""
        await store.append(ev)
        loaded = await store.read_by_session("s1")
        assert len(loaded) == 1
        assert loaded[0].origin == ""
