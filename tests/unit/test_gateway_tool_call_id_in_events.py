"""Gateway 透传 tool_call_id 进 CAPABILITY_INVOKED / CAPABILITY_FINISHED，
供 host 把 pending/started/tool_call 三态按 tool_call_id 关联。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

from ctx_weft.core.events import EventType
from ctx_weft.core.events.bus import InProcessEventBus
from ctx_weft.core.loop.capability_gateway import CapabilityGateway
from ctx_weft.core.loop.driver import LoopContext, LoopState
from ctx_weft.core.orchestrator.capability_cache import CapabilityCache
from ctx_weft.protocols import MemoryAddress, ProviderContext
from ctx_weft.protocols.capability import (
    CapabilityEvent,
    CapabilityProviderInfo,
    ToolCapability,
    ToolCapabilityProvider,
)
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider


class _Echo(ToolCapabilityProvider):
    name = "mcp:a"

    def _cap(self) -> ToolCapability:
        return ToolCapability(id="mcp:a:search", name="search", description="s")

    async def list(self, ctx): return [self._cap()]
    async def retrieve(self, ctx): return [self._cap()]
    async def describe(self, ctx): return CapabilityProviderInfo(name=self.name, capability_count=1)

    def invoke(self, capability_id, arguments, ctx) -> AsyncIterator[CapabilityEvent]:
        async def _run():
            yield CapabilityEvent(kind="result", payload={"content": "ok"})
        return _run()

    async def cancel(self, invocation_id, ctx) -> None: return None


def _state_ctx():
    mem = InMemoryMemoryProvider()
    scope = MemoryAddress(session_id="s1", task_id="tsk_1", agent_id="agt_1")
    state = LoopState(
        run_id="r1", session=SimpleNamespace(id="s1", tenant_id="default"),
        task=SimpleNamespace(id="tsk_1"), agent=SimpleNamespace(id="agt_1", template_id="t"),
        scope=scope,
    )
    ctx = LoopContext(
        assembler=None, llm=None, memory=mem, event_bus=InProcessEventBus(),
        provider_ctx=ProviderContext(session_id="s1", tenant_id="default", task_id="tsk_1", agent_id="agt_1"),
    )
    return mem, state, ctx


async def test_invoke_threads_tool_call_id_into_both_events() -> None:
    p = _Echo()
    mem, state, ctx = _state_ctx()
    bus = InProcessEventBus()
    captured: list = []

    async def _collect(ev):
        captured.append(ev)

    bus.subscribe(None, _collect)

    cache = CapabilityCache()
    cache.put("agt_1", [p._cap()])
    gw = CapabilityGateway(
        capability_cache=cache, capability_providers=[p], memory=mem, event_bus=bus,
    )
    await gw.invoke("mcp__a__search", {"q": "x"}, state, ctx, tool_call_id="tc_abc")

    invoked = [e for e in captured if e.type == EventType.CAPABILITY_INVOKED]
    finished = [e for e in captured if e.type == EventType.CAPABILITY_FINISHED]
    assert invoked and invoked[0].payload.get("tool_call_id") == "tc_abc"
    assert finished and finished[0].payload.get("tool_call_id") == "tc_abc"
