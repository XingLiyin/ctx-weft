"""双读折叠：新旧两套 HITL 事件 → HitlSnapshot（spec §12.3）。

对旧数据的判据必须与升级前**逐条同构**——迁移的正确性标准是「行为不变」，
不是「更符合新设计的意图」。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ctx_weft.core.control.reducers import fold_hitl_snapshot
from ctx_weft.protocols import ImagePart, TextPart
from ctx_weft.protocols.events import Event, EventType
from ctx_weft.protocols.hitl import (
    NoResumeDelivery,
    ToolResultDelivery,
    UserTurnDelivery,
)

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _ev(event_type: str, payload: dict, *, seq: int = 0, task_id: str = "t1",
        agent_id: str = "a1") -> Event:
    return Event(id=f"evt_{seq}", run_id=None, sequence=seq, session_id="s1",
                 type=event_type, timestamp=T0 + timedelta(seconds=seq),
                 task_id=task_id, agent_id=agent_id, payload=payload)


# ── 旧事件（legacy）─────────────────────────────────────────────────────────────

def _legacy_required(hitl_id="hit_1", form="approval", capability_id="fs:bash_exec",
                     tool_call_id="call_1", context="", seq=0) -> Event:
    return _ev(EventType.HITL_REQUIRED, {
        "hitl_id": hitl_id, "form": form, "capability_id": capability_id,
        "tool_call_id": tool_call_id, "agent_id": "a1",
        "question": "Allow bash?", "context": context,
        "arguments": {"command": "ls"}, "questions": [],
    }, seq=seq)


def test_legacy_required_becomes_pending_with_tool_result_delivery():
    snap = fold_hitl_snapshot([_legacy_required()])
    req = snap.pending["hit_1"]
    assert req.form == "approval" and req.subject_id == "fs:bash_exec"
    assert req.prompt == "Allow bash?" and req.proposal == {"command": "ls"}
    assert req.delivery == ToolResultDelivery(tool_call_id="call_1")
    assert req.agent_id == "a1"


def test_legacy_wait_form_becomes_user_turn_delivery():
    """反推用 form == 'wait'（今天 runtime 的实际判据），不是 sentinel capability_id。"""
    snap = fold_hitl_snapshot([
        _legacy_required(form="wait", capability_id="control:wait_for_user",
                         tool_call_id="", context="plain_text")])
    assert snap.pending["hit_1"].delivery == UserTurnDelivery(task_id="t1",
                                                              preface="normal")


def test_legacy_wait_form_without_the_sentinel_still_becomes_user_turn():
    """判据是 form，不是 capability_id——否则迁移会改变这条在途请求的行为。"""
    snap = fold_hitl_snapshot([
        _legacy_required(form="wait", capability_id="", tool_call_id="",
                         context="interrupt")])
    assert snap.pending["hit_1"].delivery == UserTurnDelivery(task_id="t1",
                                                              preface="interrupt")


def test_legacy_wait_context_interrupt_edit_maps_to_its_preface():
    snap = fold_hitl_snapshot([
        _legacy_required(form="wait", tool_call_id="", context="interrupt:edit")])
    assert snap.pending["hit_1"].delivery == UserTurnDelivery(
        task_id="t1", preface="interrupt_edit")


def test_legacy_non_wait_without_tool_call_id_falls_back_to_no_resume():
    """既非 wait、又无 tool_call 可补 → 显式「只可取消」，不静默丢。"""
    snap = fold_hitl_snapshot([_legacy_required(form="question", tool_call_id="")])
    assert snap.pending["hit_1"].delivery == NoResumeDelivery()


def test_legacy_approved_resolves_to_accepted():
    snap = fold_hitl_snapshot([
        _legacy_required(),
        _ev(EventType.HITL_APPROVED, {"hitl_id": "hit_1"}, seq=1)])
    assert snap.pending == {}
    decision, resume_state = snap.decisions_for["call_1"]
    assert decision.outcome == "accepted" and decision.modified_arguments is None
    assert resume_state is None


def test_legacy_modified_resolves_to_accepted_with_arguments():
    snap = fold_hitl_snapshot([
        _legacy_required(),
        _ev(EventType.HITL_MODIFIED,
            {"hitl_id": "hit_1", "modified_arguments": {"command": "ls -l"}}, seq=1)])
    decision, _ = snap.decisions_for["call_1"]
    assert decision.outcome == "accepted"
    assert decision.modified_arguments == {"command": "ls -l"}


def test_legacy_answered_carries_its_message():
    snap = fold_hitl_snapshot([
        _legacy_required(form="question"),
        _ev(EventType.HITL_ANSWERED, {"hitl_id": "hit_1", "message": "yes"}, seq=1)])
    decision, _ = snap.decisions_for["call_1"]
    assert decision.outcome == "accepted" and decision.message == "yes"


def test_legacy_rejected_and_cancelled_map_to_their_outcomes():
    rejected = fold_hitl_snapshot([
        _legacy_required(),
        _ev(EventType.HITL_REJECTED, {"hitl_id": "hit_1", "message": "no"}, seq=1)])
    assert rejected.decisions_for["call_1"][0].outcome == "rejected"

    cancelled = fold_hitl_snapshot([
        _legacy_required(),
        _ev(EventType.HITL_CANCELLED, {"hitl_id": "hit_1"}, seq=1)])
    assert cancelled.pending == {}
    assert "call_1" not in cancelled.decisions_for   # cancelled 不是可用决定


def test_answered_without_message_is_not_a_usable_decision():
    """旧事件只有 hitl_id → 还原不出答案。按未决重问，**绝不臆造**（spec §12.3.3）。"""
    snap = fold_hitl_snapshot([
        _legacy_required(form="question"),
        _ev(EventType.HITL_ANSWERED, {"hitl_id": "hit_1"}, seq=1)])
    assert "call_1" not in snap.decisions_for
    assert snap.pending == {}          # 已终局，故不在 pending；但也不可用作决定


def test_modified_without_arguments_is_not_a_usable_decision():
    """缺改参会拿原参执行，违背改参意图 → 不可用。"""
    snap = fold_hitl_snapshot([
        _legacy_required(),
        _ev(EventType.HITL_MODIFIED, {"hitl_id": "hit_1"}, seq=1)])
    assert "call_1" not in snap.decisions_for


def test_session_paused_hitl_is_ignored():
    """会话暂停态在新模型里由 pending 集合推导，旧事件不再是真相（spec §7.1）。"""
    snap = fold_hitl_snapshot([
        _legacy_required(),
        _ev(EventType.SESSION_PAUSED_HITL, {"capability_id": "fs:bash_exec"}, seq=1)])
    assert set(snap.pending) == {"hit_1"}


# ── 新事件 ──────────────────────────────────────────────────────────────────────

def _opened(hitl_id="hit_9", delivery=None, resume_state=None, seq=0) -> Event:
    return _ev(EventType.HITL_OPENED, {
        "hitl_id": hitl_id, "form": "question",
        "delivery": delivery or {"kind": "tool_result", "tool_call_id": "call_9"},
        "subject_id": "deploy:apply", "prompt": "确认部署？", "detail": "",
        "fields": [], "proposal": None, "tool_call_id": "call_9", "agent_id": "a1",
        "resume_state": resume_state, "reply_as_result": False,
    }, seq=seq)


def test_new_opened_folds_into_pending():
    snap = fold_hitl_snapshot([_opened()])
    req = snap.pending["hit_9"]
    assert req.delivery == ToolResultDelivery(tool_call_id="call_9")
    assert req.subject_id == "deploy:apply" and req.prompt == "确认部署？"


def test_new_user_turn_delivery_round_trips():
    snap = fold_hitl_snapshot([_opened(
        delivery={"kind": "user_turn", "task_id": "t1", "preface": "interrupt_edit"})])
    assert snap.pending["hit_9"].delivery == UserTurnDelivery(
        task_id="t1", preface="interrupt_edit")


def test_new_resolved_pairs_the_decision_with_its_resume_state():
    """决定必须与 resume_state 成对——只给决定就要重做让出前的工作（spec §7.2）。"""
    snap = fold_hitl_snapshot([
        _opened(resume_state={"plan": "deploy-7"}),
        _ev(EventType.HITL_RESOLVED,
            {"hitl_id": "hit_9", "outcome": "accepted", "message": "go",
             "claimed": False}, seq=1)])
    assert snap.pending == {}
    decision, resume_state = snap.decisions_for["call_9"]
    assert decision.outcome == "accepted" and decision.message == "go"
    assert resume_state == {"plan": "deploy-7"}


def test_new_resolved_with_host_custom_outcome_is_passed_through():
    """outcome 是开放值域，core 只判「非空即终局」，不解释语义（spec §9.4）。"""
    snap = fold_hitl_snapshot([
        _opened(),
        _ev(EventType.HITL_RESOLVED,
            {"hitl_id": "hit_9", "outcome": "escalated", "claimed": False}, seq=1)])
    assert snap.decisions_for["call_9"][0].outcome == "escalated"


def test_last_usable_decision_wins_for_the_same_tool_call():
    """同 tool_call 重问副本：最后一条可用决定胜出。"""
    snap = fold_hitl_snapshot([
        _legacy_required(hitl_id="hit_1", seq=0),
        _ev(EventType.HITL_REJECTED, {"hitl_id": "hit_1", "message": "no"}, seq=1),
        _legacy_required(hitl_id="hit_2", seq=2),
        _ev(EventType.HITL_APPROVED, {"hitl_id": "hit_2"}, seq=3)])
    assert snap.decisions_for["call_1"][0].outcome == "accepted"


def test_empty_event_list_yields_an_empty_snapshot():
    snap = fold_hitl_snapshot([])
    assert snap.pending == {} and snap.decisions_for == {}


# ── 回归修复（review Finding 1/2）────────────────────────────────────────────────

def test_legacy_answered_multimodal_message_round_trips_to_content_parts():
    """事件载荷存 jsonable 形态；decision.message 须经 content_from_jsonable 转回
    ContentPart，否则图片答复在 split_for_tool_result 里因无 .text 被拆成空文本。"""
    snap = fold_hitl_snapshot([
        _legacy_required(form="question"),
        _ev(EventType.HITL_ANSWERED, {
            "hitl_id": "hit_1",
            "message": [
                {"type": "text", "text": "here"},
                {"type": "image", "data": "abc", "media_type": "image/png"},
            ],
        }, seq=1)])
    decision, _ = snap.decisions_for["call_1"]
    assert decision.message == [
        TextPart(text="here"),
        ImagePart(data="abc", media_type="image/png", source_type="base64"),
    ]


def test_new_resolved_multimodal_message_round_trips_to_content_parts():
    snap = fold_hitl_snapshot([
        _opened(),
        _ev(EventType.HITL_RESOLVED, {
            "hitl_id": "hit_9", "outcome": "accepted", "claimed": False,
            "message": [{"type": "text", "text": "go"}],
        }, seq=1)])
    decision, _ = snap.decisions_for["call_9"]
    assert decision.message == [TextPart(text="go")]


def test_legacy_answered_with_empty_message_is_not_a_usable_decision():
    """判据是真值而非 is None——空串同样还原不出答案，须与 fold_cold_hitl_decision 同构。"""
    snap = fold_hitl_snapshot([
        _legacy_required(form="question"),
        _ev(EventType.HITL_ANSWERED, {"hitl_id": "hit_1", "message": ""}, seq=1)])
    assert "call_1" not in snap.decisions_for
