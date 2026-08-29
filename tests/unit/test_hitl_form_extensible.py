"""HitlForm 是开放扩展点：host 可定义自己的 form，core 原样透传、不 assert。"""

from __future__ import annotations

from ctx_weft.core.orchestrator.hitl_manager import HitlManager
from ctx_weft.protocols.hitl import (
    HITL_FORM_APPROVAL,
    HITL_FORM_QUESTION,
    HITL_FORM_WAIT,
    HitlRequest,
)


def test_wellknown_form_constants() -> None:
    assert (HITL_FORM_APPROVAL, HITL_FORM_QUESTION, HITL_FORM_WAIT) == (
        "approval", "question", "wait",
    )


def test_hitl_request_accepts_custom_form() -> None:
    req = HitlRequest(id="h1", form="diff_review", session_id="s1", task_id="t1")
    assert req.form == "diff_review"
    assert req.status == "pending"
    assert req.accepted is False


async def test_manager_round_trips_custom_form() -> None:
    """core 不得对未知 form 做白名单校验：request → get 原样返回。"""
    mgr = HitlManager()
    hitl_id = await mgr.request(
        form="diff_review",
        session_id="s1",
        task_id="t1",
        question="Review this diff?",
    )
    got = mgr.get(hitl_id)
    assert got is not None
    assert got.form == "diff_review"
