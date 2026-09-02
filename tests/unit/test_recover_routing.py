"""recover() 启动恢复（spec/07 §9）——core 内闭环,无回调,启动不 drain。

Task 6 起**没有分支**：每个 active session 一律装填内存 HitlRegistry、登记进
SessionManager,然后（若有活 TM）让 TM 照常聚合队列状态。「复活不是一种状态」——
启动本身不宣布会话怎么了,会话状态仍只由 TM 的聚合信号驱动 SM 判定。
决策只折叠 HITL 类事件,不全量回放。
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


#: 启动恢复期不该再出现的会话级宣告——旧实现在这里分流「PAUSED_HITL vs INTERRUPTED」。
_SESSION_VERDICTS = (
    EventType.SESSION_STATUS_CHANGED,
    EventType.SESSION_INTERRUPTED,
    EventType.SESSION_WAITING,
    EventType.SESSION_FINISHED,
)


def _capture_interrupts(runtime) -> list[str]:
    seen: list[str] = []
    async def recorder(ev: Event) -> None:
        if ev.type in _SESSION_VERDICTS:
            seen.append(ev.session_id)
    runtime.event_bus.subscribe(None, recorder)
    return seen


async def test_recover_registers_every_session_without_branching(monkeypatch) -> None:
    runtime = make_runtime(agent_provider=InlineAgentTemplateProvider())
    store = runtime.event_store

    # A: 有未解决 pending HITL → 只装填 HitlRegistry
    await store.append(_ev(1, "A", EventType.SESSION_CREATED, template_id="t"))
    await store.append(_ev(2, "A", EventType.HITL_REQUIRED, hitl_id="hA", form="question", tool_call_id="tcA"))
    # B: HITL 已答复 → 无 pending
    await store.append(_ev(1, "B", EventType.SESSION_CREATED, template_id="t"))
    await store.append(_ev(2, "B", EventType.HITL_REQUIRED, hitl_id="hB", form="question"))
    await store.append(_ev(3, "B", EventType.HITL_ANSWERED, hitl_id="hB"))
    # C: 从无 HITL
    await store.append(_ev(1, "C", EventType.SESSION_CREATED, template_id="t"))

    # 启动不应调 recover_session（task 重建推迟到应答）
    called: list[str] = []
    async def fail_recover_session(sid, **kw):
        called.append(sid)
    monkeypatch.setattr(runtime, "recover_session", fail_recover_session)
    interrupted = _capture_interrupts(runtime)

    n = await runtime.recover()

    assert n == 3
    assert called == []                                          # 启动不 drain/不重建 task
    assert [r.id for r in runtime.hitl_registry.list_pending(session_id="A")] == ["hA"]
    # 决定缓存键是三维的 (session, tool_call, stage)——只按 tool_call_id 查会让 A 会话的
    # 批准替 B 会话里同名 id 的调用开门，那正是本次重设计关掉的跨会话授权洞。
    assert runtime.hitl_registry.find_for_tool_call("A", "tcA", HITL_STAGE_TOOL) is not None
    assert runtime.hitl_registry.find_for_tool_call("B", "tcA", HITL_STAGE_TOOL) is None
    assert runtime.hitl_registry.list_pending(session_id="B") == []
    # 启动恢复不宣布会话状态：没有活 TM 就没有聚合信号，也就没有 SM 的判定。
    assert interrupted == []
    # 但三个 session 都已纳入 SM 管理，之后第一条聚合信号就能落到正确的状态上。
    assert all(runtime._session_manager.status_of(sid) == "RUNNING" for sid in ("A", "B", "C"))


async def test_recover_multi_hitl_partial_resolve_still_pending() -> None:
    """两个 pending、只解决一个 → 仍 pending → 重建剩余、不宣布会话状态。"""
    runtime = make_runtime(agent_provider=InlineAgentTemplateProvider())
    store = runtime.event_store
    await store.append(_ev(1, "M", EventType.SESSION_CREATED, template_id="t"))
    await store.append(_ev(2, "M", EventType.HITL_REQUIRED, hitl_id="h1", form="question"))
    await store.append(_ev(3, "M", EventType.HITL_REQUIRED, hitl_id="h2", form="approval"))
    await store.append(_ev(4, "M", EventType.HITL_ANSWERED, hitl_id="h1"))

    interrupted = _capture_interrupts(runtime)
    await runtime.recover()

    assert {r.id for r in runtime.hitl_registry.list_pending(session_id="M")} == {"h2"}
    assert interrupted == []
