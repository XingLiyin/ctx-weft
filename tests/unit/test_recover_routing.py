"""recover() 启动恢复（spec/07 §9）——core 内闭环,无回调,启动不 drain。

Task 6 起路由判据换了层：recover() 不再自己宣布会话状态,而是**代 TaskManager**
（进程刚起来,_task_managers 恒为空）发那一条队列状态信号,由 SessionManager 判定:

- 有未决 pending HITL → TaskQueueBlocked      → SessionWaiting
- 没有                → TaskQueueInterrupted  → SessionInterrupted（等 /resume）

「等的是审批面板还是一句话」不上升到会话状态——那是 delivery 的性质,由 host 的
只读入口 session_status_after_recover 推导。决策只折叠 HITL 类事件,不全量回放。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.protocols.events import Event, EventType
from ctx_weft.core.hitl.registry import HITL_STAGE_TOOL
from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_runtime

pytestmark = pytest.mark.asyncio

_TS = datetime(2026, 6, 13, tzinfo=timezone.utc)


def _ev(seq: int, sid: str, type_: EventType, **payload) -> Event:
    return Event(id=f"evt_{sid}_{seq:04d}", run_id="r1", sequence=seq, session_id=sid,
                 type=type_, timestamp=_TS, task_id="t1", payload=payload)


def _capture_interrupts(runtime) -> list[str]:
    """记下被判成 INTERRUPTED 的 session（SM 发的会话级事件,不是 TM 的聚合信号）。"""
    seen: list[str] = []
    async def recorder(ev: Event) -> None:
        if ev.type == EventType.SESSION_INTERRUPTED:
            seen.append(ev.session_id)
    runtime.event_bus.subscribe(None, recorder)
    return seen


def _capture_waiting(runtime) -> list[str]:
    """记下被判成 WAITING 的 session（有人在等回话）。"""
    seen: list[str] = []
    async def recorder(ev: Event) -> None:
        if ev.type == EventType.SESSION_WAITING:
            seen.append(ev.session_id)
    runtime.event_bus.subscribe(None, recorder)
    return seen


async def test_recover_routes_by_pending_hitl(monkeypatch) -> None:
    runtime = make_runtime(agent_provider=InlineAgentTemplateProvider())
    store = runtime.event_store

    # A: 有未解决 pending HITL → 只装填 HitlRegistry
    await store.append(_ev(1, "A", EventType.SESSION_CREATED, template_id="t"))
    await store.append(_ev(2, "A", EventType.HITL_REQUIRED, hitl_id="hA", form="question", tool_call_id="tcA"))
    # B: HITL 已答复 → 无 pending → TaskQueueInterrupted → SessionInterrupted
    await store.append(_ev(1, "B", EventType.SESSION_CREATED, template_id="t"))
    await store.append(_ev(2, "B", EventType.HITL_REQUIRED, hitl_id="hB", form="question"))
    await store.append(_ev(3, "B", EventType.HITL_ANSWERED, hitl_id="hB"))
    # C: 从无 HITL → TaskQueueInterrupted → SessionInterrupted
    await store.append(_ev(1, "C", EventType.SESSION_CREATED, template_id="t"))

    # 启动不应调 recover_session（task 重建推迟到应答）
    called: list[str] = []
    async def fail_recover_session(sid, **kw):
        called.append(sid)
    monkeypatch.setattr(runtime, "recover_session", fail_recover_session)
    interrupted = _capture_interrupts(runtime)
    waiting = _capture_waiting(runtime)

    n = await runtime.recover()

    assert n == 3
    assert called == []                                          # 启动不 drain/不重建 task
    assert [r.id for r in runtime.hitl_registry.list_pending(session_id="A")] == ["hA"]
    # 决定缓存键是三维的 (session, tool_call, stage)——只按 tool_call_id 查会让 A 会话的
    # 批准替 B 会话里同名 id 的调用开门，那正是本次重设计关掉的跨会话授权洞。
    assert runtime.hitl_registry.find_for_tool_call("A", "tcA", HITL_STAGE_TOOL) is not None
    assert runtime.hitl_registry.find_for_tool_call("B", "tcA", HITL_STAGE_TOOL) is None
    assert runtime.hitl_registry.list_pending(session_id="B") == []
    # 无 pending 的两个 → 被进程重启打断，等 /resume。
    assert set(interrupted) == {"B", "C"}
    # 有人在等回话的那个 → WAITING，不是 INTERRUPTED（绝不把 parked 任务孤立）。
    assert waiting == ["A"]
    # 判定落进了 SM 的内存态，不只是发了个事件。
    assert runtime._session_manager.status_of("A") == "WAITING"
    assert runtime._session_manager.status_of("B") == "INTERRUPTED"
    assert runtime._session_manager.status_of("C") == "INTERRUPTED"


async def test_recover_multi_hitl_partial_resolve_still_pending() -> None:
    """两个 pending、只解决一个 → 仍 pending → 重建剩余、判 WAITING 而非 INTERRUPTED。"""
    runtime = make_runtime(agent_provider=InlineAgentTemplateProvider())
    store = runtime.event_store
    await store.append(_ev(1, "M", EventType.SESSION_CREATED, template_id="t"))
    await store.append(_ev(2, "M", EventType.HITL_REQUIRED, hitl_id="h1", form="question"))
    await store.append(_ev(3, "M", EventType.HITL_REQUIRED, hitl_id="h2", form="approval"))
    await store.append(_ev(4, "M", EventType.HITL_ANSWERED, hitl_id="h1"))

    interrupted = _capture_interrupts(runtime)
    waiting = _capture_waiting(runtime)
    await runtime.recover()

    assert {r.id for r in runtime.hitl_registry.list_pending(session_id="M")} == {"h2"}
    assert interrupted == []
    assert waiting == ["M"]
