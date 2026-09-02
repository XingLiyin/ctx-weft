"""run 结局 + 判决 + 重试预算 → task 处置。纯函数，无 IO（Task 1）。

这里看不见事件总线、看不见 TaskManager 的队列——只有一张表。
"""

from __future__ import annotations

import pytest

from ctx_weft.core.orchestrator.task_disposition import (
    RunOutcome,
    RunOutcomeKind,
    disposition_for,
)


def _completed(verdict: str, **kw) -> RunOutcome:
    return RunOutcome(kind=RunOutcomeKind.COMPLETED, verdict=verdict, **kw)


def test_success_finishes():
    d = disposition_for(_completed("success", summary="done", outputs={"a": 1}),
                        retry_count=0, max_retries=3)
    assert d.status == "FINISHED"
    assert d.event_type == "TaskFinished"
    assert d.payload["outcome"] == "success"
    assert d.payload["outputs"] == {"a": 1}


def test_observer_fail_fails_with_its_own_code():
    d = disposition_for(_completed("fail", error="观察者判死"), retry_count=0, max_retries=3)
    assert d.status == "FAILED"
    assert d.event_type == "TaskFailed"
    assert d.payload["error_code"] == "TASK_FAILED_BY_OBSERVER"
    assert d.payload["error_message"] == "观察者判死"


def test_retry_with_budget_requeues():
    d = disposition_for(_completed("retry", summary="再来"), retry_count=1, max_retries=3)
    assert d.status == "PENDING"
    assert d.event_type == "TaskRequeued"
    assert d.payload["retry_count"] == 2      # 已 +1，与今天 finalize 的行为一致


def test_retry_exhausted_degrades_to_failed():
    """这条判断今天在 FinalizeStep 里——它是重试预算，属于处置不属于判决。"""
    d = disposition_for(_completed("retry", error="本轮受阻"), retry_count=3, max_retries=3)
    assert d.status == "FAILED"
    assert d.event_type == "TaskFailed"
    assert d.payload["error_code"] == "TASK_FAILED_RETRY_EXHAUSTED"
    assert d.payload["error_message"] == "本轮受阻"


def test_awaiting_human_carries_the_hitl_id():
    d = disposition_for(RunOutcome(kind=RunOutcomeKind.AWAITING_HUMAN, hitl_id="hit_1"),
                        retry_count=0, max_retries=3)
    assert d.status == "AWAITING_HUMAN"
    assert d.event_type == "TaskAwaitingHuman"
    assert d.payload == {"hitl_id": "hit_1"}


def test_suspended_on_children_carries_the_titles():
    d = disposition_for(
        RunOutcome(kind=RunOutcomeKind.SUSPENDED_ON_CHILDREN,
                   summary="等两个子任务", spawn_titles=("查资料", "写稿")),
        retry_count=0, max_retries=3)
    assert d.status == "SUSPENDED"
    assert d.event_type == "TaskSuspended"
    assert d.payload["spawn_titles"] == ["查资料", "写稿"]


def test_outage_never_retries_in_place():
    """LLM outage 等 /resume，从不原地重试——即使预算充足。"""
    d = disposition_for(
        RunOutcome(kind=RunOutcomeKind.INTERRUPTED, reason="llm_outage",
                   error_code="llm_outage", retriable=False),
        retry_count=0, max_retries=3)
    assert d.status == "INTERRUPTED"
    assert d.event_type == "TaskInterrupted"


def test_retriable_crash_with_budget_requeues():
    d = disposition_for(
        RunOutcome(kind=RunOutcomeKind.INTERRUPTED, reason="run_crash",
                   error_code="ValueError", retriable=True),
        retry_count=0, max_retries=3)
    assert d.status == "PENDING"
    assert d.event_type == "TaskRequeued"


def test_retriable_crash_without_budget_interrupts():
    d = disposition_for(
        RunOutcome(kind=RunOutcomeKind.INTERRUPTED, reason="run_crash",
                   error_code="ValueError", retriable=True),
        retry_count=3, max_retries=3)
    assert d.status == "INTERRUPTED"
    assert d.event_type == "TaskInterrupted"
    assert d.payload["reason"] == "run_crash"


def test_canceled():
    d = disposition_for(RunOutcome(kind=RunOutcomeKind.CANCELED, reason="user_cancel"),
                        retry_count=0, max_retries=3)
    assert d.status == "CANCELED"
    assert d.event_type == "TaskCanceled"
    assert d.payload == {"reason": "user_cancel"}


def test_canceled_with_empty_reason_yields_no_reason_key():
    """R5：今天 runtime.py 发 TASK_CANCELED 的 payload 是字面 `{}`——reason 为空时
    不许平白多出一个 `{"reason": ""}` 破行为等价。"""
    d = disposition_for(RunOutcome(kind=RunOutcomeKind.CANCELED), retry_count=0, max_retries=3)
    assert d.status == "CANCELED"
    assert d.event_type == "TaskCanceled"
    assert d.payload == {}


def test_canceled_with_nonempty_reason_includes_it():
    """非空 reason 照放——给未来真有取消原因的调用方留口。"""
    d = disposition_for(RunOutcome(kind=RunOutcomeKind.CANCELED, reason="user_cancel"),
                        retry_count=0, max_retries=3)
    assert d.payload == {"reason": "user_cancel"}


@pytest.mark.parametrize("kind", list(RunOutcomeKind))
def test_every_kind_yields_a_disposition(kind):
    """值域穷举：加一种结局就必须在表里给它一行，否则这条会红。"""
    d = disposition_for(RunOutcome(kind=kind, verdict="success"),
                        retry_count=0, max_retries=3)
    assert d.status and d.event_type
