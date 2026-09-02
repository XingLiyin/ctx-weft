"""RunFinished 说的是 run 自己的结局，不是 task 的状态（Task 3）。

搭台手法：不用 brief 骨架里假想的 `outage_case` / `park_case` / `normal_case`
fixture（仓里不存在），改用与 Task 2 的 `test_loop_reports_outcome.py` 同源的
既有手法——`test_run_loop_outage.py`（`run_single_task` + spy `emit`）与
`test_hitl_park.py`（`_run_loop` + stub driver 直调 + `bus.subscribe`）。
"""

from __future__ import annotations

import pytest

from ctx_weft.protocols.events import EventType

pytestmark = pytest.mark.asyncio


async def _events_from_outage() -> list:
    """同 test_run_loop_outage.py::test_outage_marks_session_interrupted_not_failed 的搭台。"""
    from ctx_weft.core.config import RuntimeConfig
    from ctx_weft.protocols import LLMOutageError
    from ctx_weft.providers.llm.mock import MockLLMAdapter
    from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
    from tests.integration.test_minimal_loop import (
        InlineAgentTemplateProvider,
        make_echo_template,
        make_runtime,
    )

    class _OutageLLM(MockLLMAdapter):
        def complete(self, request, stream=True):
            self.last_request = request

            async def _gen():
                raise LLMOutageError("simulated outage exhausted")
                yield  # pragma: no cover

            return _gen()

    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())
    runtime = make_runtime(
        llm=_OutageLLM(responses=[]),
        agent_provider=resolver,
        config=RuntimeConfig(llm_self_heal_max_attempts=1),
    )
    runtime.providers.register_memory(InMemoryMemoryProvider())

    seen: list = []
    orig_emit = runtime._event_bus.emit

    async def _spy(ev):
        seen.append(ev)
        return await orig_emit(ev)

    runtime._event_bus.emit = _spy

    await runtime.run_single_task(template_id="agent:tpl_echo", user_prompt="hi")
    return seen


async def _events_from_park() -> list:
    """同 test_hitl_park.py::test_run_loop_catches_park_returns_awaiting_human 的搭台。"""
    from collections.abc import AsyncIterator

    from ctx_weft.core import ProviderRegistry
    from ctx_weft.core.loop.driver import LoopContext, LoopState, StepOutcome
    from ctx_weft.core.loop.park import HitlPark
    from ctx_weft.core.orchestrator.capability_cache import CapabilityCache
    from ctx_weft.core.state.models import Agent, LoopGuard, Session, Task
    from ctx_weft.protocols import LoopConfig, MemoryAddress, MemoryConfig, ProviderContext
    from ctx_weft.providers.events import InProcessEventBus
    from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
    from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_runtime

    session = Session(id="s_park_3", user_prompt="park test", status="RUNNING",
                      tenant_id="default", root_agent_id="agt_p3")
    task = Task(id="tsk_park_3", session_id="s_park_3", status="ACTIVE",
               tenant_id="default", assigned_agent_id="agt_p3")
    agent = Agent(id="agt_p3", session_id="s_park_3", template_id="tpl_test",
                 template_version="0.1", status="RUNNING", tenant_id="default",
                 loop_guard=LoopGuard(), memory_config=MemoryConfig(),
                 loop_config=LoopConfig())
    scope = MemoryAddress(session_id="s_park_3", task_id="tsk_park_3", agent_id="agt_p3")
    state = LoopState(run_id="run_p3", session=session, task=task, agent=agent, scope=scope)

    class _ParkingDriver:
        async def run(
            self, initial_state: LoopState, ctx: LoopContext,
        ) -> AsyncIterator[StepOutcome]:
            raise HitlPark(hitl_id="req_park_3", tool_call_id="tc_park_3")
            yield

    bus = InProcessEventBus()
    registry = ProviderRegistry()
    registry.register_memory(InMemoryMemoryProvider())

    rt = make_runtime(agent_provider=InlineAgentTemplateProvider(), providers=registry,
                      event_store=None)
    rt._event_bus = bus

    loop_ctx = LoopContext(
        assembler=None, llm=None, memory=InMemoryMemoryProvider(), event_bus=bus,
        provider_ctx=ProviderContext(session_id="s_park_3", tenant_id="default",
                                     task_id="tsk_park_3", agent_id="agt_p3"),
        capability_cache=CapabilityCache(),
    )

    collected: list = []

    async def _handler(ev) -> None:
        collected.append(ev)

    bus.subscribe(None, _handler)

    await rt._run_loop(state=state, loop_ctx=loop_ctx, driver=_ParkingDriver(),
                       run_id="run_p3", initial_step="act", task=task, agent=agent)
    return collected


async def _events_from_normal() -> list:
    """同 test_minimal_loop.py::test_minimal_echo_loop 的搭台，加 spy 收事件。"""
    from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
    from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
    from tests.integration.test_minimal_loop import (
        InlineAgentTemplateProvider,
        make_echo_template,
        make_runtime,
    )

    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())
    llm = MockLLMAdapter(responses=[MockResponse(text="Hello! You said: say hello")])
    runtime = make_runtime(llm=llm, agent_provider=resolver)
    runtime.providers.register_memory(InMemoryMemoryProvider())

    seen: list = []
    orig_emit = runtime._event_bus.emit

    async def _spy(ev):
        seen.append(ev)
        return await orig_emit(ev)

    runtime._event_bus.emit = _spy

    await runtime.run_single_task(template_id="agent:tpl_echo", user_prompt="say hello")
    return seen


async def test_run_finished_carries_the_run_outcome_not_task_status() -> None:
    events = await _events_from_outage()
    ev = next(e for e in events if e.type == EventType.RUN_FINISHED)
    assert ev.payload["outcome"] == "interrupted"


async def test_park_run_reports_awaiting_human() -> None:
    events = await _events_from_park()
    ev = next(e for e in events if e.type == EventType.RUN_FINISHED)
    assert ev.payload["outcome"] == "awaiting_human"


async def test_normal_run_reports_completed() -> None:
    events = await _events_from_normal()
    ev = next(e for e in events if e.type == EventType.RUN_FINISHED)
    assert ev.payload["outcome"] == "completed"


async def test_will_retry_is_unchanged() -> None:
    """will_retry 是给 host 决定关不关流的，语义不变。"""
    events = await _events_from_normal()
    ev = next(e for e in events if e.type == EventType.RUN_FINISHED)
    assert "will_retry" in ev.payload


async def test_final_status_key_still_present_but_deprecated() -> None:
    """`final_status` 保留一个发布周期，值仍是 task.status，供既有断言过渡。"""
    events = await _events_from_park()
    ev = next(e for e in events if e.type == EventType.RUN_FINISHED)
    assert ev.payload["final_status"] == "AWAITING_HUMAN"
