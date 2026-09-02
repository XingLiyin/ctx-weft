"""HitlForm 是开放扩展点：host 可定义自己的 form，core 原样透传、不 assert。"""

from __future__ import annotations

from datetime import UTC, datetime

from ctx_weft.core.hitl.registry import HITL_STAGE_TOOL, HitlRegistry
from ctx_weft.protocols.hitl import (
    HITL_FORM_APPROVAL,
    HITL_FORM_QUESTION,
    HITL_FORM_WAIT,
    HitlAsk,
    NoResumeDelivery,
)


def test_wellknown_form_constants() -> None:
    assert (HITL_FORM_APPROVAL, HITL_FORM_QUESTION, HITL_FORM_WAIT) == (
        "approval", "question", "wait",
    )


def test_registry_round_trips_custom_form() -> None:
    """core 不得对未知 form 做白名单校验：open → get → to_view 全程原样透传。"""
    reg = HitlRegistry()
    req = reg.open(
        HitlAsk(form="diff_review", delivery=NoResumeDelivery(), prompt="Review this diff?"),
        hitl_id="h1", session_id="s1", task_id="t1", stage=HITL_STAGE_TOOL,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert req.form == "diff_review"
    assert req.resolved is False
    assert reg.get("h1").form == "diff_review"
    assert reg.get("h1").to_view().form == "diff_review"
