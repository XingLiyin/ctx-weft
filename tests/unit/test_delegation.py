"""委派回填：origin_tool_call_id 透传到 child（spec/06 §5）。

锁定：
- delegate_task / delegate_plan 把发起调用的 tool_call_id 写到每个 child.origin_tool_call_id
- 经 ControlCapabilityProvider 调用时，tool_call_id 经 ProviderContext.extra → ControlContext 透传
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

from loomex_core.core.events.bus import InProcessEventBus
from loomex_core.core.loop.capability_gateway import CapabilityGateway
from loomex_core.core.loop.driver import LoopContext, LoopState
from loomex_core.core.orchestrator.capability_cache import CapabilityCache
from loomex_core.core.orchestrator.control_capability import (
    PROVIDER_NAME,
    ControlCapabilityProvider,
    ControlContext,
    delegate_plan,
    delegate_task,
)
from loomex_core.core.state.models import Session, Task
from loomex_core.protocols import MemoryEventType, MemoryScope, ProviderContext
from loomex_core.protocols.capability import (
    CapabilityEvent,
    CapabilityProviderInfo,
    ToolCapability,
    ToolCapabilityProvider,
)
from loomex_core.providers.memory_blackboard.in_memory import InMemoryMemoryProvider


class _FakeTM:
    def __init__(self) -> None:
        self.staged: list[Task] = []

    def stage_task(self, child: Task, **kwargs) -> None:
        self.staged.append(child)

    def get_task(self, tid: str):
        return None


def _parent() -> Task:
    return Task(id="p1", session_id="s1", status="ACTIVE", title="P")


def _ctx(tm: _FakeTM, tool_call_id: str) -> ControlContext:
    return ControlContext(
        session_id="s1", task_id="p1", agent_id="a1", task=_parent(),
        task_manager=tm, session=None, tool_call_id=tool_call_id,
    )


def test_delegate_task_records_origin_tool_call_id() -> None:
    tm = _FakeTM()
    delegate_task(title="child", task_prompt="do it", ctx=_ctx(tm, "tc_42"))
    assert len(tm.staged) == 1
    assert tm.staged[0].origin_tool_call_id == "tc_42"
    assert tm.staged[0].parent_task_id == "p1"


def test_delegate_plan_records_origin_on_all_children() -> None:
    tm = _FakeTM()
    delegate_plan(tasks=[{"title": "a"}, {"title": "b"}], ctx=_ctx(tm, "tc_99"))
    assert [c.origin_tool_call_id for c in tm.staged] == ["tc_99", "tc_99"]


@pytest.mark.asyncio
async def test_tool_call_id_threads_through_provider_extra() -> None:
    """gateway 把 tool_call_id 放进 ProviderContext.extra → _handle → ControlContext。"""
    tm = _FakeTM()
    parent = _parent()
    tm.get_task = lambda tid: parent  # type: ignore[assignment]
    session = Session(id="s1", tenant_id="default", user_prompt="x", status="RUNNING")
    provider = ControlCapabilityProvider()
    provider.register_session("s1", tm, session)

    ctx = ProviderContext(
        session_id="s1", tenant_id="default", task_id="p1", agent_id="a1",
        extra={"tool_call_id": "tc_thread"},
    )
    async for _ev in provider.invoke(f"{PROVIDER_NAME}:delegate_task", {"title": "c", "task_prompt": "p"}, ctx):
        pass

    assert len(tm.staged) == 1
    assert tm.staged[0].origin_tool_call_id == "tc_thread"


# ── gateway 派发路由：TASK_DISPATCH(agent 层) 写入、即时 TOOL_RESULT 暂挂 ──────────────


class _DispatchProvider(ToolCapabilityProvider):
    name = "control"

    def _cap(self) -> ToolCapability:
        return ToolCapability(id="control:delegate_task", name="delegate_task", description="dispatch")

    async def list(self, ctx):
        return [self._cap()]

    async def retrieve(self, ctx):
        return [self._cap()]

    async def describe(self, ctx):
        return CapabilityProviderInfo(name=self.name, capability_count=1)

    def invoke(self, capability_id, arguments, ctx) -> AsyncIterator[CapabilityEvent]:
        async def _run():
            yield CapabilityEvent(kind="result", payload={"content": "Sub-task scheduled."})
        return _run()

    async def cancel(self, invocation_id, ctx) -> None:
        return None


@pytest.mark.asyncio
async def test_gateway_dispatch_writes_task_dispatch_not_tool_result() -> None:
    mem = InMemoryMemoryProvider()
    cache = CapabilityCache()
    cap = ToolCapability(id="control:delegate_task", name="delegate_task", description="dispatch")
    cache.put("agt_1", [cap])
    gw = CapabilityGateway(
        capability_cache=cache,
        capability_providers=[_DispatchProvider()],
        memory=mem,
        event_bus=InProcessEventBus(),
    )
    scope = MemoryScope(session_id="s1", task_id="tsk_1", agent_id="agt_1")
    state = LoopState(
        run_id="run_1",
        session=SimpleNamespace(id="s1", tenant_id="default"),
        task=SimpleNamespace(id="tsk_1"),
        agent=SimpleNamespace(id="agt_1", template_id="t"),
        scope=scope,
    )
    ctx = LoopContext(
        assembler=None, llm=None, memory=mem, event_bus=InProcessEventBus(),
        provider_ctx=ProviderContext(session_id="s1", tenant_id="default", task_id="tsk_1", agent_id="agt_1"),
    )

    await gw.invoke("control__delegate_task", {"title": "c"}, state, ctx, tool_call_id="tc_d")

    pctx = ProviderContext(session_id="s1", tenant_id="default")
    dispatched = await mem.recall_recent(scope, [MemoryEventType.TASK_DISPATCH], 10, pctx)
    tool_results = await mem.recall_recent(scope, [MemoryEventType.TOOL_RESULT], 10, pctx)
    assert len(dispatched) == 1
    assert dispatched[0].metadata.get("tool_call_id") == "tc_d"
    assert tool_results == []  # 即时 result 暂挂，不写 task 层


class _AssessProvider(ToolCapabilityProvider):
    name = "control"

    def _cap(self) -> ToolCapability:
        return ToolCapability(id="control:report_task_outcome",
                              name="report_task_outcome", description="verdict")

    async def list(self, ctx):
        return [self._cap()]

    async def retrieve(self, ctx):
        return [self._cap()]

    async def describe(self, ctx):
        return CapabilityProviderInfo(name=self.name, capability_count=1)

    def invoke(self, capability_id, arguments, ctx) -> AsyncIterator[CapabilityEvent]:
        async def _run():
            yield CapabilityEvent(kind="result", payload={"content": "human reply"})
        return _run()

    async def cancel(self, invocation_id, ctx) -> None:
        return None


@pytest.mark.asyncio
async def test_gateway_silent_tool_writes_nothing_to_task_layer() -> None:
    """report_task_outcome（编排/裁决）不入 task 对话——其 HITL 回复改由 finalize 注入。"""
    mem = InMemoryMemoryProvider()
    cache = CapabilityCache()
    cap = ToolCapability(id="control:report_task_outcome",
                         name="report_task_outcome", description="verdict")
    cache.put("agt_1", [cap])
    gw = CapabilityGateway(
        capability_cache=cache, capability_providers=[_AssessProvider()],
        memory=mem, event_bus=InProcessEventBus(),
    )
    scope = MemoryScope(session_id="s1", task_id="tsk_1", agent_id="agt_1")
    state = LoopState(
        run_id="r1",
        session=SimpleNamespace(id="s1", tenant_id="default"),
        task=SimpleNamespace(id="tsk_1"),
        agent=SimpleNamespace(id="agt_1", template_id="t"),
        scope=scope,
    )
    ctx = LoopContext(
        assembler=None, llm=None, memory=mem, event_bus=InProcessEventBus(),
        provider_ctx=ProviderContext(session_id="s1", tenant_id="default", task_id="tsk_1", agent_id="agt_1"),
    )

    await gw.invoke("control__report_task_outcome", {"x": 1}, state, ctx, tool_call_id="tc_a")

    pctx = ProviderContext(session_id="s1", tenant_id="default")
    inv = await mem.recall_recent(scope, [MemoryEventType.TOOL_INVOCATION], 10, pctx)
    res = await mem.recall_recent(scope, [MemoryEventType.TOOL_RESULT], 10, pctx)
    assert inv == [] and res == []  # task 对话零污染
