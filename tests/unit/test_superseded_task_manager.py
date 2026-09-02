"""被同一 session 上更新的 TaskManager 顶替后，旧 TM 的收尾必须变 no-op。

复现的 bug：后台 observe 尚未跑完时用户开启下一轮对话 → 旧 TM 的 `_fire_session_done`
迟发 `SessionFinished` + 触发 `_release_session`，冲掉新一轮已进入的 HITL 挂起态，
任务卡在 ACTIVE、会话显示已结束（详见 session ses_01KWGR112XHFYQYR3HES8D4MYK）。

关键：顶替可能发生在 `_fire_session_done` 等待后台任务（gather）期间，
所以归属权判定必须放在 gather **之后**。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.providers.events import InProcessEventBus
from ctx_weft.protocols.events import Event, EventType
from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.state.models import NormalTaskSettings, Session, Task
from ctx_weft.providers.llm.mock import MockLLMAdapter
from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_runtime
from tests.unit._stub_runner import StubRunner

pytestmark = pytest.mark.asyncio


async def _noop_runner(_sid, _tid):
    return None


def _running_tm(rt: CtxWeftRuntime, session: Session, task_id: str) -> TaskManager:
    """构造一个「有一个在跑任务」的 TaskManager，并经 runtime 真实 wiring 注册。"""
    tm = TaskManager(session_id=session.id, event_bus=rt._event_bus, max_concurrent=1)
    tm.set_runner(StubRunner(tm, _noop_runner))
    tm.set_session(session)
    tm.register_task(Task(id=task_id, session_id=session.id, status="ACTIVE",
                          assigned_agent_id="a", creator_agent_id="a",
                          settings=NormalTaskSettings()))
    tm._running_tasks.add(task_id)
    rt._register_and_drain(session, tm)
    return tm


def _tm(bus: InProcessEventBus) -> TaskManager:
    tm = TaskManager(session_id="s1", event_bus=bus, max_concurrent=1)
    tm.set_session(Session(id="s1", tenant_id="default", user_prompt="x",
                           status="SUCCEEDED", token_budget=0))
    return tm


async def test_superseded_tm_skips_finish_when_replaced_during_gather() -> None:
    bus = InProcessEventBus()
    finished: list = []

    async def _capture(ev):
        finished.append(ev)

    bus.subscribe(EventType.TASK_QUEUE_DRAINED, _capture)

    tm = _tm(bus)
    done_called: list = []

    async def _on_done():
        done_called.append(True)

    tm.set_session_done_callback(_on_done)

    current = {"v": True}
    tm.set_is_current(lambda: current["v"])

    # 后台任务：卡住 gather 直到我们放行（模拟慢的 background observe）
    release = asyncio.Event()

    async def _blocker():
        await release.wait()

    bg = asyncio.create_task(_blocker())
    tm.track_background(bg)

    fire = asyncio.create_task(tm._fire_session_done())
    await asyncio.sleep(0)  # 让 _fire_session_done 进入 gather 阻塞
    await asyncio.sleep(0)

    # 等待期间被新一轮 TaskManager 顶替
    current["v"] = False
    release.set()
    await fire

    assert finished == [], "被顶替的旧 TM 不应报队列状态（→ 不会有 SessionFinished）"
    assert done_called == [], "被顶替的旧 TM 不应触发终结回调（_release_session）"


async def test_current_tm_still_fires_session_finished() -> None:
    """Task 6：TM 报 TaskQueueDrained（SessionFinished 由 SM 据它发），回调照常触发。"""
    bus = InProcessEventBus()
    drained: list = []

    async def _capture(ev):
        drained.append(ev)

    bus.subscribe(EventType.TASK_QUEUE_DRAINED, _capture)

    tm = _tm(bus)
    done_called: list = []

    async def _on_done():
        done_called.append(True)

    tm.set_session_done_callback(_on_done)
    tm.set_is_current(lambda: True)

    await tm._fire_session_done()

    assert len(drained) == 1
    assert drained[0].payload["final_status"] == "SUCCEEDED"
    assert done_called == [True]


async def test_superseded_tm_park_does_not_announce_queue_state() -> None:
    """**idle 路径**同样受归属权守卫：被顶替的旧 TM 迟到的 park 收尾不得报队列状态。

    这条是 `_fire_session_done` 那条不变量的孪生兄弟，而且更隐蔽：`announce_queue_state`
    发的是**会话级**信号，SM 按 session_id 无条件应用——旧 TM 报一句 TaskQueueBlocked，
    新一轮正在跑的会话就被翻成 WAITING。`_fire_session_idle` 从前 docstring 明写「不发
    事件」、因此无守卫；它现在会发了，守卫必须跟上。
    """
    tm = _tm(InProcessEventBus())
    signals: list = []

    async def _cap(ev):
        if ev.type in (EventType.TASK_QUEUE_BLOCKED, EventType.TASK_QUEUE_INTERRUPTED,
                       EventType.TASK_QUEUE_DRAINED):
            signals.append(ev.type)

    tm._event_bus.subscribe(None, _cap)
    idle_called: list = []

    async def _on_idle():
        idle_called.append(True)

    tm.set_session_idle_callback(_on_idle)
    tm.register_task(Task(id="A", session_id="s1", status="AWAITING_HUMAN",
                          assigned_agent_id="a", creator_agent_id="a",
                          settings=NormalTaskSettings()))

    tm.set_is_current(lambda: True)
    await tm._fire_session_idle()
    assert signals == [EventType.TASK_QUEUE_BLOCKED], "对照：current TM 照常报「有人在等」"

    signals.clear()
    tm.set_is_current(lambda: False)          # 被同 session 上更新的 TM 顶替
    await tm._fire_session_idle()
    assert signals == [], "被顶替的旧 TM 的 park 收尾不得报队列状态（会翻掉新一轮的会话）"


async def test_superseded_tm_crash_suspend_does_not_announce_queue_state() -> None:
    """同上，走 `_suspend_task_interrupted` 那条裸调用路径（崩溃收尾）。"""
    tm = _tm(InProcessEventBus())
    signals: list = []

    async def _cap(ev):
        if ev.type == EventType.TASK_QUEUE_INTERRUPTED:
            signals.append(ev.type)

    tm._event_bus.subscribe(None, _cap)

    async def _noop_runner(_sid, _tid):
        return None

    tm.set_runner(StubRunner(tm, _noop_runner))
    tm.register_task(Task(id="A", session_id="s1", status="ACTIVE",
                          assigned_agent_id="a", creator_agent_id="a",
                          settings=NormalTaskSettings()))
    tm.set_is_current(lambda: False)          # 已被顶替

    class _NonRetriable(Exception):
        retriable = False

    await tm._handle_task_failure("A", error="boom", exc=_NonRetriable("boom"))

    assert signals == [], "被顶替的旧 TM 的崩溃收尾不得报队列状态"


async def test_register_and_drain_marks_older_tm_not_current() -> None:
    rt = make_runtime(llm=MockLLMAdapter(responses=[]),
                        agent_provider=InlineAgentTemplateProvider())
    sess = Session(id="s1", tenant_id="default", user_prompt="x",
                   status="RUNNING", token_budget=0)

    async def _noop_runner(_sid, _tid):
        return None

    tm_old = TaskManager(session_id="s1", event_bus=rt._event_bus, max_concurrent=1)
    tm_old.set_runner(StubRunner(tm_old, _noop_runner))
    tm_new = TaskManager(session_id="s1", event_bus=rt._event_bus, max_concurrent=1)
    tm_new.set_runner(StubRunner(tm_new, _noop_runner))

    rt._register_and_drain(sess, tm_old)
    rt._register_and_drain(sess, tm_new)

    assert tm_old._is_current is not None and tm_old._is_current() is False
    assert tm_new._is_current is not None and tm_new._is_current() is True


async def test_superseded_tm_drain_does_not_dispatch() -> None:
    """被顶替的 TM（_is_current()==False）的 drain 不派发任何任务——防重叠 resume 下两套 drain。"""
    tm = _tm(InProcessEventBus())
    started: list = []

    async def runner(_sid, tid):
        started.append(tid)

    tm.set_runner(StubRunner(tm, runner))
    tm.set_is_current(lambda: False)  # 已被同 session 上更新的 TM 顶替
    await tm.push_task(Task(id="A", session_id="s1", status="PENDING",
                            assigned_agent_id="a", creator_agent_id="a",
                            settings=NormalTaskSettings()))
    await tm.drain()
    await asyncio.sleep(0)  # 若误 create_task(_run_task) 给它跑的机会
    assert started == [], "被顶替的 TM 的 drain 不应派发任务"


async def test_current_tm_drain_dispatches() -> None:
    """对照：current TM（_is_current()==True）的 drain 正常派发。"""
    tm = _tm(InProcessEventBus())
    started: list = []
    release = asyncio.Event()

    async def runner(_sid, tid):
        started.append(tid)
        await release.wait()  # park 住，避免收尾级联干扰断言

    tm.set_runner(StubRunner(tm, runner))
    tm.set_is_current(lambda: True)
    await tm.push_task(Task(id="A", session_id="s1", status="PENDING",
                            assigned_agent_id="a", creator_agent_id="a",
                            settings=NormalTaskSettings()))
    await tm.drain()
    await asyncio.sleep(0)
    assert started == ["A"]
    release.set()
    await asyncio.sleep(0)


async def test_recover_session_serialized_per_session(monkeypatch) -> None:
    """per-session resume 锁：同一 session 的两次并发 recover_session 串行执行（不并发建两套 drain）。

    用一个在闸门处阻塞的 fake rebuild_view 观测并发度：有锁 → 峰值并发 1；无锁 → 2。
    """
    rt = make_runtime(llm=MockLLMAdapter(responses=[]),
                        agent_provider=InlineAgentTemplateProvider())
    active = {"n": 0, "max": 0}
    gate = asyncio.Event()

    async def fake_rebuild_view(_store, _sid):
        active["n"] += 1
        active["max"] = max(active["max"], active["n"])
        await gate.wait()
        active["n"] -= 1
        raise RuntimeError("stop after gate")  # 短路 recover 余下步骤，释放锁

    monkeypatch.setattr("ctx_weft.core.control.reducers.rebuild_view", fake_rebuild_view)

    t1 = asyncio.create_task(rt.recover_session("s1"))
    t2 = asyncio.create_task(rt.recover_session("s1"))
    await asyncio.sleep(0.02)  # 两个都尝试进入锁
    assert active["max"] == 1, "per-session 锁应串行化两次 recover（峰值并发=1）"
    gate.set()
    res = await asyncio.gather(t1, t2, return_exceptions=True)
    assert all(isinstance(r, RuntimeError) for r in res)


async def test_recover_session_different_sessions_not_serialized(monkeypatch) -> None:
    """不同 session 之间不应被 resume 锁串行化（各自独立锁，可并发）。"""
    rt = make_runtime(llm=MockLLMAdapter(responses=[]),
                        agent_provider=InlineAgentTemplateProvider())
    active = {"n": 0, "max": 0}
    gate = asyncio.Event()

    async def fake_rebuild_view(_store, _sid):
        active["n"] += 1
        active["max"] = max(active["max"], active["n"])
        await gate.wait()
        active["n"] -= 1
        raise RuntimeError("stop after gate")

    monkeypatch.setattr("ctx_weft.core.control.reducers.rebuild_view", fake_rebuild_view)

    t1 = asyncio.create_task(rt.recover_session("sA"))
    t2 = asyncio.create_task(rt.recover_session("sB"))
    await asyncio.sleep(0.02)
    assert active["max"] == 2, "不同 session 的 recover 应能并发（峰值并发=2）"
    gate.set()
    await asyncio.gather(t1, t2, return_exceptions=True)


async def test_slow_prior_turn_bg_observe_does_not_clobber_next_turn() -> None:
    """端到端复现：第 N 轮 finish_task 后其 background observe 拖久了，第 N+1 轮已开启并
    进入 HITL 挂起；旧 TM 迟到的收尾必须 no-op——不释放新 TM、不发 SessionFinished。"""
    rt = make_runtime(llm=MockLLMAdapter(responses=[]),
                        agent_provider=InlineAgentTemplateProvider())
    sid = "s1"
    finished: list = []

    async def _capture(ev):
        finished.append(ev)

    rt._event_bus.subscribe(EventType.SESSION_FINISHED, _capture)

    # ── 第 N 轮 "6"：任务完成，但其 background observe 慢 ──
    sess6 = Session(id=sid, tenant_id="default", user_prompt="6", status="RUNNING", token_budget=0)
    tm6 = _running_tm(rt, sess6, "t6")
    release = asyncio.Event()

    async def _slow_bg():
        await release.wait()

    tm6.track_background(asyncio.create_task(_slow_bg()))

    # 任务完成 → is_done → _fire_session_done → gather 阻塞
    done6 = asyncio.create_task(tm6.on_task_finished("t6", status="FINISHED"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # ── 第 N+1 轮 "7"：新 TM 接管；模拟其 HITL 挂起（runtime 侧保留 pause token）──
    sess7 = Session(id=sid, tenant_id="default", user_prompt="7", status="RUNNING", token_budget=0)
    tm7 = _running_tm(rt, sess7, "t7")
    rt._pausing.add(sid)

    # ── 旧轮的慢 bg observe 现在才跑完 → tm6._fire_session_done 从 gather 恢复 ──
    release.set()
    await done6

    assert rt._task_managers[sid] is tm7, "被顶替的旧 TM 不得释放掉接管的新 TM"
    assert sid in rt._pausing, "新一轮的 pause 闩锁不得被旧 TM 迟到收尾清除"
    assert finished == [], "被顶替的旧 TM 不得发 SessionFinished"


async def test_probe_prior_turn_finishing_after_supersession() -> None:
    """探针：强制让旧轮的 on_task_finished 在被顶替**之后**才跑（forced ordering）。

    验证关键不变量仍成立：不释放新 TM、不发 SessionFinished。
    is_done 分支已加归属权守卫，被顶替旧 TM 收尾时连 stale 的会话级聚合信号
    （TaskQueueDrained）也不再发出——这里断言其为空。
    """
    rt = make_runtime(llm=MockLLMAdapter(responses=[]),
                        agent_provider=InlineAgentTemplateProvider())
    sid = "s1"
    statuses: list = []
    finished: list = []

    async def _cap(ev):
        if ev.type == EventType.TASK_QUEUE_DRAINED:
            statuses.append(ev.payload.get("final_status"))
        elif ev.type == EventType.SESSION_FINISHED:
            finished.append(ev)

    rt._event_bus.subscribe(None, _cap)

    sess6 = Session(id=sid, tenant_id="default", user_prompt="6", status="RUNNING", token_budget=0)
    tm6 = _running_tm(rt, sess6, "t6")

    # 新轮先接管（顶替），旧轮的任务此后才结束
    sess7 = Session(id=sid, tenant_id="default", user_prompt="7", status="RUNNING", token_budget=0)
    tm7 = _running_tm(rt, sess7, "t7")
    rt._pausing.add(sid)

    await tm6.on_task_finished("t6", status="FINISHED")

    # 关键不变量：迟到的旧轮收尾不得造成不可恢复的破坏——不释放新 TM、不发 SessionFinished。
    assert rt._task_managers[sid] is tm7
    assert sid in rt._pausing, "新一轮的 pause 闩锁不得被旧 TM 迟到收尾清除"
    assert finished == [], "被顶替的旧 TM 不得发 SessionFinished"

    # is_done 分支已加归属权守卫：被顶替旧 TM 的迟到收尾连 stale 的 TaskQueueDrained
    # 也不发出（新 owner 的状态才是真相），故这里 statuses 为空。
    assert statuses == []
