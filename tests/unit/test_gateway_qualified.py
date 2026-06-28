"""Gateway resolves by qualified name; no bare fallback; control classification."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

from ctx_weft.core.events.bus import InProcessEventBus
from ctx_weft.core.loop.capability_gateway import (
    DISPATCH_TOOLS,
    SILENT_TOOLS,
    CapabilityGateway,
)
from ctx_weft.core.loop.driver import LoopContext, LoopState
from ctx_weft.core.orchestrator.capability_cache import CapabilityCache
from ctx_weft.protocols import MemoryScope, ProviderContext
from ctx_weft.protocols.capability import (
    CapabilityEvent,
    CapabilityProviderInfo,
    ToolCapability,
    ToolCapabilityProvider,
)
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider


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


async def test_control_tool_resolves_when_cache_evicted() -> None:
    """Control tools are session-global: the gateway resolves them even when the per-agent
    capability cache is empty (e.g. a fire-and-forget background observe invoking
    collect_process_report AFTER its run ended and the per-agent cache was evicted).
    """
    from ctx_weft.core.orchestrator.control_capability import ControlCapabilityProvider

    mem, state, ctx = _state_ctx()
    provider = ControlCapabilityProvider()
    cache = CapabilityCache()  # EMPTY — agent binding evicted (run already finished)
    gw = CapabilityGateway(
        capability_cache=cache, capability_providers=[provider],
        memory=mem, event_bus=InProcessEventBus(),
    )
    res = await gw.invoke(
        "control__collect_process_report",
        {"task_process_report": "段总结X"}, state, ctx,
    )
    assert res.is_error is False, f"expected resolved via global fallback, got: {res.content}"
    assert res.content == "段总结X"


async def test_non_control_tool_still_unknown_when_uncached() -> None:
    """The global fallback is control-tools-only: a non-control (skill/mcp) tool that is NOT
    in the per-agent cache must still be unknown (no per-agent gating bypass)."""
    p = _Echo()
    mem, state, ctx = _state_ctx()
    cache = CapabilityCache()  # empty — provider exists but agent not bound to it
    gw = CapabilityGateway(
        capability_cache=cache, capability_providers=[p],
        memory=mem, event_bus=InProcessEventBus(),
    )
    res = await gw.invoke("mcp__a__search", {}, state, ctx)
    assert res.is_error is True
    assert "unknown tool" in res.content
