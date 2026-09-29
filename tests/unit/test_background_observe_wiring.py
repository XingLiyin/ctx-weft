"""Task 5 & 13: 在交互 / finish 段边界接线 launch_recap 的接线单测。

测试四个触发点（均为 root-gated）：
  1. observe.py ask_human 边界（act_exit_reason="normal"，root task）→ 触发一次
  2. observe.py root normal-exit finish（act_exit_reason="actor_done"，root task）→ 触发一次
  3. act.py 软打断 park（`_park_for_interrupt`，root task）→ 触发一次
  4. act.py 纯文本暂停 park（`_park_await_user`，root task）→ 触发一次（Task 13）
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
    """root task, act_exit_reason='normal' → launch_recap called once."""
    launched = []

    def fake_launch(state, ctx, *, boundary=""):
        launched.append((state.task.id,))
        fut = asyncio.get_event_loop().create_future()
        fut.set_result(None)
        return asyncio.ensure_future(asyncio.sleep(0))

    from ctx_weft.core.loop.background import runner as bo_mod
    monkeypatch.setattr(bo_mod, "_task_pending", {})

    # Patch launch_recap in observe module's namespace
    import ctx_weft.core.loop.steps.observe as obs_mod
    monkeypatch.setattr(
        "ctx_weft.core.loop.background.launch_recap",
        fake_launch,
        raising=False,
    )

    task = _make_root_task()
    state, ctx = _make_observe_state_ctx(task, act_exit_reason="normal")

    await ObserveStep().execute(state, ctx)

    assert len(launched) == 1, f"Expected 1 launch, got {len(launched)}"


# ── observe.py: root normal-exit finish (act_exit_reason="actor_done") ────────


async def test_observe_finish_fires_for_root(monkeypatch):
    """root task, act_exit_reason='actor_done' → launch_recap called once."""
    launched = []

    def fake_launch(state, ctx, *, boundary=""):
        launched.append((state.task.id,))
        return asyncio.ensure_future(asyncio.sleep(0))

    from ctx_weft.core.loop.background import runner as bo_mod
    monkeypatch.setattr(bo_mod, "_task_pending", {})
    monkeypatch.setattr(
        "ctx_weft.core.loop.background.launch_recap",
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
    close 边界仍严格只属 root——mechanical 不在 CLOSE_BOUNDARIES，不写 _close_report。
    """
    launched = []

    def fake_launch(state, ctx, *, boundary=""):
        launched.append((state.task.id, boundary))
        return asyncio.ensure_future(asyncio.sleep(0))

    from ctx_weft.core.loop.background import runner as bo_mod
    monkeypatch.setattr(bo_mod, "_task_pending", {})
    monkeypatch.setattr(
        "ctx_weft.core.loop.background.launch_recap",
        fake_launch,
        raising=False,
    )

    child = _make_child_task(actor_done=True)
    state, ctx = _make_observe_state_ctx(child, act_exit_reason="actor_done")

    await ObserveStep().execute(state, ctx)

    from ctx_weft.core.loop.background.boundaries import CLOSE_BOUNDARIES
    assert launched == [("t2", "mechanical")], launched
    assert not [b for _, b in launched if b in CLOSE_BOUNDARIES], (
        f"child task must never take a close boundary, got {launched}")


# ── observe.py: 前台真判过 → close 边界那次后台调用取消（2026-09-28）──────────


def _spy_launch(monkeypatch) -> list:
    """把 `launch_recap` 换成记录器，返回 (task_id, boundary) 列表。"""
    launched: list = []

    def fake_launch(state, ctx, *, boundary=""):
        launched.append((state.task.id, boundary))
        return asyncio.ensure_future(asyncio.sleep(0))

    from ctx_weft.core.loop.background import runner as bo_mod
    monkeypatch.setattr(bo_mod, "_task_pending", {})
    monkeypatch.setattr(
        "ctx_weft.core.loop.background.launch_recap",
        fake_launch,
        raising=False,
    )
    return launched


def _with_observe_role(state) -> None:
    """给 state 装上一个有 observe facet 的 template，让 `_should_use_llm` 放行。"""
    state.extra["template"] = SimpleNamespace(identity={"observe": SimpleNamespace(text="ROLE")})


async def test_foreground_verdict_cancels_the_close_boundary_recap(monkeypatch):
    """前台 LLM 真报了判决 → close 边界**不发**后台 observe。

    这道门是随「删掉 `_should_use_llm` 的 root 降级」一起加的，删不得：那次后台调用的全部
    用途就是替机械判决补一份摘要，前台报过之后它不只是多余，而是**有害**。

    `has_llm_summary = verdict.reported`（finalize）为真时，close 当场就把真摘要写进 finish
    对、末段 raw 同步折。但 `_synthesize_dispatch_pair` 仍带 `register_bg=True`，此刻
    `pop_close_report` 必然是空的（后台才刚 launch），于是走 `register_close_synth` 登记；
    等这次只摘要档回来，`_replace_finish_report` 就把前台那份好摘要**覆盖**成 recap-only 的
    产物。一次白烧的 LLM 换一次内容降级。
    """
    launched = _spy_launch(monkeypatch)
    from ctx_weft.core.loop.steps.observe import Verdict

    async def fake_llm_observe(self, state, ctx, events):
        return Verdict(task_outcome="success", act_recap="真 recap",
                       task_summary="真 summary", reported=True)

    monkeypatch.setattr(ObserveStep, "_llm_observe", fake_llm_observe, raising=True)

    task = _make_root_task(actor_done=True)
    state, ctx = _make_observe_state_ctx(task, act_exit_reason="actor_done")
    _with_observe_role(state)

    outcome = await ObserveStep().execute(state, ctx)

    assert launched == [], f"前台已产判决，不该再发任何后台 observe；实得 {launched}"
    assert outcome.state_patch["verdict"].reported is True


async def test_a_failed_foreground_verdict_still_gets_the_close_boundary_recap(monkeypatch):
    """同一格的另一半：前台 LLM 落空（抛异常 / 耗尽轮次没调工具）→ close 边界照发。

    这是那整套 close 机制（`_close_report` / `_close_synth` / `_replace_finish_report` /
    延迟折叠）在 2026-09-28 之后的**唯一**存在理由——它从「root 的常规路径」降级成「前台
    判决没摘要时的兜底」，但不能删：机械判决的 `act_recap` 是空的，末段 raw 的账要么由这次
    后台 recap 记，要么永久保 raw。
    """
    launched = _spy_launch(monkeypatch)

    async def boom(self, state, ctx, events):
        raise RuntimeError("observer 掉线")

    monkeypatch.setattr(ObserveStep, "_llm_observe", boom, raising=True)

    task = _make_root_task(actor_done=True)
    state, ctx = _make_observe_state_ctx(task, act_exit_reason="actor_done")
    _with_observe_role(state)

    outcome = await ObserveStep().execute(state, ctx)

    assert launched == [("t1", "finish")], launched
    # 机械判决兜底，且它没有摘要——正是上面那次后台调用要补的东西。
    assert outcome.state_patch["verdict"].reported is False
    assert outcome.state_patch["verdict"].act_recap == ""


# ── act.py: soft-interrupt park fires for root ───────────────────────────────


async def test_act_soft_interrupt_fires_for_root(monkeypatch):
    """act soft-interrupt park (_park_for_interrupt, root task) → launch_recap called once."""
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

    from ctx_weft.core.loop.background import runner as bo_mod
    monkeypatch.setattr(bo_mod, "_task_pending", {})
    monkeypatch.setattr(
        "ctx_weft.core.loop.background.launch_recap",
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
    """act soft-interrupt park with child task → launch_recap NOT called."""
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

    from ctx_weft.core.loop.background import runner as bo_mod
    monkeypatch.setattr(bo_mod, "_task_pending", {})
    monkeypatch.setattr(
        "ctx_weft.core.loop.background.launch_recap",
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
    """act plain-text pause (_park_await_user, root task, interactive) → launch_recap called once."""
    from ctx_weft.core.loop.steps.act import _finish_plain_text_turn
    from tests.hitl_env import make_hitl
    from ctx_weft.providers.events import InProcessEventBus

    launched = []

    def fake_launch(state, ctx, *, boundary=""):
        launched.append((state.task.id,))
        return asyncio.ensure_future(asyncio.sleep(0))

    from ctx_weft.core.loop.background import runner as bo_mod
    monkeypatch.setattr(bo_mod, "_task_pending", {})
    monkeypatch.setattr(
        "ctx_weft.core.loop.background.launch_recap",
        fake_launch,
        raising=False,
    )

    bus = InProcessEventBus()
    mem = InMemoryMemoryProvider()
    hitl, _hitl_reg = make_hitl(bus)

    session = Session(id="s1", tenant_id="default", user_prompt="hi", status="RUNNING")
    task = dataclasses.replace(_make_root_task(status="ACTIVE"))
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
        await _finish_plain_text_turn(state, ctx, turn_num=1, transcript=[])

    assert len(launched) == 1, f"Expected 1 launch for root plain-text pause, got {len(launched)}"


# ── act.py: plain-text pause does NOT fire for child (Task 13) ──────────────


async def test_act_plain_text_pause_child_task_fires(monkeypatch):
    """子任务的纯文本 park **也**触发后台 observe（2026-09-22 去掉 `_is_own_root` 闸）。

    那个闸原先的理由是「非 root 走 LLM observe 向 parent 上报」——但纯文本回合压根到不了
    前台 observe（它在 park 处就退出了），于是子任务的这一段既没人判也没人折。并发不会
    因此失控：单交互线闸门保证一个 session 同时只有一条非 unattended 的线。
    """
    from ctx_weft.core.loop.steps.act import _finish_plain_text_turn
    from tests.hitl_env import make_hitl
    from ctx_weft.providers.events import InProcessEventBus

    launched = []

    def fake_launch(state, ctx, *, boundary=""):
        launched.append((state.task.id,))
        return asyncio.ensure_future(asyncio.sleep(0))

    from ctx_weft.core.loop.background import runner as bo_mod
    monkeypatch.setattr(bo_mod, "_task_pending", {})
    monkeypatch.setattr(
        "ctx_weft.core.loop.background.launch_recap",
        fake_launch,
        raising=False,
    )

    bus = InProcessEventBus()
    mem = InMemoryMemoryProvider()
    hitl, _hitl_reg = make_hitl(bus)

    session = Session(id="s1", tenant_id="default", user_prompt="hi", status="RUNNING")
    child = dataclasses.replace(_make_child_task(status="ACTIVE"), unattended=False)
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
        await _finish_plain_text_turn(state, ctx, turn_num=1, transcript=[])

    assert [t for (t,) in launched] == ["t2"],         f"子任务的纯文本 park 应触发一次后台 observe，实得 {launched}"


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
    """root task delegate suspend → launch_recap(boundary='dispatch') once."""
    launched = []

    def fake_launch(state, ctx, *, boundary=""):
        launched.append((state.task.id, boundary))
        return asyncio.ensure_future(asyncio.sleep(0))

    from ctx_weft.core.loop.background import runner as bo_mod
    monkeypatch.setattr(bo_mod, "_task_pending", {})
    monkeypatch.setattr(
        "ctx_weft.core.loop.background.launch_recap",
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

    from ctx_weft.core.loop.background import runner as bo_mod
    monkeypatch.setattr(bo_mod, "_task_pending", {})
    monkeypatch.setattr(
        "ctx_weft.core.loop.background.launch_recap",
        fake_launch, raising=False,
    )

    child = _make_child_task(status="SUSPENDED")
    state, ctx = _make_suspend_state_ctx(child)

    await SuspendStep().execute(state, ctx)

    assert launched == [("t2", "dispatch")], f"Expected one dispatch launch, got {launched}"
