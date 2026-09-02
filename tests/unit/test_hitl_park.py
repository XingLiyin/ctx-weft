"""HitlPark 信号本身，以及 `_run_loop` 对它的处理（挂起而非失败）。

原先本文件还覆盖 `defer`、`HitlManager.wait/wait_for_decision` 的双出口、以及
approval 的冷路径短路。三者都随重设计消失或换了归属：

- `AuthorizationDecision.defer` 已删——它只能说「挂起」、说不出问什么，于是 authorizer
  必须自己先去登记请求（耦合的源头）。取代它的是 `needs_human: HitlAsk`，见
  `test_gateway_authz_hitl.py` 与 `test_authorizer_human_stateless.py`。
- `wait()` 抛 `HitlPark` / `wait_for_decision()` 返 `None` 这组双出口已合并成
  `HitlWaiter.wait()` 单一出口（驱逐返 `None`，翻译成 park 的权力只在 gateway）：
  `test_hitl_waiter.py` + `test_gateway_*_needs_human` 的驱逐用例。
- 冷决定短路：`test_gateway_authz_hitl.py::test_a_cached_decision_short_circuits_without_asking_again`
  与 `test_hitl_registry_load.py`。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


def test_hitl_park_is_base_exception_not_exception() -> None:
    from ctx_weft.core.loop.park import HitlPark
    assert issubclass(HitlPark, BaseException)
    assert not issubclass(HitlPark, Exception)


def test_hitl_park_carries_ids() -> None:
    from ctx_weft.core.loop.park import HitlPark
    p = HitlPark(hitl_id="hit_1", tool_call_id="tc1")
    assert p.hitl_id == "hit_1" and p.tool_call_id == "tc1"


async def test_run_loop_catches_park_returns_suspended() -> None:
    """_run_loop must catch HitlPark, set task SUSPENDED, and NOT raise (no FAILED)."""
    from collections.abc import AsyncIterator

    from ctx_weft.core import CtxWeftRuntime, ProviderRegistry
    from ctx_weft.protocols.events import EventType
    from ctx_weft.providers.events import InProcessEventBus
    from ctx_weft.core.loop.driver import LoopContext, LoopState, StepOutcome
    from ctx_weft.core.loop.park import HitlPark
    from ctx_weft.core.orchestrator.capability_cache import CapabilityCache
    from ctx_weft.core.state.models import Agent, LoopGuard, Session, Task
    from ctx_weft.protocols import (
        LoopConfig,
        MemoryConfig,
        MemoryAddress,
        ProviderContext,
    )
    from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
    from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_runtime

    # ── real objects ──────────────────────────────────────────────────────────
    session = Session(
        id="s_park_1",
        user_prompt="park test",
        status="RUNNING",
        tenant_id="default",
        root_agent_id="agt_p1",
    )
    task = Task(
        id="tsk_park_1",
        session_id="s_park_1",
        status="ACTIVE",
        tenant_id="default",
        assigned_agent_id="agt_p1",
    )
    agent = Agent(
        id="agt_p1",
        session_id="s_park_1",
        template_id="tpl_test",
        template_version="0.1",
        status="RUNNING",
        tenant_id="default",
        loop_guard=LoopGuard(),
        memory_config=MemoryConfig(),
        loop_config=LoopConfig(),
    )
    scope = MemoryAddress(session_id="s_park_1", task_id="tsk_park_1", agent_id="agt_p1")
    state = LoopState(run_id="run_p1", session=session, task=task, agent=agent, scope=scope)

    # ── stub driver that raises HitlPark immediately ──────────────────────────
    class _ParkingDriver:
        async def run(
            self, initial_state: LoopState, ctx: LoopContext
        ) -> AsyncIterator[StepOutcome]:
            raise HitlPark(hitl_id="req_park", tool_call_id="tc_park")
            yield  # make it an async generator

    # ── runtime wired with real bus + memory (no LLM needed) ─────────────────
    bus = InProcessEventBus()
    registry = ProviderRegistry()
    registry.register_memory(InMemoryMemoryProvider())

    rt = make_runtime(
        agent_provider=InlineAgentTemplateProvider(),
        providers=registry,
        event_store=None,  # uses InMemoryEventStore default
    )
    rt._event_bus = bus  # replace with observable bus

    # minimal LoopContext (only event_bus needed by _run_loop's make_event)
    loop_ctx = LoopContext(
        assembler=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        memory=InMemoryMemoryProvider(),
        event_bus=bus,
        provider_ctx=ProviderContext(
            session_id="s_park_1", tenant_id="default",
            task_id="tsk_park_1", agent_id="agt_p1",
        ),
        capability_cache=CapabilityCache(),
    )

    # ── collect events via handler (registered BEFORE run so no events are missed) ──
    collected: list = []

    async def _handler(ev) -> None:
        collected.append(ev)

    bus.subscribe(None, _handler)

    # ── call _run_loop; must NOT raise ────────────────────────────────────────
    final_state = await rt._run_loop(
        state=state,
        loop_ctx=loop_ctx,
        driver=_ParkingDriver(),  # type: ignore[arg-type]
        run_id="run_p1",
        initial_step="act",
        task=task,
        agent=agent,
    )

    # ── assertions ───────────────────────────────────────────────────────────
    assert task.status == "SUSPENDED", f"expected SUSPENDED, got {task.status!r}"

    run_finished = next((e for e in collected if e.type == EventType.RUN_FINISHED), None)
    assert run_finished is not None, "RUN_FINISHED not emitted"
    assert run_finished.payload["final_status"] == "SUSPENDED"
    assert run_finished.payload.get("error") is None

    task_failed_events = [e for e in collected if e.type == "TaskFailed"]
    assert task_failed_events == [], f"unexpected TaskFailed events: {task_failed_events}"

    # Phase 3：冷 park 也补发 TASK_SUSPENDED，使 task 投影状态 = 内存状态(SUSPENDED)，
    # 消除"投影停在 ACTIVE、与在等人脱节"的漂移。
    task_suspended = [e for e in collected if e.type == EventType.TASK_SUSPENDED]
    assert len(task_suspended) == 1, f"expected one TASK_SUSPENDED, got {len(task_suspended)}"
    assert task_suspended[0].task_id == "tsk_park_1"


def test_authorizer_filter_is_gone():
    """filter 零调用点，且对 HumanConfirmation 会真的发一个 HITL 请求并等人——是陷阱。"""
    from ctx_weft.protocols.capability import Authorizer
    assert not hasattr(Authorizer, "filter")
