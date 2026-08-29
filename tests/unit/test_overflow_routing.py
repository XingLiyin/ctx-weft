"""_run_loop routes ContextOverflowError to recoverable SUSPENDED (interrupted), not terminal FAILED.

溢出不再终态：retriable=False → 不重试、挂起等 /resume；用户换更大窗口的模型恢复
（recover_session 的 llm_model 覆盖 + 窗口参数同步）。错误文案仍随 task.error 抵达 host。
"""
import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.config import RuntimeConfig
from ctx_weft.core.errors import ContextOverflowError
from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.providers.llm.mock import MockLLMAdapter
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from ctx_weft.protocols.events import EventType
from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_echo_template, make_runtime

pytestmark = pytest.mark.asyncio


class _OverflowLLM(MockLLMAdapter):
    """Raises ContextOverflowError on every complete() (budget.apply enrichment)."""

    def complete(self, request, stream=True):
        self.last_request = request
        async def _gen():
            raise ContextOverflowError(
                required=200_000, effective_limit=171_808,
                context_limit=180_000, reserved_output_tokens=8192,
            )
            yield  # pragma: no cover  (make this an async generator)
        return _gen()


async def test_overflow_marks_task_suspended_not_failed():
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())
    runtime = make_runtime(
        llm=_OverflowLLM(responses=[]),
        agent_provider=resolver,
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
            await runtime.run_single_task(template_id="agent:tpl_echo", user_prompt="hi")
    finally:
        TaskManager.register_task = orig_register

    task = registered["task"]
    # 溢出 = 可恢复中断：挂起等 /resume，不是终态失败
    assert task.status == "SUSPENDED"
    assert "171808" in task.error or "171,808" in task.error

    # 本 harness 直驱 _execute_task/_run_loop，不经 TaskManager._run_task；
    # SessionStatusChanged(INTERRUPTED) 由 TaskManager._suspend_task_interrupted 挂起终局发，
    # 不在本层——此处仍应为空（该事件由 test_run_crash_suspend.py 覆盖）。
    status_events = [
        e for e in seen
        if getattr(e, "type", None) == EventType.SESSION_STATUS_CHANGED
        and (e.payload or {}).get("new_status") == "INTERRUPTED"
    ]
    assert not status_events, "INTERRUPTED 由 TaskManager 挂起终局发，不在本层"
    # 不发 TASK_FAILED；TaskSuspended + SessionStatusChanged(INTERRUPTED) 由
    # TaskManager._suspend_task_interrupted 发（tests/unit/test_run_crash_suspend.py 覆盖）
    assert not [e for e in seen if getattr(e, "type", None) == EventType.TASK_FAILED]
    # RUN_FINISHED 反映挂起（可恢复中断），且不再自动重试
    finished = [e for e in seen if getattr(e, "type", None) == EventType.RUN_FINISHED]
    assert finished and finished[-1].payload.get("final_status") == "SUSPENDED"
    assert finished[-1].payload.get("will_retry") is False
