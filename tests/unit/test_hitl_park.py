"""HITL park 信号 + 热→冷驱逐 + approval 冷路径（spec/07 §5/§7/§8）。"""

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


def test_authorization_decision_has_defer_default_false() -> None:
    from ctx_weft.core.auth.authorizer import AuthorizationDecision
    assert AuthorizationDecision(allowed=True).defer is False


async def test_gateway_defer_raises_park_and_skips_provider() -> None:
    from types import SimpleNamespace
    from collections.abc import AsyncIterator
    from ctx_weft.core.auth import AuthorizationDecision, Authorizer
    from ctx_weft.core.events.bus import InProcessEventBus
    from ctx_weft.core.loop.capability_gateway import CapabilityGateway
    from ctx_weft.core.loop.driver import LoopContext, LoopState
    from ctx_weft.core.loop.park import HitlPark
    from ctx_weft.core.orchestrator.capability_cache import CapabilityCache
    from ctx_weft.protocols import MemoryScope, ProviderContext
    from ctx_weft.protocols.capability import (
        CapabilityEvent, CapabilityProviderInfo, ToolCapability, ToolCapabilityProvider)
    from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

    cap = ToolCapability(id="test:echo", name="echo", description="e")

    class _Prov(ToolCapabilityProvider):
        name = "test"
        def __init__(self): self.invoked = False
        async def list(self, ctx): return [cap]
        async def retrieve(self, ctx): return [cap]
        async def describe(self, ctx): return CapabilityProviderInfo(name=self.name, capability_count=1)
        def invoke(self, cid, args, ctx) -> AsyncIterator[CapabilityEvent]: return self._run()
        async def _run(self):
            self.invoked = True
            yield CapabilityEvent(kind="result", payload={"content": "x"})
        async def cancel(self, iid, ctx): return None

    class _DeferAuth(Authorizer):
        async def authorize(self, capability, agent, task, ctx, arguments=None, *, tool_call_id=""):
            return AuthorizationDecision(allowed=False, defer=True)

    prov = _Prov()
    cache = CapabilityCache(); cache.put("agt_1", [cap])
    gw = CapabilityGateway(capability_cache=cache, capability_providers=[prov],
                           memory=InMemoryMemoryProvider(), event_bus=InProcessEventBus(),
                           provider_authorizers={"test:echo": _DeferAuth()})
    agent = SimpleNamespace(id="agt_1", template_id="tmpl_a", session_id="s1")
    session = SimpleNamespace(id="s1", tenant_id="default")
    task = SimpleNamespace(id="tsk_1")
    scope = MemoryScope(session_id="s1", task_id="tsk_1", agent_id="agt_1")
    state = LoopState(run_id="r1", session=session, task=task, agent=agent, scope=scope)
    ctx = LoopContext(assembler=None, llm=None, memory=InMemoryMemoryProvider(),
                      event_bus=InProcessEventBus(),
                      provider_ctx=ProviderContext(session_id="s1", tenant_id="default",
                                                   task_id="tsk_1", agent_id="agt_1"))
    with pytest.raises(HitlPark):
        await gw.invoke("test__echo", {"text": "hi"}, state, ctx, tool_call_id="tcZ")
    assert prov.invoked is False


async def test_run_loop_catches_park_returns_suspended() -> None:
    """_run_loop must catch HitlPark, set task SUSPENDED, and NOT raise (no FAILED)."""
    from collections.abc import AsyncIterator

    from ctx_weft.core import CtxWeftRuntime, ProviderRegistry
    from ctx_weft.core.events import EventType
    from ctx_weft.core.events.bus import InProcessEventBus
    from ctx_weft.core.loop.driver import LoopContext, LoopState, StepOutcome
    from ctx_weft.core.loop.park import HitlPark
    from ctx_weft.core.orchestrator.capability_cache import CapabilityCache
    from ctx_weft.core.state.models import Agent, LoopGuard, Session, Task
    from ctx_weft.protocols import (
        LoopConfig,
        MemoryConfig,
        MemoryScope,
        ProviderContext,
    )
    from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider
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
    scope = MemoryScope(session_id="s_park_1", task_id="tsk_park_1", agent_id="agt_p1")
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


async def test_timeout_evicts_to_cold_keeps_pending() -> None:
    from ctx_weft.core.orchestrator.hitl_manager import HitlManager
    from ctx_weft.core.loop.park import HitlPark
    mgr = HitlManager(timeout_sec=0)
    rid = await mgr.request(form="question", session_id="s1", task_id="t1", tool_call_id="tc1")
    with pytest.raises(HitlPark):
        await mgr.wait(rid)
    assert mgr.get(rid).status == "pending"        # 仍 pending（hot→cold，不是 timeout 终态）
    assert mgr.list_pending()
    resolved, was_hot = await mgr.resolve_answer(rid, "late answer")
    assert resolved.status == "accepted" and was_hot is False


async def test_answer_before_timeout_is_hot_and_wins() -> None:
    import asyncio
    from ctx_weft.core.orchestrator.hitl_manager import HitlManager
    mgr = HitlManager(timeout_sec=None)             # never times out
    rid = await mgr.request(form="question", session_id="s1", task_id="t1", tool_call_id="tc1")
    waiter = asyncio.create_task(mgr.wait(rid))
    await asyncio.sleep(0)
    resolved, was_hot = await mgr.resolve_answer(rid, "answered")
    assert was_hot is True
    assert (await waiter).status == "accepted"


async def test_authorize_cold_uses_resolved_decision_no_new_hitl() -> None:
    from types import SimpleNamespace
    from ctx_weft.core.auth import HumanConfirmationAuthorizer
    from ctx_weft.core.orchestrator.hitl_manager import HitlManager
    from ctx_weft.protocols import ProviderContext
    from ctx_weft.protocols.capability import ToolCapability

    mgr = HitlManager()
    rid = await mgr.request(form="approval", session_id="s1", task_id="t1", tool_call_id="tcZ")
    await mgr.approve(rid, modified_arguments={"command": "ls -la"})

    authz = HumanConfirmationAuthorizer(hitl_manager=mgr)
    cap = ToolCapability(id="fs:bash_exec", name="bash_exec", description="run")
    agent = SimpleNamespace(id="a1", template_id="t", session_id="s1")
    d = await authz.authorize(cap, agent, SimpleNamespace(id="t1"),
                              ProviderContext(session_id="s1", tenant_id="default"),
                              {"command": "ls"}, tool_call_id="tcZ")
    assert d.allowed and d.modified_arguments == {"command": "ls -la"}
    assert len(mgr.list_pending()) == 0     # 未新建


async def test_authorize_cold_no_future_does_not_keyerror() -> None:
    """restart 后：rebuild_pending(无 future) + 冷 resolve → authorize 必须短路（否则 wait() KeyError）。"""
    from types import SimpleNamespace
    from ctx_weft.core.auth import HumanConfirmationAuthorizer
    from ctx_weft.core.state.models import HitlRequest
    from ctx_weft.core.orchestrator.hitl_manager import HitlManager
    from ctx_weft.protocols import ProviderContext
    from ctx_weft.protocols.capability import ToolCapability

    mgr = HitlManager()
    # 模拟 restart：从 view 重建 pending（无 future）
    mgr.rebuild_pending({
        "hit_1": HitlRequest(id="hit_1", form="approval", session_id="s1",
                             task_id="t1", capability_id="fs:bash_exec", tool_call_id="tcR"),
    })
    # 冷应答（无 future → was_hot False）
    _resolved, was_hot = await mgr.resolve_approve("hit_1", modified_arguments={"command": "ls -la"})
    assert was_hot is False

    authz = HumanConfirmationAuthorizer(hitl_manager=mgr)
    cap = ToolCapability(id="fs:bash_exec", name="bash_exec", description="run")
    agent = SimpleNamespace(id="a1", template_id="t", session_id="s1")
    # reconcile 再入：必须用缓存决定、不调 wait()（否则 KeyError：无 future）
    d = await authz.authorize(cap, agent, SimpleNamespace(id="t1"),
                              ProviderContext(session_id="s1", tenant_id="default"),
                              {"command": "ls"}, tool_call_id="tcR")
    assert d.allowed and d.modified_arguments == {"command": "ls -la"}
