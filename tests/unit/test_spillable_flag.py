from collections.abc import AsyncIterator
from types import SimpleNamespace

from ctx_weft.providers.events import InProcessEventBus
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
from ctx_weft.protocols.filesystem import SpillSink
from ctx_weft.providers._tooldecl import make_tool_registry
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider


def test_toolcapability_spillable_defaults_true():
    cap = ToolCapability(id="x:a", name="a")
    assert cap.spillable is True


def test_tool_decorator_passes_spillable():
    tool, tools, _ = make_tool_registry("x")

    @tool(purposes=["act"], spillable=False)
    async def reader():
        """Read something."""

    @tool(purposes=["act"])
    async def normal():
        """Do something."""

    assert tools["reader"].spillable is False
    assert tools["normal"].spillable is True


class _Big(ToolCapabilityProvider):
    name = "mcp:b"

    def __init__(self, spillable: bool) -> None:
        self._spillable = spillable

    def _cap(self):
        return ToolCapability(id="mcp:b:dump", name="dump", description="d", spillable=self._spillable)

    async def list(self, ctx): return [self._cap()]
    async def retrieve(self, ctx): return [self._cap()]
    async def describe(self, ctx): return CapabilityProviderInfo(name=self.name, capability_count=1)

    def invoke(self, capability_id, arguments, ctx) -> AsyncIterator[CapabilityEvent]:
        async def _run():
            yield CapabilityEvent(kind="result", payload={"content": "x" * 20_000})
        return _run()

    async def cancel(self, invocation_id, ctx) -> None: return None


class _SpySpill(SpillSink):
    def __init__(self) -> None:
        self.called = False

    async def spill(self, content, ctx, *, name_hint="") -> str:
        self.called = True
        return "/tmp/spilled.txt"


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


def _gw(provider, spill, mem):
    cache = CapabilityCache()
    cache.put("agt_1", [provider._cap()])
    return CapabilityGateway(
        capability_cache=cache, capability_providers=[provider, spill],
        memory=mem, event_bus=InProcessEventBus(), spill_threshold=8000,
    )


async def test_spillable_false_tool_is_not_spilled():
    p, spy = _Big(spillable=False), _SpySpill()
    mem, state, ctx = _state_ctx()
    res = await _gw(p, spy, mem).invoke("mcp__b__dump", {}, state, ctx)
    assert spy.called is False
    assert res.content == "x" * 20_000  # returned whole, untouched


async def test_spillable_true_tool_is_spilled():
    p, spy = _Big(spillable=True), _SpySpill()
    mem, state, ctx = _state_ctx()
    res = await _gw(p, spy, mem).invoke("mcp__b__dump", {}, state, ctx)
    assert spy.called is True
    assert "truncated" in res.content and "/tmp/spilled.txt" in res.content
