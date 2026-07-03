"""_run_loop routes ContextOverflowError to terminal FAILED, not SUSPENDED."""
import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.config import RuntimeConfig
from ctx_weft.core.errors import ContextOverflowError
from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.providers.llm.mock import MockLLMAdapter
from ctx_weft.providers.memory_blackboard import InMemoryMemoryProvider
from ctx_weft.core.events.types import EventType
from tests.integration.test_minimal_loop import InMemoryTemplateResolver, make_echo_template

pytestmark = pytest.mark.asyncio


class _OverflowLLM(MockLLMAdapter):
    """Raises ContextOverflowError on every complete() (budget.apply enrichment)."""

    def complete(self, request, stream=True):
        self.last_request = request
        async def _gen():
            raise ContextOverflowError(
                "overflow", required=200_000, effective_limit=171_808,
                context_limit=180_000, reserved_output_tokens=8192,
            )
            yield  # pragma: no cover  (make this an async generator)
        return _gen()


async def test_overflow_marks_task_failed_not_suspended():
    resolver = InMemoryTemplateResolver()
    resolver.register(make_echo_template())
    runtime = CtxWeftRuntime(
        llm=_OverflowLLM(responses=[]),
        template_resolver=resolver,
        config=RuntimeConfig(),
    )
    runtime.providers.register_memory(InMemoryMemoryProvider())

    seen = []
    orig_emit = runtime._event_bus.emit
    async def _spy(ev):
        seen.append(ev)
        return await orig_emit(ev)
    runtime._event_bus.emit = _spy

    # run_single_task (Phase-1 compat path) re-raises non-retriable run errors
    # after _run_loop's finally block mutates `task` in place — capture the
    # very Task object registered with TaskManager so we can inspect it after
    # the exception propagates.
    registered = {}
    orig_register = TaskManager.register_task
    def _spy_register(self, task):
        registered["task"] = task
        return orig_register(self, task)
    TaskManager.register_task = _spy_register
    try:
        with pytest.raises(ContextOverflowError):
            await runtime.run_single_task(template_id="tpl_echo", user_prompt="hi")
    finally:
        TaskManager.register_task = orig_register

    task = registered["task"]
    # task must be terminal FAILED, not SUSPENDED
    assert task.status == "FAILED"
    assert task.status != "SUSPENDED"
    assert "171808" in task.error or "171,808" in task.error

    # no SessionStatusChanged(INTERRUPTED) must have been emitted
    status_events = [
        e for e in seen
        if getattr(e, "type", None) == EventType.SESSION_STATUS_CHANGED
        and (e.payload or {}).get("new_status") == "INTERRUPTED"
    ]
    assert not status_events, "did not expect SessionStatusChanged(INTERRUPTED)"
    # RUN_FINISHED must reflect the terminal FAILED status (not a retry/suspend)
    finished = [e for e in seen if getattr(e, "type", None) == EventType.RUN_FINISHED]
    assert finished and finished[-1].payload.get("final_status") == "FAILED"
