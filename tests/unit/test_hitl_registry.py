"""HitlRegistry：纯同步内存状态机（段 1）。

全同步、无 await —— 单线程 asyncio 下无 await 即原子，故不需要锁。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ctx_weft.core.hitl.registry import HITL_STAGE_AUTHZ, HitlRegistry, PendingHitl
from ctx_weft.protocols.hitl import (
    HitlAsk,
    HitlDecision,
    ToolResultDelivery,
    UserTurnDelivery,
)

T0 = datetime(2026, 9, 1, tzinfo=UTC)


class FakeSlot:
    """WaitSlot 测试替身：记录是否被投递过。"""

    def __init__(self, accepts: bool = True) -> None:
        self.accepts = accepts
        self.delivered: HitlDecision | None = None

    def deliver(self, decision: HitlDecision) -> bool:
        self.delivered = decision
        return self.accepts


def _ask(tool_call_id: str = "call_1") -> HitlAsk:
    return HitlAsk(form="approval", delivery=ToolResultDelivery(tool_call_id=tool_call_id),
                   prompt="Allow bash?", subject_id="fs:bash_exec",
                   proposal={"command": "ls"})


def _open(reg: HitlRegistry, hitl_id: str = "hit_1", tool_call_id: str = "call_1",
          at: datetime = T0, session_id: str = "s1",
          stage: str = HITL_STAGE_AUTHZ) -> PendingHitl:
    return reg.open(_ask(tool_call_id), hitl_id=hitl_id, session_id=session_id, task_id="t1",
                    agent_id="a1", tool_call_id=tool_call_id, stage=stage, created_at=at)


def test_open_registers_a_pending_request():
    reg = HitlRegistry()
    req = _open(reg)
    assert req.id == "hit_1" and req.resolved is False
    assert reg.get("hit_1") is req
    assert [r.id for r in reg.list_pending()] == ["hit_1"]


def test_open_is_idempotent_by_tool_call_id():
    """同 tool_call_id 再次 open 复用既有请求，不新建（spec §10）。"""
    reg = HitlRegistry()
    first = _open(reg, hitl_id="hit_1")
    again = _open(reg, hitl_id="hit_2")          # 不同 id，同 tool_call
    assert again is first
    assert reg.get("hit_2") is None


def test_open_with_empty_tool_call_id_always_creates_a_new_request():
    """空 tool_call_id 不是幂等键（UserTurn 的 park 就没有 tool_call）。"""
    reg = HitlRegistry()
    a = reg.open(HitlAsk(form="wait", delivery=UserTurnDelivery(task_id="t1")),
                 hitl_id="hit_1", session_id="s1", task_id="t1", agent_id="a1",
                 tool_call_id="", stage=HITL_STAGE_AUTHZ, created_at=T0)
    b = reg.open(HitlAsk(form="wait", delivery=UserTurnDelivery(task_id="t1")),
                 hitl_id="hit_2", session_id="s1", task_id="t1", agent_id="a1",
                 tool_call_id="", stage=HITL_STAGE_AUTHZ, created_at=T0)
    assert a is not b


def test_resolve_transitions_and_returns_the_slot():
    reg = HitlRegistry()
    _open(reg)
    slot = FakeSlot()
    reg.attach_slot("hit_1", slot)
    result = reg.resolve("hit_1", HitlDecision(outcome="accepted", message="ok"), T0)
    assert result is not None
    req, taken = result
    assert req.resolved is True and req.decision.message == "ok"
    assert taken is slot
    assert reg.get("hit_1").slot is None          # 槽被取走，不可二次投递


def test_resolve_is_idempotent_and_returns_none_the_second_time():
    """已终局再 resolve = no-op，不二次转移、调用方据此不重发事实（spec §10）。"""
    reg = HitlRegistry()
    _open(reg)
    assert reg.resolve("hit_1", HitlDecision(outcome="accepted"), T0) is not None
    assert reg.resolve("hit_1", HitlDecision(outcome="rejected"), T0) is None
    assert reg.get("hit_1").decision.outcome == "accepted"


def test_resolve_unknown_id_returns_none():
    assert HitlRegistry().resolve("nope", HitlDecision(outcome="accepted"), T0) is None


def test_resolved_request_leaves_the_pending_list():
    reg = HitlRegistry()
    _open(reg)
    reg.resolve("hit_1", HitlDecision(outcome="accepted"), T0)
    assert reg.list_pending() == []


def test_list_pending_filters_by_session():
    reg = HitlRegistry()
    _open(reg, hitl_id="hit_1", tool_call_id="call_1")
    reg.open(_ask("call_2"), hitl_id="hit_2", session_id="s2", task_id="t2",
             agent_id="a1", tool_call_id="call_2", stage=HITL_STAGE_AUTHZ, created_at=T0)
    assert [r.id for r in reg.list_pending(session_id="s2")] == ["hit_2"]


def test_decision_for_returns_decision_and_resume_state_as_a_pair():
    """冷路径重入调 resume(decision, resume_state)——只给决定就得重做让出前的工作。"""
    reg = HitlRegistry()
    ask = HitlAsk(form="question", delivery=ToolResultDelivery(tool_call_id="call_1"),
                  resume_state={"plan": "deploy-7"})
    reg.open(ask, hitl_id="hit_1", session_id="s1", task_id="t1", agent_id="a1",
             tool_call_id="call_1", stage=HITL_STAGE_AUTHZ, created_at=T0)
    reg.resolve("hit_1", HitlDecision(outcome="accepted", message="go"), T0)
    got = reg.decision_for("s1", "call_1", HITL_STAGE_AUTHZ)
    assert got is not None
    decision, resume_state = got
    assert decision.message == "go" and resume_state == {"plan": "deploy-7"}


def test_decision_for_returns_none_while_still_pending():
    """内存 pending = 活的等待，不得被当成「已答过」。"""
    reg = HitlRegistry()
    _open(reg)
    assert reg.decision_for("s1", "call_1", HITL_STAGE_AUTHZ) is None


def test_decision_for_empty_or_unknown_tool_call_id_is_none():
    reg = HitlRegistry()
    _open(reg)
    assert reg.decision_for("s1", "", HITL_STAGE_AUTHZ) is None
    assert reg.decision_for("s1", "other", HITL_STAGE_AUTHZ) is None


def test_gc_trims_oldest_resolved_and_never_touches_pending():
    reg = HitlRegistry(max_resolved=1)
    for i in (1, 2):
        reg.open(_ask(f"call_{i}"), hitl_id=f"hit_{i}", session_id="s1", task_id="t1",
                 agent_id="a1", tool_call_id=f"call_{i}", stage=HITL_STAGE_AUTHZ,
                 created_at=T0)
        reg.resolve(f"hit_{i}", HitlDecision(outcome="accepted"),
                    T0 + timedelta(seconds=i))
    reg.open(_ask("call_3"), hitl_id="hit_3", session_id="s1", task_id="t1",
             agent_id="a1", tool_call_id="call_3", stage=HITL_STAGE_AUTHZ, created_at=T0)
    reg.gc()
    assert reg.get("hit_1") is None               # 最旧的已终局项被裁剪
    assert reg.get("hit_2") is not None
    assert reg.get("hit_3") is not None           # pending 永不裁剪


def test_to_view_projects_the_host_facing_fields():
    reg = HitlRegistry()
    req = _open(reg)
    view = req.to_view()
    assert (view.id, view.form, view.session_id, view.task_id) == (
        "hit_1", "approval", "s1", "t1")
    assert view.subject_id == "fs:bash_exec" and view.proposal == {"command": "ls"}
    assert view.outcome == "" and view.resolved is False


def test_to_view_reflects_the_decision_after_resolve():
    reg = HitlRegistry()
    _open(reg)
    reg.resolve("hit_1", HitlDecision(outcome="rejected"), T0)
    view = reg.get("hit_1").to_view()
    assert view.outcome == "rejected" and view.resolved is True and view.resolved_at == T0
