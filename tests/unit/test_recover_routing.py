"""recover() 启动恢复（spec/07 §9）——core 内闭环,无回调,启动不 drain。

Task 6 起路由判据换了层：recover() 不再自己宣布会话状态,而是**代 TaskManager**
（进程刚起来,_task_managers 恒为空）发那一条队列状态信号:

- 有未决 pending HITL → TaskQueueBlocked
- 没有                → TaskQueueInterrupted（等 /resume）

Task 16 起：SessionManager 随会话状态机一并降格,不再消费这条信号译成会话级
SessionWaiting/SessionInterrupted 事件——本文件因此直接钉住 TM 的这条聚合信号
本身（它是 recover() 真正发出的、也是退役前 SM 唯一消费的同一条输入）,不再断言
已经不存在的会话级事件或 `SessionManager.status_of`。

「等的是审批面板还是一句话」不上升到任何状态事件——那是 delivery 的性质,由 host 的
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


def _capture_interrupted_signal(runtime) -> list[str]:
    """记下被 TM 报 TaskQueueInterrupted 的 session（无 pending、被进程重启打断）。

    Task 16 起：这是 recover() 真正发出的信号本身,不再是已退役的 SM 会话级事件。
    """
    seen: list[str] = []
    async def recorder(ev: Event) -> None:
        if ev.type == EventType.TASK_QUEUE_INTERRUPTED:
            seen.append(ev.session_id)
    runtime.event_bus.subscribe(None, recorder)
    return seen


def _capture_blocked_signal(runtime) -> list[str]:
    """记下被 TM 报 TaskQueueBlocked 的 session（有人在等回话）。"""
    seen: list[str] = []
    async def recorder(ev: Event) -> None:
        if ev.type == EventType.TASK_QUEUE_BLOCKED:
            seen.append(ev.session_id)
    runtime.event_bus.subscribe(None, recorder)
    return seen


async def test_recover_routes_by_pending_hitl(monkeypatch) -> None:
    runtime = make_runtime(agent_provider=InlineAgentTemplateProvider())
    store = runtime.event_store

    # A: 有未解决 pending HITL → 只装填 HitlRegistry
    await store.append(_ev(1, "A", EventType.SESSION_CREATED, template_id="t"))
    await store.append(_ev(2, "A", EventType.HITL_REQUIRED, hitl_id="hA", form="question", tool_call_id="tcA"))
    # B: HITL 已答复 → 无 pending → TaskQueueInterrupted
    await store.append(_ev(1, "B", EventType.SESSION_CREATED, template_id="t"))
    await store.append(_ev(2, "B", EventType.HITL_REQUIRED, hitl_id="hB", form="question"))
    await store.append(_ev(3, "B", EventType.HITL_ANSWERED, hitl_id="hB"))
    # C: 从无 HITL → TaskQueueInterrupted
    await store.append(_ev(1, "C", EventType.SESSION_CREATED, template_id="t"))

    # 启动不应调 recover_session（task 重建推迟到应答）
    called: list[str] = []
    async def fail_recover_session(sid, **kw):
        called.append(sid)
    monkeypatch.setattr(runtime, "recover_session", fail_recover_session)
    interrupted = _capture_interrupted_signal(runtime)
    blocked = _capture_blocked_signal(runtime)

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
    # 有人在等回话的那个 → TaskQueueBlocked，不是 Interrupted（绝不把 parked 任务孤立）。
    assert blocked == ["A"]


async def test_recover_multi_hitl_partial_resolve_still_pending() -> None:
    """两个 pending、只解决一个 → 仍 pending → 重建剩余、报 TaskQueueBlocked 而非 Interrupted。"""
    runtime = make_runtime(agent_provider=InlineAgentTemplateProvider())
    store = runtime.event_store
    await store.append(_ev(1, "M", EventType.SESSION_CREATED, template_id="t"))
    await store.append(_ev(2, "M", EventType.HITL_REQUIRED, hitl_id="h1", form="question"))
    await store.append(_ev(3, "M", EventType.HITL_REQUIRED, hitl_id="h2", form="approval"))
    await store.append(_ev(4, "M", EventType.HITL_ANSWERED, hitl_id="h1"))

    interrupted = _capture_interrupted_signal(runtime)
    blocked = _capture_blocked_signal(runtime)
    await runtime.recover()

    assert {r.id for r in runtime.hitl_registry.list_pending(session_id="M")} == {"h2"}
    assert interrupted == []
    assert blocked == ["M"]
