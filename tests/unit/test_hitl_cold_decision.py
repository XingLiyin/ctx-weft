"""HITL 冷决定查询（事件日志回落）——reconcile 短路跨重启（spec/07 §6 补遗）。

热路径的决定缓存（find_for_tool_call）是纯内存：重启后只重建 pending、不重建已解决，
"回答 → 续跑到 reconcile"之间夹一次重启，答案就丢了、同一问题会被重新问（0.4.19 旧数据
"已答复 HITL 重现"的产生源头）。本组测试锁定三层修复：
1. resolve 事件补记 message / modified_arguments——事件日志自足，否则查了也还原不出答案；
2. fold_cold_hitl_decision：从 HITL 事件折出某 tool_call 的**可用**人工决定
   （旧事件缺 message / 改参的视为不可用 → 调用方重新问，绝不臆造答案）；
3. HitlManager.find_resolved_for_tool_call：内存命中优先，未命中回落冷查询（运行时绑定）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ctx_weft.core.control.reducers import fold_cold_hitl_decision
from ctx_weft.protocols.events import Event, EventType
from ctx_weft.core.orchestrator.control_capability import (
    PROVIDER_NAME,
    ControlCapabilityProvider,
)
from ctx_weft.core.orchestrator.hitl_manager import HitlManager
from ctx_weft.core.state.models import HitlRequest, Session, Task
from ctx_weft.protocols import ProviderContext

_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _ev(t: EventType, payload: dict, sec: int, *, task_id="t1", session_id="s1") -> Event:
    return Event(
        id=f"evt_{sec:04d}", run_id="r1", sequence=sec, session_id=session_id,
        type=t, timestamp=_BASE + timedelta(seconds=sec), task_id=task_id, payload=payload,
    )


def _required(rid: str, tcid: str, sec: int, question: str = "Which DB?") -> Event:
    return _ev(EventType.HITL_REQUIRED, {
        "hitl_id": rid, "form": "question", "capability_id": "control:ask_user",
        "tool_call_id": tcid, "question": question,
    }, sec)


# ── 1) fold_cold_hitl_decision：事件 → 可用人工决定 ──────────────────────────────


def test_cold_decision_answered_with_message() -> None:
    events = [
        _required("h1", "tc1", 1),
        _ev(EventType.HITL_ANSWERED, {"hitl_id": "h1", "message": "use postgres"}, 2),
    ]
    req = fold_cold_hitl_decision(events, "tc1")
    assert req is not None
    assert req.status == "accepted" and req.message == "use postgres"
    assert req.tool_call_id == "tc1" and req.id == "h1"


def test_cold_decision_answered_without_message_is_unusable() -> None:
    """旧事件只有 hitl_id：知道"答过"但还原不出内容 → 不可用（调用方重新问,不臆造）。"""
    events = [
        _required("h1", "tc1", 1),
        _ev(EventType.HITL_ANSWERED, {"hitl_id": "h1"}, 2),
    ]
    assert fold_cold_hitl_decision(events, "tc1") is None


def test_cold_decision_approved_and_rejected_usable_without_message() -> None:
    """approve/reject 无 message 也可执行（放行/拦下本身就是决定）。"""
    events = [
        _required("a1", "tcA", 1),
        _ev(EventType.HITL_APPROVED, {"hitl_id": "a1"}, 2),
        _required("r1", "tcR", 3),
        _ev(EventType.HITL_REJECTED, {"hitl_id": "r1", "message": "no prod"}, 4),
    ]
    ok = fold_cold_hitl_decision(events, "tcA")
    assert ok is not None and ok.status == "accepted"
    no = fold_cold_hitl_decision(events, "tcR")
    assert no is not None and no.status == "rejected" and no.message == "no prod"


def test_cold_decision_modified_requires_args() -> None:
    """HitlModified 的语义=改参放行：事件缺 modified_arguments（旧数据）→ 不可用,
    否则会拿原参数执行、违背用户改参意图。"""
    events = [
        _required("m1", "tcM", 1),
        _ev(EventType.HITL_MODIFIED, {"hitl_id": "m1"}, 2),
    ]
    assert fold_cold_hitl_decision(events, "tcM") is None

    events2 = [
        _required("m2", "tcM2", 1),
        _ev(EventType.HITL_MODIFIED,
            {"hitl_id": "m2", "modified_arguments": {"cmd": "ls"}}, 2),
    ]
    req = fold_cold_hitl_decision(events2, "tcM2")
    assert req is not None and req.status == "accepted"
    assert req.modified_arguments == {"cmd": "ls"}


def test_cold_decision_cancelled_pending_or_no_tcid_none() -> None:
    events = [
        _required("c1", "tcC", 1),
        _ev(EventType.HITL_CANCELLED, {"hitl_id": "c1"}, 2),   # 取消 ≠ 决定
        _required("p1", "tcP", 3),                              # 仍 pending
    ]
    assert fold_cold_hitl_decision(events, "tcC") is None
    assert fold_cold_hitl_decision(events, "tcP") is None
    assert fold_cold_hitl_decision(events, "") is None
    assert fold_cold_hitl_decision(events, "tc_nowhere") is None


def test_cold_decision_duplicate_requireds_usable_resolution_wins() -> None:
    """重问副本场景：同 tool_call_id 两条 Required,先答后重问 → 用已答的那条。"""
    events = [
        _required("h1", "tc_dup", 1),
        _ev(EventType.HITL_ANSWERED, {"hitl_id": "h1", "message": "yes, proceed"}, 2),
        _required("h2", "tc_dup", 3),   # 重问副本,pending
    ]
    req = fold_cold_hitl_decision(events, "tc_dup")
    assert req is not None and req.id == "h1" and req.message == "yes, proceed"


# ── 2) resolve 事件补记内容 ─────────────────────────────────────────────────────


class _CollectBus:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_resolve_events_carry_message_and_modified_arguments() -> None:
    bus = _CollectBus()
    mgr = HitlManager(event_bus=bus)

    r1 = await mgr.request(form="question", session_id="s1", task_id="t1", tool_call_id="tc1")
    await mgr.answer(r1, "use postgres")
    answered = [e for e in bus.events if e.type == EventType.HITL_ANSWERED][-1]
    assert answered.payload["message"] == "use postgres"

    r2 = await mgr.request(form="approval", session_id="s1", task_id="t1", tool_call_id="tc2")
    await mgr.approve(r2, modified_arguments={"cmd": "ls"})
    modified = [e for e in bus.events if e.type == EventType.HITL_MODIFIED][-1]
    assert modified.payload["modified_arguments"] == {"cmd": "ls"}

    r3 = await mgr.request(form="approval", session_id="s1", task_id="t1", tool_call_id="tc3")
    await mgr.reject(r3, message="no prod access")
    rejected = [e for e in bus.events if e.type == EventType.HITL_REJECTED][-1]
    assert rejected.payload["message"] == "no prod access"


# ── 3) find_resolved_for_tool_call：内存优先,回落冷查询 ─────────────────────────


def _resolved_req(tcid: str) -> HitlRequest:
    return HitlRequest(
        id="h_cold", form="question", session_id="s1", task_id="t1",
        tool_call_id=tcid, question="Which DB?", status="accepted", message="use postgres",
    )


@pytest.mark.asyncio
async def test_find_resolved_prefers_memory() -> None:
    mgr = HitlManager()
    calls: list[tuple[str, str]] = []

    async def lookup(session_id: str, tool_call_id: str) -> HitlRequest | None:
        calls.append((session_id, tool_call_id))
        return _resolved_req(tool_call_id)

    mgr.set_cold_decision_lookup(lookup)
    rid = await mgr.request(form="question", session_id="s1", task_id="t1", tool_call_id="tcX")
    await mgr.answer(rid, "in-memory answer")

    req = await mgr.find_resolved_for_tool_call("s1", "tcX")
    assert req is not None and req.message == "in-memory answer"
    assert calls == []   # 内存命中,不落冷查询


@pytest.mark.asyncio
async def test_find_resolved_falls_back_to_cold_lookup() -> None:
    mgr = HitlManager()   # 空内存 = 重启后
    calls: list[tuple[str, str]] = []

    async def lookup(session_id: str, tool_call_id: str) -> HitlRequest | None:
        calls.append((session_id, tool_call_id))
        return _resolved_req(tool_call_id)

    mgr.set_cold_decision_lookup(lookup)
    req = await mgr.find_resolved_for_tool_call("s1", "tc_cold")
    assert req is not None and req.message == "use postgres"
    assert calls == [("s1", "tc_cold")]


@pytest.mark.asyncio
async def test_find_resolved_pending_in_memory_short_circuits_to_none() -> None:
    """内存里同 tool_call_id 还是 pending（活的等待）→ 返回 None,让 request() 幂等复用,
    不得用日志里的旧决定盖掉活请求。"""
    mgr = HitlManager()

    async def lookup(session_id: str, tool_call_id: str) -> HitlRequest | None:
        raise AssertionError("pending in memory must not trigger cold lookup")

    mgr.set_cold_decision_lookup(lookup)
    await mgr.request(form="question", session_id="s1", task_id="t1", tool_call_id="tcP")
    assert await mgr.find_resolved_for_tool_call("s1", "tcP") is None


@pytest.mark.asyncio
async def test_find_resolved_without_lookup_or_tcid_is_none() -> None:
    mgr = HitlManager()
    assert await mgr.find_resolved_for_tool_call("s1", "tc_none") is None
    assert await mgr.find_resolved_for_tool_call("s1", "") is None


# ── 4) 端到端：重启后 ask_user 再入,冷决定短路、不重问 ───────────────────────────


@pytest.mark.asyncio
async def test_ask_user_short_circuits_via_cold_lookup_after_restart() -> None:
    """模拟重启：全新 HitlManager（内存空）,冷查询从"事件日志"折出已答决定 →
    ask_user 直接返回原答案,不新建 pending、不重发 HitlRequired。"""
    log = [
        _required("h_old", "tc_re2", 1),
        _ev(EventType.HITL_ANSWERED, {"hitl_id": "h_old", "message": "use postgres"}, 2),
    ]
    bus = _CollectBus()
    mgr = HitlManager(event_bus=bus)

    async def lookup(session_id: str, tool_call_id: str) -> HitlRequest | None:
        return fold_cold_hitl_decision(log, tool_call_id)

    mgr.set_cold_decision_lookup(lookup)

    provider = ControlCapabilityProvider(hitl_manager=mgr)
    session = Session(id="s1", tenant_id="default", user_prompt="do it", status="RUNNING")
    task = Task(id="tsk_1", session_id="s1", status="ACTIVE", title="T1")
    tm = SimpleNamespace(get_task=lambda tid: task, reopen_chain=None)
    provider.register_session("s1", tm, session)

    ctx = ProviderContext(session_id="s1", tenant_id="default", task_id="tsk_1",
                          agent_id="agt_1", extra={"tool_call_id": "tc_re2"})
    parts: list[str] = []

    async def drain():
        async for ev in provider.invoke(f"{PROVIDER_NAME}:ask_user",
                                        {"questions": [{"question": "Which DB?"}]}, ctx):
            if ev.kind == "result":
                parts.append(ev.payload.get("content", ""))

    await asyncio.wait_for(asyncio.create_task(drain()), timeout=1.0)  # 不应阻塞等人
    assert parts and "use postgres" in parts[0]
    assert mgr.list_pending() == []                                     # 未重新登记
    assert not any(e.type == EventType.HITL_REQUIRED for e in bus.events)  # 未重发重问
