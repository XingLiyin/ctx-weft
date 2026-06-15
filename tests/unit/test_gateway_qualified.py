"""Gateway resolves by qualified name; no bare fallback; control classification."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

from loomex_core.core.events.bus import InProcessEventBus
from loomex_core.core.loop.capability_gateway import (
    DISPATCH_TOOLS,
    SILENT_TOOLS,
    CapabilityGateway,
)
from loomex_core.core.loop.driver import LoopContext, LoopState
from loomex_core.core.orchestrator.capability_cache import CapabilityCache
from loomex_core.protocols import MemoryScope, ProviderContext
from loomex_core.protocols.capability import (
    CapabilityEvent,
    CapabilityProviderInfo,
    ToolCapability,
    ToolCapabilityProvider,
)
from loomex_core.providers.memory_blackboard.in_memory import InMemoryMemoryProvider


class _Echo(ToolCapabilityProvider):
    name = "mcp:a"

    def __init__(self) -> None:
        self.invoked_with: str | None = None

    def _cap(self) -> ToolCapability:
        return ToolCapability(id="mcp:a:search", name="search", description="s")

    async def list(self, ctx): return [self._cap()]
    async def retrieve(self, ctx): return [self._cap()]
    async def describe(self, ctx): return CapabilityProviderInfo(name=self.name, capability_count=1)

    def invoke(self, capability_id, arguments, ctx) -> AsyncIterator[CapabilityEvent]:
        async def _run():
            self.invoked_with = capability_id
            yield CapabilityEvent(kind="result", payload={"content": "ok"})
        return _run()

    async def cancel(self, invocation_id, ctx) -> None: return None


def _state_ctx():
    mem = InMemoryMemoryProvider()
    scope = MemoryScope(session_id="s1", task_id="tsk_1", agent_id="agt_1")
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


def _gw(provider, mem):
    cache = CapabilityCache()
    cache.put("agt_1", [provider._cap()])
    return CapabilityGateway(
        capability_cache=cache, capability_providers=[provider],
        memory=mem, event_bus=InProcessEventBus(),
    )


async def test_resolves_qualified_and_dispatches_by_cap_id() -> None:
    p = _Echo()
    mem, state, ctx = _state_ctx()
    res = await _gw(p, mem).invoke("mcp__a__search", {"q": "x"}, state, ctx)
    assert res.is_error is False
    assert p.invoked_with == "mcp:a:search"  # execution dispatched by cap.id


async def test_bare_name_does_not_resolve() -> None:
    p = _Echo()
    mem, state, ctx = _state_ctx()
    res = await _gw(p, mem).invoke("search", {}, state, ctx)  # bare — no fallback
    assert res.is_error is True
    assert "unknown tool" in res.content
    assert p.invoked_with is None


def test_classification_sets_are_qualified() -> None:
    assert "control__delegate_task" in DISPATCH_TOOLS
    assert "control__finish_task" in SILENT_TOOLS
    assert "delegate_task" not in DISPATCH_TOOLS  # bare no longer matches
