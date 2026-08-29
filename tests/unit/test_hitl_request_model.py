"""HitlRequest 统一实体（spec 2026-07-05）：form 三形态 + 默认值。"""
from ctx_weft.protocols.hitl import HitlForm, HitlRequest, HitlStatus  # noqa: F401


def test_hitl_request_defaults():
    req = HitlRequest(id="hit_1", form="question", session_id="s1", task_id="t1")
    assert req.status == "pending"
    assert req.accepted is False
    assert req.arguments == {} and req.questions == []
    assert req.resolved_at is None
    assert req.resume_llm_account is None and req.resume_llm_model is None
    assert req.created_at is not None


def test_hitl_request_accepted_property():
    req = HitlRequest(id="hit_2", form="approval", session_id="s1", task_id="t1")
    req.status = "accepted"
    assert req.accepted is True


def test_hitl_request_three_forms():
    for form in ("approval", "question", "wait"):
        assert HitlRequest(id="x", form=form, session_id="s", task_id="t").form == form
