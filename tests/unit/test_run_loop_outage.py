"""_run_loop routes LLMOutageError to INTERRUPTED, not FAILED."""
import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.config import RuntimeConfig
from ctx_weft.providers.llm.mock import MockLLMAdapter
from ctx_weft.providers.memory_blackboard import InMemoryMemoryProvider
from ctx_weft.protocols import LLMOutageError
from ctx_weft.core.events.types import EventType
from tests.integration.test_minimal_loop import InMemoryTemplateResolver, make_echo_template, make_runtime

pytestmark = pytest.mark.asyncio


class _OutageLLM(MockLLMAdapter):
    """Raises LLMOutageError on every complete() (outage already self-heal-exhausted)."""

    def complete(self, request, stream=True):
        self.last_request = request
        async def _gen():
            raise LLMOutageError("simulated outage exhausted")
            yield  # pragma: no cover  (make this an async generator)
        return _gen()


async def test_outage_marks_session_interrupted_not_failed():
    resolver = InMemoryTemplateResolver()
    resolver.register(make_echo_template())
    # max_attempts=1 so self-heal exhausts immediately (no retry delays in tests)
    runtime = make_runtime(
        llm=_OutageLLM(responses=[]),
        template_resolver=resolver,
        config=RuntimeConfig(llm_self_heal_max_attempts=1),
    )
    runtime.providers.register_memory(InMemoryMemoryProvider())

    seen = []
    orig_emit = runtime._event_bus.emit
    async def _spy(ev):
        seen.append(ev)
        return await orig_emit(ev)
    runtime._event_bus.emit = _spy

    handle, state = await runtime.run_single_task(template_id="agent:tpl_echo", user_prompt="hi")

    # task must NOT be terminal-failed
    assert state.task.status == "SUSPENDED"
    # a SessionStatusChanged(INTERRUPTED) must have been emitted
    status_events = [
        e for e in seen
        if getattr(e, "type", None) == EventType.SESSION_STATUS_CHANGED
        and (e.payload or {}).get("new_status") == "INTERRUPTED"
    ]
    assert status_events, "expected SessionStatusChanged(INTERRUPTED)"
    # no TASK_FAILED emitted
    assert not [e for e in seen if getattr(e, "type", None) == EventType.TASK_FAILED]
