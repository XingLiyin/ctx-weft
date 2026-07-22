"""_run_loop A1 守卫（Task 10）：was_cancelled 只在 task.status == "CANCELED" 时才发
RUN_CANCELED + TASK_CANCELED；已被熔断标 FAILED 的 run 被协作取消属内部清场，两条都不发
（否则 host/postgres 投影会把 FAILED 盖成 CANCELED）。RUN_FINISHED 无条件发出。
"""

from __future__ import annotations

import asyncio

import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.events.types import EventType
from ctx_weft.core.loop.driver import LoopContext, LoopState
from ctx_weft.core.orchestrator.capability_cache import CapabilityCache
from ctx_weft.core.state.models import Agent, LoopGuard, Session, Task
from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols import MemoryScope, ProviderContext
from ctx_weft.providers.llm.mock import MockLLMAdapter
from ctx_weft.providers.memory_blackboard import InMemoryMemoryProvider
from tests.integration.test_minimal_loop import InMemoryTemplateResolver, make_runtime

pytestmark = pytest.mark.asyncio


class _CancelingDriver:
    """driver.run() 的替身：立刻抛 asyncio.CancelledError，模拟协作取消命中检查点。"""

    async def run(self, state, ctx):
        raise asyncio.CancelledError("simulated cooperative cancel")
        yield  # pragma: no cover — 让方法保持 async generator 形状


def _runtime() -> CtxWeftRuntime:
    resolver = InMemoryTemplateResolver()
    runtime = make_runtime(llm=MockLLMAdapter(responses=[]), template_resolver=resolver)
    runtime.providers.register_memory(InMemoryMemoryProvider())
    return runtime


def _build_state_and_ctx(runtime: CtxWeftRuntime, task: Task):
    session = Session(id="s1", user_prompt="hi", status="RUNNING")
    agent = Agent(id="agt1", session_id="s1", template_id="tpl", template_version="1",
                  status="ACTIVE", loop_guard=LoopGuard())
    scope = MemoryScope(session_id="s1", task_id=task.id, agent_id="agt1")
    state = LoopState(run_id=generate_id("run"), session=session, task=task, agent=agent, scope=scope)
    provider_ctx = ProviderContext(session_id="s1", tenant_id="default", task_id=task.id, agent_id="agt1")
    ctx = LoopContext(
        assembler=None, llm=runtime._resolve_llm(), memory=runtime.providers.get_memory(),
        event_bus=runtime._event_bus, provider_ctx=provider_ctx,
        capability_cache=CapabilityCache(),
    )
    return state, ctx


async def _run_and_capture(runtime: CtxWeftRuntime, task: Task):
    state, ctx = _build_state_and_ctx(runtime, task)
    seen = []
    orig_emit = runtime._event_bus.emit

    async def _spy(ev):
        seen.append(ev)
        return await orig_emit(ev)

    runtime._event_bus.emit = _spy
    try:
        await runtime._run_loop(
            state, ctx, _CancelingDriver(), run_id=state.run_id,
            initial_step="prepare", task=task, agent=state.agent,
        )
    except asyncio.CancelledError:
        pass
    return seen


async def test_was_cancelled_and_task_failed_suppresses_cancel_events():
    """熔断已先手把 task 标 FAILED 后再协作取消：不发 RUN_CANCELED/TASK_CANCELED，RUN_FINISHED 照发。"""
    runtime = _runtime()
    task = Task(id="root", session_id="s1", status="FAILED", error_code="TASK_FAILED_BY_THRESHOLD")

    seen = await _run_and_capture(runtime, task)
    types = [e.type for e in seen]

    assert EventType.RUN_CANCELED not in types
    assert EventType.TASK_CANCELED not in types
    assert EventType.RUN_FINISHED in types
    assert task.status == "FAILED"  # 未被 except 分支覆写（FAILED 在排除元组里）


async def test_was_cancelled_and_task_canceled_emits_both_events():
    """回归：普通协作取消（task 未处于任何终态）本 run 自己置 CANCELED → 两条事件照发。"""
    runtime = _runtime()
    task = Task(id="c1", session_id="s1", status="ACTIVE")

    seen = await _run_and_capture(runtime, task)
    types = [e.type for e in seen]

    assert EventType.RUN_CANCELED in types
    assert EventType.TASK_CANCELED in types
    assert EventType.RUN_FINISHED in types
    assert task.status == "CANCELED"
