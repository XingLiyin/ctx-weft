"""_run_loop routes LLMOutageError to INTERRUPTED, not FAILED."""
import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.config import RuntimeConfig
from ctx_weft.providers.llm.mock import MockLLMAdapter
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from ctx_weft.protocols import LLMOutageError
from ctx_weft.protocols.events import EventType
from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_echo_template, make_runtime

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
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())
    # max_attempts=1 so self-heal exhausts immediately (no retry delays in tests)
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

    handle, state = await runtime.run_single_task(template_id="agent:tpl_echo", user_prompt="hi")

    # task must NOT be terminal-failed
    assert state.task.status == "INTERRUPTED"

    types = [getattr(e, "type", None) for e in seen]
    # 三层各发各的（Task 6）：loop 报 run 级事实，TM 聚合队列状态，SM 判会话状态。
    run_interrupted = [e for e in seen if e.type == EventType.RUN_INTERRUPTED]
    assert run_interrupted, "expected RunInterrupted (run-level fact)"
    assert run_interrupted[0].payload["reason"] == "llm_outage"
    assert EventType.TASK_QUEUE_INTERRUPTED in types, "expected TaskQueueInterrupted (TM aggregate)"
    assert EventType.SESSION_INTERRUPTED in types, "expected SessionInterrupted (SM verdict)"
    # 通用 setter 退役：谁都不许再发它。
    assert EventType.SESSION_STATUS_CHANGED not in types
    # no TASK_FAILED emitted
    assert not [e for e in seen if getattr(e, "type", None) == EventType.TASK_FAILED]
