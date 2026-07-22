"""recover() 据事件决策恢复策略（spec/07 §9）——core 内闭环,无回调,启动不 drain。

有未解决 pending HITL 的 session → **只重建内存 HitlManager**（task 重建+续跑推迟到应答的
recover_session）;否则 → **emit SessionStatusChanged(INTERRUPTED)**。决策只折叠 HITL 类事件,不全量回放。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.events.types import Event, EventType
from tests.integration.test_minimal_loop import InMemoryTemplateResolver, make_runtime

pytestmark = pytest.mark.asyncio

_TS = datetime(2026, 6, 13, tzinfo=timezone.utc)


def _ev(seq: int, sid: str, type_: EventType, **payload) -> Event:
    return Event(id=f"evt_{sid}_{seq:04d}", run_id="r1", sequence=seq, session_id=sid,
                 type=type_, timestamp=_TS, task_id="t1", payload=payload)


def _capture_interrupts(runtime) -> list[str]:
    seen: list[str] = []
    async def recorder(ev: Event) -> None:
        if ev.type == EventType.SESSION_STATUS_CHANGED and (ev.payload or {}).get("new_status") == "INTERRUPTED":
            seen.append(ev.session_id)
    runtime.event_bus.subscribe(None, recorder)
    return seen


async def test_recover_routes_by_pending_hitl(monkeypatch) -> None:
    runtime = make_runtime(template_resolver=InMemoryTemplateResolver())
    store = runtime.event_store

    # A: 有未解决 pending HITL → 只重建 HitlManager
    await store.append(_ev(1, "A", EventType.SESSION_CREATED, template_id="t"))
    await store.append(_ev(2, "A", EventType.HITL_REQUIRED, hitl_id="hA", form="question", tool_call_id="tcA"))
    # B: HITL 已答复 → 无 pending → interrupt(event)
    await store.append(_ev(1, "B", EventType.SESSION_CREATED, template_id="t"))
    await store.append(_ev(2, "B", EventType.HITL_REQUIRED, hitl_id="hB", form="question"))
    await store.append(_ev(3, "B", EventType.HITL_ANSWERED, hitl_id="hB"))
    # C: 从无 HITL → interrupt(event)
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
    assert [r.id for r in runtime.hitl_manager.list_pending(session_id="A")] == ["hA"]
    assert runtime.hitl_manager.find_for_tool_call("tcA") is not None
    assert runtime.hitl_manager.list_pending(session_id="B") == []
    assert set(interrupted) == {"B", "C"}                        # 其余 emit INTERRUPTED


async def test_recover_multi_hitl_partial_resolve_still_pending() -> None:
    """两个 pending、只解决一个 → 仍 pending → 重建剩余、不发 INTERRUPTED。"""
    runtime = make_runtime(template_resolver=InMemoryTemplateResolver())
    store = runtime.event_store
    await store.append(_ev(1, "M", EventType.SESSION_CREATED, template_id="t"))
    await store.append(_ev(2, "M", EventType.HITL_REQUIRED, hitl_id="h1", form="question"))
    await store.append(_ev(3, "M", EventType.HITL_REQUIRED, hitl_id="h2", form="approval"))
    await store.append(_ev(4, "M", EventType.HITL_ANSWERED, hitl_id="h1"))

    interrupted = _capture_interrupts(runtime)
    await runtime.recover()

    assert {r.id for r in runtime.hitl_manager.list_pending(session_id="M")} == {"h2"}
    assert interrupted == []
