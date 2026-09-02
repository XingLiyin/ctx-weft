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


def test_new_hitl_event_types_are_registered():
    from ctx_weft.protocols.events import EVENT_TYPES, EventType

    assert EventType.HITL_OPENED == "HitlOpened"
    assert EventType.HITL_RESOLVED == "HitlResolved"
    # EVENT_TYPES 由 frozenset(EventType) 派生，登记即自动生效
    assert "HitlOpened" in EVENT_TYPES and "HitlResolved" in EVENT_TYPES


def test_legacy_hitl_event_types_still_registered():
    """段 1 不删旧事件——双读折叠仍要认它们（spec §12.3）。"""
    from ctx_weft.protocols.events import EventType

    for name in ("HitlRequired", "HitlApproved", "HitlModified",
                 "HitlAnswered", "HitlRejected", "HitlCancelled"):
        assert name in {e.value for e in EventType}


def test_legacy_python_symbols_are_gone():
    """旧词汇必须**彻底**消失，不留只读别名——留着就会有人继续按旧模型写代码，
    而下一次「顺手接回去」就把跨会话授权洞（旧 `find_for_tool_call` 不过滤 session）
    一起接回来。

    `HitlStatus` 是更早一轮重整删掉的（状态两维化：outcome 存储、resolved 推导），
    `HitlRequest` 是本轮删掉的；两条一起钉在这里，因为它们是同一件事的两次复发。
    """
    import importlib

    import ctx_weft.protocols as p
    import ctx_weft.protocols.hitl as h

    for mod in (h, p):
        assert not hasattr(mod, "HitlStatus")
        assert not hasattr(mod, "HitlRequest")

    for gone in ("ctx_weft.core.orchestrator.hitl_manager",):
        try:
            importlib.import_module(gone)
        except ModuleNotFoundError:
            continue
        raise AssertionError(f"{gone} 仍可 import——旧 HitlManager 必须是删除而非停用")


def test_reducers_no_longer_expose_the_legacy_hitl_folds():
    """`fold_pending_hitl` / `fold_cold_hitl_decision` / `HITL_STATUS_EVENT_TYPES` 已删。

    留一份第二口径的 HITL 折叠，就等于给「重建了 pending 却没重建已解决」那类漂移
    留了复发的地方。真相源只剩 `fold_hitl_snapshot`。
    """
    import ctx_weft.core.control.reducers as r

    for name in ("fold_pending_hitl", "unresolved_hitl_ids", "fold_cold_hitl_decision",
                 "HITL_STATUS_EVENT_TYPES"):
        assert not hasattr(r, name), name
    assert hasattr(r, "fold_hitl_snapshot")


def test_authorization_decision_no_longer_has_defer():
    """`defer` 只能说「挂起」、说不出问什么，于是 authorizer 必须自己先去登记请求——
    那正是耦合的源头。取代它的是 `needs_human: HitlAsk`。"""
    import dataclasses

    from ctx_weft.protocols.capability import AuthorizationDecision

    names = {f.name for f in dataclasses.fields(AuthorizationDecision)}
    assert "defer" not in names
    assert "needs_human" in names
