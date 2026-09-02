"""loop 在 run 结束时产出 RunOutcome（Task 2）。

本 task 只加产出、不删旧路径——旧的写状态与发事件仍在，
所以既有行为一字不变，全量应当仍是那 2 条基线失败。

搭台方式参照 `tests/unit/test_finalize_fail_reason.py`（SimpleNamespace state + Step().execute()
直调）、`tests/unit/test_hitl_park.py`（`_run_loop` + stub driver）、
`tests/unit/test_run_loop_outage.py`（`run_single_task` 全链路 + `_OutageLLM`）——三种既有自
包含搭台手法的合集，本文件不改动那两个文件本身。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from ctx_weft.core.loop.steps.finalize import FinalizeStep
from ctx_weft.core.loop.steps.observe import Verdict
from ctx_weft.core.loop.steps.suspend import SuspendStep
from ctx_weft.core.orchestrator.task_disposition import RunOutcomeKind
from ctx_weft.core.state.models import NormalTaskSettings, Task
from ctx_weft.protocols import (
    MemoryAddress,
    MemoryConfig,
    MemoryEvent,
    MemoryEventType,
    ProviderContext,
)
from ctx_weft.protocols.template import LoopConfig
from ctx_weft.providers.llm.tokenizer import HeuristicTokenizer
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider

pytestmark = pytest.mark.asyncio

T = MemoryEventType


def _ctx() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default")


class _FakeTM:
    def children_of(self, task_id: str) -> set[str]:
        return set()


def _loop_ctx(mem):
    return SimpleNamespace(memory=mem, provider_ctx=_ctx(), task_manager=_FakeTM(),
                           llm=SimpleNamespace(tokenizer=HeuristicTokenizer()))


# ── finalize / observer verdict ────────────────────────────────────────────────


async def _finalize_state(mem, *, verdict_outcome: str, summary: str, retry_count: int = 0):
    scope = MemoryAddress(session_id="s1", task_id="c1", agent_id="ag2")
    await mem.ingest(MemoryEvent(type=T.USER_PROMPT, address=scope, content="do it",
                                 timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                                 role="user"), _ctx())
    task = Task(id="c1", session_id="s1", status="ACTIVE", tenant_id="default",
                assigned_agent_id="ag2", creator_agent_id="ag1", parent_task_id="p1",
                title="Doing Task", user_prompt="do it", settings=NormalTaskSettings())
    task.retry_count = retry_count
    verdict = Verdict(task_outcome=verdict_outcome, act_recap=summary, task_summary="")
    agent = SimpleNamespace(id="ag2", loop_config=LoopConfig())
    session = SimpleNamespace(id="s1", tenant_id="default")
    return SimpleNamespace(run_id="r1", sequence_counter=0, session=session,
                           scope=scope, task=task, agent=agent, verdict=verdict,
                           run_outcome=None)


async def run_until_finalize(*, verdict: str, summary: str = "", retry_count: int = 0):
    """自包含搭台：直调 `FinalizeStep().execute()`（同 test_finalize_fail_reason.py 手法），
    再把它 state_patch 里的 run_outcome 应用回 state（driver.apply_patch 在真链路里做的事）。
    """
    mem = InMemoryMemoryProvider()
    state = await _finalize_state(mem, verdict_outcome=verdict, summary=summary,
                                  retry_count=retry_count)
    outcome = await FinalizeStep().execute(state, _loop_ctx(mem))
    state.run_outcome = outcome.state_patch.get("run_outcome")
    return state


async def test_observer_success_produces_a_completed_outcome() -> None:
    state = await run_until_finalize(verdict="success", summary="做完了")
    assert state.run_outcome is not None
    assert state.run_outcome.kind is RunOutcomeKind.COMPLETED
    assert state.run_outcome.verdict == "success"
    assert state.run_outcome.summary == "做完了"


async def test_observer_retry_produces_the_verdict_not_the_disposition() -> None:
    """loop 只报「observer 说重试」，**不判**预算够不够——那是 TM 的活。"""
    state = await run_until_finalize(verdict="retry", summary="进行中", retry_count=99)
    assert state.run_outcome.kind is RunOutcomeKind.COMPLETED
    # 即使预算早耗尽（max_retries=3），这里仍是 retry
    assert state.run_outcome.verdict == "retry"


# ── suspend ─────────────────────────────────────────────────────────────────


async def run_until_suspend(*, titles: list[str]):
    """真 `LoopState`（非 SimpleNamespace）：SuspendStep 末尾会 fire-and-forget 一个
    `dataclasses.replace(state)` 快照（background_observe），SimpleNamespace 不是
    dataclass 会在那一步炸掉。"""
    from ctx_weft.core.loop.driver import LoopState
    from ctx_weft.core.state.models import Agent, LoopGuard, Session

    mem = InMemoryMemoryProvider()
    scope = MemoryAddress(session_id="s1", task_id="c1", agent_id="ag2")
    task = Task(id="c1", session_id="s1", status="SUSPENDED", tenant_id="default",
                assigned_agent_id="ag2", creator_agent_id="ag1", parent_task_id="p1",
                title="Suspending Task", user_prompt=None,
                settings=NormalTaskSettings(spawn_titles=list(titles)))
    task.user_prompt_in_memory = True  # 跳过 ingest 分支（不是本测试关心的）
    session = Session(id="s1", user_prompt="do it", status="RUNNING",
                      tenant_id="default", root_agent_id="ag2")
    agent = Agent(id="ag2", session_id="s1", template_id="tpl_test", template_version="0.1",
                 status="RUNNING", tenant_id="default", loop_guard=LoopGuard(),
                 memory_config=MemoryConfig(), loop_config=LoopConfig())
    state = LoopState(run_id="r1", session=session, task=task, agent=agent, scope=scope)
    # launch_background_observe 是 fire-and-forget：给它一个真 event_bus，否则它的
    # finally 块（无条件发 TASK_RECAP_DONE）会在测试结束后抛 "exception was never
    # retrieved"。段里没有任何 CONVERSATION_TURN → n_raw==0 幂等护栏立即 return，
    # 不会真的调 LLM。
    from ctx_weft.core.loop.steps.background_observe import await_pending_background_observe
    from ctx_weft.providers.events import InProcessEventBus
    ctx = _loop_ctx(mem)
    ctx.event_bus = InProcessEventBus()
    outcome = await SuspendStep().execute(state, ctx)
    state.run_outcome = outcome.state_patch.get("run_outcome")
    # 等后台 recap task 真正跑完（它的幂等护栏会立即 return），不留悬空 task 到测试之外。
    await await_pending_background_observe(task.id)
    return state


async def test_suspend_on_children_carries_spawn_titles() -> None:
    state = await run_until_suspend(titles=["查资料"])
    assert state.run_outcome.kind is RunOutcomeKind.SUSPENDED_ON_CHILDREN
    assert list(state.run_outcome.spawn_titles) == ["查资料"]


# ── HITL park（runtime._run_loop） ───────────────────────────────────────────


async def run_until_park():
    """同 test_hitl_park.py::test_run_loop_catches_park_returns_awaiting_human 的搭台。"""
    from collections.abc import AsyncIterator

    from ctx_weft.core import ProviderRegistry
    from ctx_weft.core.loop.driver import LoopContext, LoopState, StepOutcome
    from ctx_weft.core.loop.park import HitlPark
    from ctx_weft.core.orchestrator.capability_cache import CapabilityCache
    from ctx_weft.core.state.models import Agent, LoopGuard, Session
    from ctx_weft.protocols import LoopConfig as RTLoopConfig
    from ctx_weft.protocols import MemoryConfig
    from ctx_weft.providers.events import InProcessEventBus
    from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider as MemProv
    from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_runtime

    session = Session(id="s_park_2", user_prompt="park test", status="RUNNING",
                      tenant_id="default", root_agent_id="agt_p2")
    task = Task(id="tsk_park_2", session_id="s_park_2", status="ACTIVE",
               tenant_id="default", assigned_agent_id="agt_p2")
    agent = Agent(id="agt_p2", session_id="s_park_2", template_id="tpl_test",
                 template_version="0.1", status="RUNNING", tenant_id="default",
                 loop_guard=LoopGuard(), memory_config=MemoryConfig(),
                 loop_config=RTLoopConfig())
    scope = MemoryAddress(session_id="s_park_2", task_id="tsk_park_2", agent_id="agt_p2")
    state = LoopState(run_id="run_p2", session=session, task=task, agent=agent, scope=scope)

    class _ParkingDriver:
        async def run(
            self, initial_state: LoopState, ctx: LoopContext,
        ) -> AsyncIterator[StepOutcome]:
            raise HitlPark(hitl_id="req_park_2", tool_call_id="tc_park_2")
            yield

    bus = InProcessEventBus()
    registry = ProviderRegistry()
    registry.register_memory(MemProv())
    rt = make_runtime(agent_provider=InlineAgentTemplateProvider(), providers=registry,
                      event_store=None)
    rt._event_bus = bus

    loop_ctx = LoopContext(
        assembler=None, llm=None, memory=MemProv(), event_bus=bus,
        provider_ctx=ProviderContext(session_id="s_park_2", tenant_id="default",
                                     task_id="tsk_park_2", agent_id="agt_p2"),
        capability_cache=CapabilityCache(),
    )

    return await rt._run_loop(state=state, loop_ctx=loop_ctx, driver=_ParkingDriver(),
                              run_id="run_p2", initial_step="act", task=task, agent=agent)


async def test_hitl_park_produces_awaiting_human_with_hitl_id() -> None:
    state = await run_until_park()
    assert state.run_outcome.kind is RunOutcomeKind.AWAITING_HUMAN
    assert state.run_outcome.hitl_id == "req_park_2"


# ── LLM outage（runtime._run_loop 经 run_single_task 全链路） ─────────────────


async def run_until_outage():
    """同 test_run_loop_outage.py::test_outage_marks_session_interrupted_not_failed 的搭台。"""
    from ctx_weft.core.config import RuntimeConfig
    from ctx_weft.protocols import LLMOutageError
    from ctx_weft.providers.llm.mock import MockLLMAdapter
    from tests.integration.test_minimal_loop import (
        InlineAgentTemplateProvider,
        make_echo_template,
        make_runtime,
    )

    class _OutageLLM(MockLLMAdapter):
        def complete(self, request, stream=True):
            self.last_request = request
            async def _gen():
                raise LLMOutageError("simulated outage exhausted")
                yield  # pragma: no cover
            return _gen()

    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())
    runtime = make_runtime(llm=_OutageLLM(responses=[]), agent_provider=resolver,
                           config=RuntimeConfig(llm_self_heal_max_attempts=1))
    runtime.providers.register_memory(InMemoryMemoryProvider())

    _handle, state = await runtime.run_single_task(
        template_id="agent:tpl_echo", user_prompt="hi")
    return state


async def test_llm_outage_produces_interrupted_not_retriable() -> None:
    state = await run_until_outage()
    assert state.run_outcome.kind is RunOutcomeKind.INTERRUPTED
    assert state.run_outcome.reason == "llm_outage"
    assert state.run_outcome.retriable is False     # outage 从不原地重试
