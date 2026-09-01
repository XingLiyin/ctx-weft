"""装填：恢复期把折叠结果喂进 registry，之后 core 的一切查询只读内存（spec §3.1）。"""

from __future__ import annotations

from datetime import UTC, datetime

from ctx_weft.core.hitl.registry import HitlRegistry, PendingHitl
from ctx_weft.core.hitl.snapshot import HitlSnapshot
from ctx_weft.protocols.hitl import HitlDecision, ToolResultDelivery

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _pending(hitl_id: str, tool_call_id: str) -> PendingHitl:
    return PendingHitl(
        id=hitl_id, form="approval", session_id="s1", task_id="t1", agent_id="a1",
        delivery=ToolResultDelivery(tool_call_id=tool_call_id), created_at=T0,
        tool_call_id=tool_call_id)


def test_load_snapshot_restores_pending_requests():
    reg = HitlRegistry()
    n = reg.load_snapshot(HitlSnapshot(pending={"hit_1": _pending("hit_1", "call_1")}))
    assert n == 1
    assert reg.get("hit_1") is not None
    assert [r.id for r in reg.list_pending()] == ["hit_1"]


def test_loaded_pending_has_no_wait_slot():
    """重启后一切皆冷：装填出来的请求没有等待槽（spec §10）。"""
    reg = HitlRegistry()
    reg.load_snapshot(HitlSnapshot(pending={"hit_1": _pending("hit_1", "call_1")}))
    assert reg.get("hit_1").slot is None


def test_load_snapshot_restores_decisions_with_their_resume_state():
    reg = HitlRegistry()
    reg.load_snapshot(HitlSnapshot(decisions_for={
        "call_9": (HitlDecision(outcome="accepted", message="go"), {"plan": "deploy-7"})}))
    got = reg.decision_for("call_9")
    assert got is not None
    decision, resume_state = got
    assert decision.message == "go" and resume_state == {"plan": "deploy-7"}


def test_loaded_decision_is_queryable_without_touching_storage():
    """装填之后不再有第二级回落——一次内存查询即可（spec §11）。"""
    reg = HitlRegistry()
    reg.load_snapshot(HitlSnapshot(decisions_for={
        "call_9": (HitlDecision(outcome="rejected"), None)}))
    assert reg.decision_for("call_9")[0].outcome == "rejected"
    assert reg.decision_for("call_unknown") is None


def test_live_pending_wins_over_a_loaded_decision_for_the_same_tool_call():
    """内存 pending = 活的等待，不得被日志里的旧决定盖掉。"""
    reg = HitlRegistry()
    reg.load_snapshot(HitlSnapshot(pending={"hit_1": _pending("hit_1", "call_1")}))
    reg.load_snapshot(HitlSnapshot(decisions_for={
        "call_1": (HitlDecision(outcome="accepted"), None)}))
    assert reg.decision_for("call_1") is None
    assert reg.get("hit_1").resolved is False


def test_load_snapshot_is_idempotent():
    reg = HitlRegistry()
    snap = HitlSnapshot(pending={"hit_1": _pending("hit_1", "call_1")})
    reg.load_snapshot(snap)
    reg.load_snapshot(snap)
    assert len(reg.list_pending()) == 1
