"""新 HITL 契约类型（段 1 · 纯新增）。"""

from __future__ import annotations

from datetime import UTC, datetime

from ctx_weft.protocols.hitl import (
    PREFACE_AFTER_INTERRUPT_EDIT,
    HitlAsk,
    HitlDecision,
    HitlReply,
    HitlRequestView,
    NoResumeDelivery,
    ResumeHint,
    ToolResultDelivery,
    UserTurnDelivery,
)


def test_delivery_variants_are_frozen_and_carry_their_target():
    tr = ToolResultDelivery(tool_call_id="call_1")
    ut = UserTurnDelivery(task_id="tsk_1", preface=PREFACE_AFTER_INTERRUPT_EDIT)
    nr = NoResumeDelivery()
    assert tr.tool_call_id == "call_1"
    assert (ut.task_id, ut.preface) == ("tsk_1", "interrupt_edit")
    assert nr == NoResumeDelivery()          # 无字段，值相等


def test_user_turn_preface_defaults_to_normal():
    assert UserTurnDelivery(task_id="tsk_1").preface == "normal"


def test_ask_defaults_keep_optional_slots_empty():
    ask = HitlAsk(form="approval", delivery=ToolResultDelivery(tool_call_id="call_1"))
    assert ask.prompt == "" and ask.detail == "" and ask.subject_id == ""
    assert ask.fields == [] and ask.proposal is None
    assert ask.resume_state is None
    assert ask.reply_as_result is False


def test_ask_fields_are_not_shared_between_instances():
    a, b = HitlAsk(form="q", delivery=NoResumeDelivery()), HitlAsk(
        form="q", delivery=NoResumeDelivery())
    a.fields.append({"name": "x"})
    assert b.fields == []


def test_decision_defaults_to_empty_message_and_no_modified_arguments():
    d = HitlDecision(outcome="accepted")
    assert d.message == "" and d.modified_arguments is None


def test_reply_carries_resume_hint_separate_from_the_request():
    r = HitlReply(hitl_id="hit_1", outcome="accepted",
                  resume_hint=ResumeHint(llm_account="acc", llm_model="m"))
    assert (r.resume_hint.llm_account, r.resume_hint.llm_model) == ("acc", "m")


def test_reply_resume_hint_defaults_to_empty_hint():
    r = HitlReply(hitl_id="hit_1", outcome="accepted")
    assert r.resume_hint.llm_account is None and r.resume_hint.llm_model is None


def test_view_resolved_is_derived_from_outcome():
    now = datetime.now(UTC)
    pending = HitlRequestView(id="hit_1", form="approval", session_id="s1",
                              task_id="t1", created_at=now)
    done = HitlRequestView(id="hit_2", form="approval", session_id="s1",
                           task_id="t1", created_at=now, outcome="accepted")
    assert pending.resolved is False
    assert done.resolved is True


def test_view_has_no_tool_call_id_field():
    """tool_call_id 是 core 的幂等键，不进对外视图（spec §4）。"""
    assert not hasattr(
        HitlRequestView(id="hit_1", form="approval", session_id="s1", task_id="t1",
                        created_at=datetime.now(UTC)),
        "tool_call_id",
    )
