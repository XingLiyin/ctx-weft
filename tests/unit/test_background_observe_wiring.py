"""Task 5: 在交互 / finish 段边界接线 launch_background_observe 的接线单测。

测试三个触发点（均为 root-gated）：
  1. observe.py ask_human 边界（act_exit_reason="normal"，root task）→ 触发一次
  2. observe.py root normal-exit finish（act_exit_reason="actor_done"，root task）→ 触发一次
  3. act.py 软打断 park（source="interrupt"，root task）→ 触发一次
  4. 子任务（parent_task_id 非空、同 agent）走相同路径 → 不触发
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from ctx_weft.core.loop.driver import LoopContext, LoopState
from ctx_weft.core.loop.park import HitlPark
from ctx_weft.core.loop.steps.observe import ObserveStep
from ctx_weft.core.state.models import (
    Agent,
    NormalTaskSettings,
    Session,
    Task,
)
from ctx_weft.protocols import (
    LLMChunk,
    MemoryEventType,
    MemoryScope,
    ProviderContext,
)
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

pytestmark = pytest.mark.asyncio

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_root_task(**kwargs) -> Task:
    """Root task: parent_task_id=None, same creator/assigned agent."""
    defaults = dict(
        id="t1",
        session_id="s1",
        status="ACTIVE",
        assigned_agent_id="ag1",
        creator_agent_id="ag1",
        parent_task_id=None,
        settings=NormalTaskSettings(),
        actor_done=False,
        observer_outcome=None,
    )
    defaults.update(kwargs)
    return Task(**defaults)


def _make_child_task(**kwargs) -> Task:
    """Child task: parent_task_id set, same agent (same_agent=True, cross_agent=False)."""
    defaults = dict(
        id="t2",
        session_id="s1",
        status="ACTIVE",
        assigned_agent_id="ag1",
        creator_agent_id="ag1",
        parent_task_id="t0",  # has a parent → is child
        settings=NormalTaskSettings(),
        actor_done=False,
        observer_outcome=None,
    )
    defaults.update(kwargs)
    return Task(**defaults)


def _make_observe_state_ctx(task: Task, act_exit_reason: str):
    """Build minimal LoopState + LoopContext for ObserveStep.execute()."""
    mem = InMemoryMemoryProvider()
    scope = MemoryScope(session_id="s1", task_id=task.id, agent_id="ag1")
    pctx = ProviderContext(session_id="s1", tenant_id="default", task_id=task.id, agent_id="ag1")
    session = Session(id="s1", tenant_id="default", user_prompt="hello", status="RUNNING")

    agent = SimpleNamespace(
        id="ag1",
        loop_config=SimpleNamespace(
            max_turns_per_observe=1,
            compact_keep_last=2,
        ),
        runtime={"llm_model": "mock"},
        loop_guard=SimpleNamespace(context_limit=100_000, context_tokens=0),
    )

    state = LoopState(
        run_id="r1",
        session=session,
        task=task,
        agent=agent,
        scope=scope,
        act_exit_reason=act_exit_reason,
        extra={"template": None},
    )

    class _FakeEventBus:
        async def emit(self, event: Any) -> None:
            pass

    class _FakeAssembler:
        async def assemble(self, request: Any) -> Any:
            return SimpleNamespace(system="SYS", messages=[], tools=[])

    class _FakeLLM:
        context_limit = 100_000
        max_output_tokens = 4096

        async def complete(self, request: Any, stream: bool = True):
            yield LLMChunk(kind="token", text="summary")

        async def count_tokens(self, text: str) -> int:
            return len(text)

    ctx = LoopContext(
        assembler=_FakeAssembler(),
        llm=_FakeLLM(),
        memory=mem,
        event_bus=_FakeEventBus(),
        provider_ctx=pctx,
    )
    return state, ctx


# ── observe.py: ask_human boundary (act_exit_reason="normal", root task) ─────


async def test_observe_ask_human_boundary_fires_for_root(monkeypatch):
    """root task, act_exit_reason='normal' → launch_background_observe called once."""
    launched = []

    def fake_launch(state, ctx):
        launched.append((state.task.id,))
        fut = asyncio.get_event_loop().create_future()
        fut.set_result(None)
        return asyncio.ensure_future(asyncio.sleep(0))

    import ctx_weft.core.loop.steps.background_observe as bo_mod
    monkeypatch.setattr(bo_mod, "_task_pending", {})

    # Patch launch_background_observe in observe module's namespace
    import ctx_weft.core.loop.steps.observe as obs_mod
    monkeypatch.setattr(
        "ctx_weft.core.loop.steps.background_observe.launch_background_observe",
        fake_launch,
        raising=False,
    )

    task = _make_root_task()
    state, ctx = _make_observe_state_ctx(task, act_exit_reason="normal")

    await ObserveStep().execute(state, ctx)

    assert len(launched) == 1, f"Expected 1 launch, got {len(launched)}"


# ── observe.py: root normal-exit finish (act_exit_reason="actor_done") ────────


async def test_observe_finish_fires_for_root(monkeypatch):
    """root task, act_exit_reason='actor_done' → launch_background_observe called once."""
    launched = []

    def fake_launch(state, ctx):
        launched.append((state.task.id,))
        return asyncio.ensure_future(asyncio.sleep(0))

    import ctx_weft.core.loop.steps.background_observe as bo_mod
    monkeypatch.setattr(bo_mod, "_task_pending", {})
    monkeypatch.setattr(
        "ctx_weft.core.loop.steps.background_observe.launch_background_observe",
        fake_launch,
        raising=False,
    )

    task = _make_root_task(actor_done=True)
    state, ctx = _make_observe_state_ctx(task, act_exit_reason="actor_done")

    await ObserveStep().execute(state, ctx)

    assert len(launched) == 1, f"Expected 1 launch, got {len(launched)}"


# ── observe.py: child task does NOT fire ─────────────────────────────────────


async def test_observe_child_task_does_not_fire(monkeypatch):
    """child task (parent_task_id set, same agent) → launch_background_observe NOT called."""
    launched = []

    def fake_launch(state, ctx):
        launched.append((state.task.id,))
        return asyncio.ensure_future(asyncio.sleep(0))

    import ctx_weft.core.loop.steps.background_observe as bo_mod
    monkeypatch.setattr(bo_mod, "_task_pending", {})
    monkeypatch.setattr(
        "ctx_weft.core.loop.steps.background_observe.launch_background_observe",
        fake_launch,
        raising=False,
    )

    child = _make_child_task(actor_done=True)
    state, ctx = _make_observe_state_ctx(child, act_exit_reason="actor_done")

    await ObserveStep().execute(state, ctx)

    assert len(launched) == 0, f"Expected 0 launches for child task, got {len(launched)}"


# ── act.py: soft-interrupt park fires for root ───────────────────────────────


async def test_act_soft_interrupt_fires_for_root(monkeypatch):
    """act soft-interrupt park (source='interrupt', root task) → launch_background_observe called once."""
    from ctx_weft.core.control.tokens import PauseToken
    from ctx_weft.core.loop.steps.act import ActStep
    from ctx_weft.core.orchestrator.hitl_manager import HitlManager
    from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
    from ctx_weft.core.assembler.assembler import AssembledPrompt
    from ctx_weft.protocols import LLMMessage

    launched = []

    def fake_launch(state, ctx):
        launched.append((state.task.id,))
        return asyncio.ensure_future(asyncio.sleep(0))

    import ctx_weft.core.loop.steps.background_observe as bo_mod
    monkeypatch.setattr(bo_mod, "_task_pending", {})
    monkeypatch.setattr(
        "ctx_weft.core.loop.steps.background_observe.launch_background_observe",
        fake_launch,
        raising=False,
    )

    from ctx_weft.core.events.bus import InProcessEventBus
    bus = InProcessEventBus()
    mem = InMemoryMemoryProvider()
    hitl = HitlManager(event_bus=bus)

    session = Session(id="s1", tenant_id="default", user_prompt="hi", status="RUNNING")
    task = _make_root_task(status="ACTIVE")
    agent = Agent(id="ag1", session_id="s1", template_id="t", template_version="1", status="RUNNING")
    scope = MemoryScope(session_id="s1", task_id="t1", agent_id="ag1")
    pctx = ProviderContext(session_id="s1", tenant_id="default", task_id="t1", agent_id="ag1")

    prompt = AssembledPrompt(
        system="", messages=[LLMMessage(role="user", content="hi")], tools=[], token_count=1,
    )
    state = LoopState(
        run_id="r1", session=session, task=task, agent=agent, scope=scope, assembled_prompt=prompt,
    )
    llm = MockLLMAdapter(responses=[MockResponse(text="partial")])
    ctx = LoopContext(
        assembler=None, llm=llm, memory=mem, event_bus=bus,
        provider_ctx=pctx, hitl_manager=hitl,
    )

    # Fire pause before act starts
    pause = PauseToken()
    pause.pause()
    ctx.pause_token = pause

    with pytest.raises(HitlPark):
        await ActStep().execute(state, ctx)

    assert len(launched) == 1, f"Expected 1 launch for root soft-interrupt, got {len(launched)}"


# ── act.py: soft-interrupt park does NOT fire for child ──────────────────────


async def test_act_soft_interrupt_child_task_does_not_fire(monkeypatch):
    """act soft-interrupt park with child task → launch_background_observe NOT called."""
    from ctx_weft.core.control.tokens import PauseToken
    from ctx_weft.core.loop.steps.act import ActStep
    from ctx_weft.core.orchestrator.hitl_manager import HitlManager
    from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
    from ctx_weft.core.assembler.assembler import AssembledPrompt
    from ctx_weft.protocols import LLMMessage

    launched = []

    def fake_launch(state, ctx):
        launched.append((state.task.id,))
        return asyncio.ensure_future(asyncio.sleep(0))

    import ctx_weft.core.loop.steps.background_observe as bo_mod
    monkeypatch.setattr(bo_mod, "_task_pending", {})
    monkeypatch.setattr(
        "ctx_weft.core.loop.steps.background_observe.launch_background_observe",
        fake_launch,
        raising=False,
    )

    from ctx_weft.core.events.bus import InProcessEventBus
    bus = InProcessEventBus()
    mem = InMemoryMemoryProvider()
    hitl = HitlManager(event_bus=bus)

    session = Session(id="s1", tenant_id="default", user_prompt="hi", status="RUNNING")
    child = _make_child_task(status="ACTIVE")
    agent = Agent(id="ag1", session_id="s1", template_id="t", template_version="1", status="RUNNING")
    scope = MemoryScope(session_id="s1", task_id="t2", agent_id="ag1")
    pctx = ProviderContext(session_id="s1", tenant_id="default", task_id="t2", agent_id="ag1")

    prompt = AssembledPrompt(
        system="", messages=[LLMMessage(role="user", content="hi")], tools=[], token_count=1,
    )
    state = LoopState(
        run_id="r1", session=session, task=child, agent=agent, scope=scope, assembled_prompt=prompt,
    )
    llm = MockLLMAdapter(responses=[MockResponse(text="partial")])
    ctx = LoopContext(
        assembler=None, llm=llm, memory=mem, event_bus=bus,
        provider_ctx=pctx, hitl_manager=hitl,
    )

    pause = PauseToken()
    pause.pause()
    ctx.pause_token = pause

    with pytest.raises(HitlPark):
        await ActStep().execute(state, ctx)

    assert len(launched) == 0, f"Expected 0 launches for child soft-interrupt, got {len(launched)}"
