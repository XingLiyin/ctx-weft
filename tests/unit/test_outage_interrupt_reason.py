"""LLM-outage interrupt is tagged with reason='llm_outage' on the status event,
so the host/frontend can tell it apart from a generic interrupt (e.g. restart)."""
import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.config import RuntimeConfig
from ctx_weft.providers.llm.mock import MockLLMAdapter
from ctx_weft.providers.memory_blackboard import InMemoryMemoryProvider
from ctx_weft.protocols import LLMOutageError
from ctx_weft.core.events.types import EventType
from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_echo_template, make_runtime

pytestmark = pytest.mark.asyncio


class _OutageLLM(MockLLMAdapter):
    def complete(self, request, stream=True):
        async def _gen():
            raise LLMOutageError("simulated outage exhausted")
            yield  # pragma: no cover  (make this an async generator)
        return _gen()


async def test_outage_interrupt_carries_llm_outage_reason():
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())
    runtime = make_runtime(
        llm=_OutageLLM(responses=[]),
        agent_provider=resolver,
        config=RuntimeConfig(llm_self_heal_max_attempts=1),
    )
    runtime.providers.register_memory(InMemoryMemoryProvider())

    seen = []
    orig_emit = runtime._event_bus.emit
    async def _spy(ev):
        seen.append(ev)
        return await orig_emit(ev)
    runtime._event_bus.emit = _spy

    await runtime.run_single_task(template_id="agent:tpl_echo", user_prompt="hi")

    interrupts = [
        e for e in seen
        if getattr(e, "type", None) == EventType.SESSION_STATUS_CHANGED
        and (e.payload or {}).get("new_status") == "INTERRUPTED"
    ]
    assert interrupts, "expected SessionStatusChanged(INTERRUPTED)"
    assert all((e.payload or {}).get("reason") == "llm_outage" for e in interrupts)
