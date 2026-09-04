"""Task 5 & 13: 在交互 / finish 段边界接线 launch_background_observe 的接线单测。

测试四个触发点（均为 root-gated）：
  1. observe.py ask_human 边界（act_exit_reason="normal"，root task）→ 触发一次
  2. observe.py root normal-exit finish（act_exit_reason="actor_done"，root task）→ 触发一次
  3. act.py 软打断 park（source="interrupt"，root task）→ 触发一次
  4. act.py 纯文本暂停 park（source="plain_text"，root task）→ 触发一次（Task 13）
  5. 子任务（parent_task_id 非空、同 agent）走相同路径 → 不触发
"""

from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from ctx_weft.core.loop.driver import LoopContext, LoopState
from ctx_weft.core.loop.park import HitlPark
from ctx_weft.core.loop.steps.observe import ObserveStep
from ctx_weft.core.loop.steps.suspend import SuspendStep
from ctx_weft.core.models.agent import Agent
from ctx_weft.core.models.session import Session
from ctx_weft.core.models.task import NormalTaskSettings, Task
from ctx_weft.protocols import (
    LLMChunk,
    MemoryEventType,
    MemoryAddress,
    ProviderContext,
)
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from ctx_weft.providers.llm.tokenizer import HeuristicTokenizer

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
    scope = MemoryAddress(session_id="s1", task_id=task.id, agent_id="ag1")
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
        resolved_model=SimpleNamespace(model="mock", account=""),
    )

    class _FakeEventBus:
        async def emit(self, event: Any) -> None:
            pass

    class _FakeAssembler:
        async def assemble(self, request: Any) -> Any:
            return SimpleNamespace(system="SYS", messages=[], tools=[])

    class _FakeLLM:
        context_limit = 100_000
        output_reserve = 4096
        tokenizer = HeuristicTokenizer()

        async def complete(self, request: Any, stream: bool = True):
            yield LLMChunk(kind="token", text="summary")

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

    def fake_launch(state, ctx, *, boundary=""):
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

    def fake_launch(state, ctx, *, boundary=""):
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


async def test_observe_child_task_does_not_fire_close_boundary(monkeypatch):
    """child task（parent_task_id 非空、同 agent）→ 不触发 close 边界（finish/normal）。

    Task 4 起它仍会 launch 一次，但边界是 "mechanical"：该子任务无 observe ROLE
    （template=None）→ 走机械判决，判决无摘要，摘要交 background observe 补。
    close 边界仍严格只属 root——mechanical 不在 _CLOSE_BOUNDARIES，不写 _close_report。
    """
    launched = []

    def fake_launch(state, ctx, *, boundary=""):
        launched.append((state.task.id, boundary))
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

    from ctx_weft.core.loop.steps.background_observe import _CLOSE_BOUNDARIES
    assert launched == [("t2", "mechanical")], launched
    assert not [b for _, b in launched if b in _CLOSE_BOUNDARIES], (
        f"child task must never take a close boundary, got {launched}")


# ── act.py: soft-interrupt park fires for root ───────────────────────────────


async def test_act_soft_interrupt_fires_for_root(monkeypatch):
    """act soft-interrupt park (source='interrupt', root task) → launch_background_observe called once."""
    from ctx_weft.core.control.tokens import PauseToken
    from ctx_weft.core.loop.steps.act import ActStep
    from tests.hitl_env import make_hitl
    from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
    from ctx_weft.core.assembler.assembler import AssembledPrompt
    from ctx_weft.protocols import LLMMessage

    launched = []

    def fake_launch(state, ctx, *, boundary=""):
        launched.append((state.task.id,))
        return asyncio.ensure_future(asyncio.sleep(0))

    import ctx_weft.core.loop.steps.background_observe as bo_mod
    monkeypatch.setattr(bo_mod, "_task_pending", {})
    monkeypatch.setattr(
        "ctx_weft.core.loop.steps.background_observe.launch_background_observe",
        fake_launch,
        raising=False,
    )

    from ctx_weft.providers.events import InProcessEventBus
    bus = InProcessEventBus()
    mem = InMemoryMemoryProvider()
    hitl, _hitl_reg = make_hitl(bus)

    session = Session(id="s1", tenant_id="default", user_prompt="hi", status="RUNNING")
    task = _make_root_task(status="ACTIVE")
    agent = Agent(id="ag1", session_id="s1", template_id="t")
    scope = MemoryAddress(session_id="s1", task_id="t1", agent_id="ag1")
    pctx = ProviderContext(session_id="s1", tenant_id="default", task_id="t1", agent_id="ag1")

    prompt = AssembledPrompt(
        system="", messages=[LLMMessage(role="user", content="hi")], tools=[], token_count=1,
    )
    state = LoopState(
        run_id="r1", session=session, task=task, agent=agent, scope=scope, assembled_prompt=prompt,
        resolved_model=SimpleNamespace(model="mock", account=""),
    )
    llm = MockLLMAdapter(responses=[MockResponse(text="partial")])
    ctx = LoopContext(
        assembler=None, llm=llm, memory=mem, event_bus=bus,
        provider_ctx=pctx, hitl=hitl,
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
    from tests.hitl_env import make_hitl
    from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
    from ctx_weft.core.assembler.assembler import AssembledPrompt
    from ctx_weft.protocols import LLMMessage

    launched = []

    def fake_launch(state, ctx, *, boundary=""):
        launched.append((state.task.id,))
        return asyncio.ensure_future(asyncio.sleep(0))

    import ctx_weft.core.loop.steps.background_observe as bo_mod
    monkeypatch.setattr(bo_mod, "_task_pending", {})
    monkeypatch.setattr(
        "ctx_weft.core.loop.steps.background_observe.launch_background_observe",
        fake_launch,
        raising=False,
    )

    from ctx_weft.providers.events import InProcessEventBus
    bus = InProcessEventBus()
    mem = InMemoryMemoryProvider()
    hitl, _hitl_reg = make_hitl(bus)

    session = Session(id="s1", tenant_id="default", user_prompt="hi", status="RUNNING")
    child = _make_child_task(status="ACTIVE")
    agent = Agent(id="ag1", session_id="s1", template_id="t")
    scope = MemoryAddress(session_id="s1", task_id="t2", agent_id="ag1")
    pctx = ProviderContext(session_id="s1", tenant_id="default", task_id="t2", agent_id="ag1")

    prompt = AssembledPrompt(
        system="", messages=[LLMMessage(role="user", content="hi")], tools=[], token_count=1,
    )
    state = LoopState(
        run_id="r1", session=session, task=child, agent=agent, scope=scope, assembled_prompt=prompt,
        resolved_model=SimpleNamespace(model="mock", account=""),
    )
    llm = MockLLMAdapter(responses=[MockResponse(text="partial")])
    ctx = LoopContext(
        assembler=None, llm=llm, memory=mem, event_bus=bus,
        provider_ctx=pctx, hitl=hitl,
    )

    pause = PauseToken()
    pause.pause()
    ctx.pause_token = pause

    with pytest.raises(HitlPark):
        await ActStep().execute(state, ctx)

    assert len(launched) == 0, f"Expected 0 launches for child soft-interrupt, got {len(launched)}"


# ── act.py: plain-text pause fires for root (Task 13) ───────────────────────


async def test_act_plain_text_pause_fires_for_root(monkeypatch):
    """act plain-text pause (source='plain_text', root task, interactive) → launch_background_observe called once."""
    from ctx_weft.core.loop.steps.act import _finish_plain_text_turn
    from tests.hitl_env import make_hitl
    from ctx_weft.providers.events import InProcessEventBus

    launched = []

    def fake_launch(state, ctx, *, boundary=""):
        launched.append((state.task.id,))
        return asyncio.ensure_future(asyncio.sleep(0))

    import ctx_weft.core.loop.steps.background_observe as bo_mod
    monkeypatch.setattr(bo_mod, "_task_pending", {})
    monkeypatch.setattr(
        "ctx_weft.core.loop.steps.background_observe.launch_background_observe",
        fake_launch,
        raising=False,
    )

    bus = InProcessEventBus()
    mem = InMemoryMemoryProvider()
    hitl, _hitl_reg = make_hitl(bus)

    session = Session(id="s1", tenant_id="default", user_prompt="hi", status="RUNNING")
    task = dataclasses.replace(_make_root_task(status="ACTIVE"), interaction_mode="interactive")
    agent = Agent(id="ag1", session_id="s1", template_id="t")
    scope = MemoryAddress(session_id="s1", task_id="t1", agent_id="ag1")
    pctx = ProviderContext(session_id="s1", tenant_id="default", task_id="t1", agent_id="ag1")

    state = LoopState(
        run_id="r1", session=session, task=task, agent=agent, scope=scope,
        resolved_model=SimpleNamespace(model="mock", account=""),
    )
    ctx = LoopContext(
        assembler=None, llm=None, memory=mem, event_bus=bus,
        provider_ctx=pctx, hitl=hitl,
    )

    with pytest.raises(HitlPark):
        await _finish_plain_text_turn(state, ctx, turn_num=1)

    assert len(launched) == 1, f"Expected 1 launch for root plain-text pause, got {len(launched)}"


# ── act.py: plain-text pause does NOT fire for child (Task 13) ──────────────


async def test_act_plain_text_pause_child_task_does_not_fire(monkeypatch):
    """act plain-text pause with child task → launch_background_observe NOT called."""
    from ctx_weft.core.loop.steps.act import _finish_plain_text_turn
    from tests.hitl_env import make_hitl
    from ctx_weft.providers.events import InProcessEventBus

    launched = []

    def fake_launch(state, ctx, *, boundary=""):
        launched.append((state.task.id,))
        return asyncio.ensure_future(asyncio.sleep(0))

    import ctx_weft.core.loop.steps.background_observe as bo_mod
    monkeypatch.setattr(bo_mod, "_task_pending", {})
    monkeypatch.setattr(
        "ctx_weft.core.loop.steps.background_observe.launch_background_observe",
        fake_launch,
        raising=False,
    )

    bus = InProcessEventBus()
    mem = InMemoryMemoryProvider()
    hitl, _hitl_reg = make_hitl(bus)

    session = Session(id="s1", tenant_id="default", user_prompt="hi", status="RUNNING")
    child = dataclasses.replace(_make_child_task(status="ACTIVE"), interaction_mode="interactive")
    agent = Agent(id="ag1", session_id="s1", template_id="t")
    scope = MemoryAddress(session_id="s1", task_id="t2", agent_id="ag1")
    pctx = ProviderContext(session_id="s1", tenant_id="default", task_id="t2", agent_id="ag1")

    state = LoopState(
        run_id="r1", session=session, task=child, agent=agent, scope=scope,
        resolved_model=SimpleNamespace(model="mock", account=""),
    )
    ctx = LoopContext(
        assembler=None, llm=None, memory=mem, event_bus=bus,
        provider_ctx=pctx, hitl=hitl,
    )

    with pytest.raises(HitlPark):
        await _finish_plain_text_turn(state, ctx, turn_num=1)

    assert len(launched) == 0, f"Expected 0 launches for child plain-text pause, got {len(launched)}"


# ── suspend.py: dispatch boundary (spec 2026-07-16) fires for all delegate parents ──


def _make_suspend_state_ctx(task: Task):
    """Minimal LoopState + LoopContext for SuspendStep.execute()."""
    mem = InMemoryMemoryProvider()
    scope = MemoryAddress(session_id="s1", task_id=task.id, agent_id="ag1")
    pctx = ProviderContext(session_id="s1", tenant_id="default", task_id=task.id, agent_id="ag1")
    session = Session(id="s1", tenant_id="default", user_prompt="hello", status="RUNNING")
    agent = SimpleNamespace(id="ag1")

    state = LoopState(
        run_id="r1", session=session, task=task, agent=agent, scope=scope,
        extra={"template": None},
        resolved_model=SimpleNamespace(model="mock", account=""),
    )

    class _FakeEventBus:
        async def emit(self, event: Any) -> None:
            pass

    ctx = LoopContext(
        assembler=None, llm=None, memory=mem, event_bus=_FakeEventBus(),
        provider_ctx=pctx,
    )
    return state, ctx


async def test_suspend_step_fires_dispatch_boundary_for_root(monkeypatch):
    """root task delegate suspend → launch_background_observe(boundary='dispatch') once."""
    launched = []

    def fake_launch(state, ctx, *, boundary=""):
        launched.append((state.task.id, boundary))
        return asyncio.ensure_future(asyncio.sleep(0))

    import ctx_weft.core.loop.steps.background_observe as bo_mod
    monkeypatch.setattr(bo_mod, "_task_pending", {})
    monkeypatch.setattr(
        "ctx_weft.core.loop.steps.background_observe.launch_background_observe",
        fake_launch, raising=False,
    )

    task = _make_root_task(status="SUSPENDED")
    state, ctx = _make_suspend_state_ctx(task)

    await SuspendStep().execute(state, ctx)

    assert launched == [("t1", "dispatch")], f"Expected one dispatch launch, got {launched}"


async def test_suspend_step_fires_dispatch_boundary_for_child(monkeypatch):
    """non-root delegate parent fires same way (spec: no _is_own_root gating)."""
    launched = []

    def fake_launch(state, ctx, *, boundary=""):
        launched.append((state.task.id, boundary))
        return asyncio.ensure_future(asyncio.sleep(0))

    import ctx_weft.core.loop.steps.background_observe as bo_mod
    monkeypatch.setattr(bo_mod, "_task_pending", {})
    monkeypatch.setattr(
        "ctx_weft.core.loop.steps.background_observe.launch_background_observe",
        fake_launch, raising=False,
    )

    child = _make_child_task(status="SUSPENDED")
    state, ctx = _make_suspend_state_ctx(child)

    await SuspendStep().execute(state, ctx)

    assert launched == [("t2", "dispatch")], f"Expected one dispatch launch, got {launched}"
