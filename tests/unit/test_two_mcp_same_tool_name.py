"""Two providers exposing the same bare tool name coexist and route independently."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

from ctx_weft.core.assembler.sources.capability import CapabilitySource
from ctx_weft.providers.events import InProcessEventBus
from ctx_weft.core.loop.capability_gateway import CapabilityGateway
from ctx_weft.core.loop.driver import LoopContext, LoopState
from ctx_weft.core.orchestrator.capability_cache import CapabilityCache
from ctx_weft.core.utils import estimate_tokens
from ctx_weft.protocols import MemoryAddress, ProviderContext
from ctx_weft.protocols.capability import (
    CapabilityEvent, CapabilityProviderInfo, ToolCapability, ToolCapabilityProvider,
)
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider


class _Server(ToolCapabilityProvider):
    def __init__(self, server: str) -> None:
        self.name = f"mcp:{server}"
        self._server = server
        self.hit = False

    def _cap(self) -> ToolCapability:
        return ToolCapability(id=f"mcp:{self._server}:search", name="search", description="s")

    async def list(self, ctx): return [self._cap()]
    async def retrieve(self, ctx): return [self._cap()]
    async def describe(self, ctx): return CapabilityProviderInfo(name=self.name, capability_count=1)

    def invoke(self, capability_id, arguments, ctx) -> AsyncIterator[CapabilityEvent]:
        async def _run():
            self.hit = True
            yield CapabilityEvent(kind="result", payload={"content": self._server})
        return _run()

    async def cancel(self, invocation_id, ctx) -> None: return None


async def test_two_servers_same_tool_name_coexist_and_route() -> None:
    a, b = _Server("a"), _Server("b")

    # 1. Cache no longer raises on the shared bare name.
    cache = CapabilityCache()
    cache.put("agt_1", [a._cap(), b._cap()])

    # 2. The LLM sees two distinct qualified names.
    request = SimpleNamespace(bound_capabilities=[a._cap(), b._cap()], purpose="act",
                              token_counter=estimate_tokens)
    names = {blk.metadata["llm_tool"].name async for blk in CapabilitySource().fetch(request, deps=None)}
    assert names == {"mcp__a__search", "mcp__b__search"}

    # 3. Each qualified name routes to its own provider.
    mem = InMemoryMemoryProvider()
    scope = MemoryAddress(session_id="s1", task_id="tsk_1", agent_id="agt_1")
    state = LoopState(
        run_id="r1", session=SimpleNamespace(id="s1", tenant_id="default"),
        task=SimpleNamespace(id="tsk_1"), agent=SimpleNamespace(id="agt_1", template_id="t"),
        scope=scope,
        resolved_model=SimpleNamespace(model="mock", account=""),
    )
    ctx = LoopContext(
        assembler=None, llm=None, memory=mem, event_bus=InProcessEventBus(),
        provider_ctx=ProviderContext(session_id="s1", tenant_id="default", task_id="tsk_1", agent_id="agt_1"),
    )
    gw = CapabilityGateway(
        capability_cache=cache, capability_providers=[a, b],
        memory=mem, event_bus=InProcessEventBus(),
    )
    res = await gw.invoke("mcp__b__search", {}, state, ctx)
    assert res.content == "b" and b.hit and not a.hit
