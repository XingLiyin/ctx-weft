"""Four verdicts: continue always runs; waiting yields to the user."""

from __future__ import annotations

import logging

import pytest

from ctx_weft.core.capabilities.control_tools import ControlContext
from tests.unit.observation_helpers import submit_and_apply
from ctx_weft.core.models.task import Task
from ctx_weft.core.orchestrator.task.disposition import (
    RunOutcome,
    RunOutcomeKind,
    disposition_for,
)
from ctx_weft.core.utils.verdict import (
    VERDICT_CONTINUE,
    VERDICT_WAITING,
    VERDICT_FAIL,
    VERDICT_SUCCESS,
    VERDICTS,
    normalize_verdict,
)


def _completed(verdict) -> RunOutcome:
    return RunOutcome(kind=RunOutcomeKind.COMPLETED, verdict=verdict, error="受阻原因")


def _task(**kw) -> Task:
    return Task(id="t1", session_id="s1", status="ACTIVE", **kw)


def _ctx(task: Task) -> ControlContext:
    return ControlContext(session_id="s1", task_id="t1", agent_id="a1", task=task,
                          task_manager=None, session=None)


# ── ① 归一 ────────────────────────────────────────────────────────────────────


def test_the_vocabulary_is_exactly_four_words() -> None:
    assert VERDICTS == (VERDICT_SUCCESS, VERDICT_WAITING, VERDICT_CONTINUE, VERDICT_FAIL)
    assert VERDICTS == ("success", "waiting", "continue", "fail")


@pytest.mark.parametrize("value", ["success", "waiting", "continue", "fail"])
def test_canonical_values_pass_through(value: str) -> None:
    assert normalize_verdict(value) == value


@pytest.mark.parametrize("raw", ["retry", "RETRY", "  Retry  "])
def test_retry_is_a_permanent_alias_for_continue(raw: str, caplog) -> None:
    """`retry` 不能停止被接受：prompt 缓存、重放的老 transcript、模型自身的先验都会吐它。

    **别名与"未知值兜底"必须可区分**：两者都落到 `continue`，所以单看返回值这条测试是重言式
    （实测过：把 `_ALIASES` 整段删掉它照样绿）。唯一可观测的差别是**别名不吵**——`retry` 是
    预期输入，不该在日志里刷 WARNING；认不出的值才该。
    """
    with caplog.at_level(logging.WARNING, logger="ctx_weft.core.utils.verdict"):
        assert normalize_verdict(raw) == VERDICT_CONTINUE
    assert caplog.records == [], f"`retry` 是别名、不是意外输入，不该告警：{caplog.text}"


def test_an_unrecognized_value_does_warn(caplog) -> None:
    """对照：认不出的值必须留一条 WARNING，否则模型吐错值会永远无声无息。"""
    with caplog.at_level(logging.WARNING, logger="ctx_weft.core.utils.verdict"):
        assert normalize_verdict("mostly done?") == VERDICT_CONTINUE
    assert any("mostly done?" in r.getMessage() for r in caplog.records), caplog.text


@pytest.mark.parametrize("raw", ["active", "ask_human", "done", "", "  ", None, 0, object()])
def test_anything_unrecognized_becomes_continue_never_fail(raw: object) -> None:
    """方向：拿不准时继续；只有明确的 fail 才判失败。"""
    assert normalize_verdict(raw) == VERDICT_CONTINUE
    assert normalize_verdict(raw) != VERDICT_FAIL


# ── ② 处置表四支 ──────────────────────────────────────────────────────────────


def test_success_finishes() -> None:
    d = disposition_for(_completed(VERDICT_SUCCESS), retry_count=0, max_retries=3)
    assert (d.status, d.payload["outcome"]) == ("FINISHED", VERDICT_SUCCESS)


def test_continue_preserves_budget_and_says_continue_in_the_payload() -> None:
    d = disposition_for(_completed(VERDICT_CONTINUE), retry_count=1, max_retries=3)
    assert d.status == "PENDING"
    assert d.event_type == "TaskRequeued"
    assert d.payload["outcome"] == VERDICT_CONTINUE, "payload 的词面也要跟着改，不留旧词"
    assert d.payload["retry_count"] == 1


def test_continue_without_exception_retry_budget_still_requeues() -> None:
    d = disposition_for(_completed(VERDICT_CONTINUE), retry_count=3, max_retries=3)
    assert d.status == "PENDING"
    assert d.payload["retry_count"] == 3


def test_fail_is_its_own_branch() -> None:
    d = disposition_for(_completed(VERDICT_FAIL), retry_count=0, max_retries=3)
    assert d.status == "FAILED"
    assert d.payload["error_code"] == "TASK_FAILED_BY_OBSERVER"


@pytest.mark.parametrize("raw", ["retry", "typo", "", None])
def test_an_unknown_verdict_never_kills_the_task(raw: object) -> None:
    """**改名前这里是雷**：末尾那个 catch-all 同时服务 `fail` 和任何认不出的值，于是一个错别字
    就让 task 无声判死（FAILED / BY_OBSERVER）。现在它按 `continue` 走重排。
    """
    d = disposition_for(_completed(raw), retry_count=0, max_retries=3)
    assert d.status == "PENDING", f"{raw!r} 被判死了——那颗雷回来了"
    assert d.payload["outcome"] == VERDICT_CONTINUE


# ── ③ 工具边界 ────────────────────────────────────────────────────────────────


def test_the_tool_normalizes_retry_to_continue() -> None:
    t = _task(outputs="partial")
    res = submit_and_apply(task_status="retry", act_recap="还差一步", ctx=_ctx(t))
    assert t.observer_outcome == VERDICT_CONTINUE
    assert res.metadata["observer_outcome"] == VERDICT_CONTINUE


def test_the_tool_normalizes_garbage_to_continue() -> None:
    t = _task(outputs="partial")
    submit_and_apply(task_status="mostly done?", act_recap="r", ctx=_ctx(t))
    assert t.observer_outcome == VERDICT_CONTINUE


@pytest.mark.parametrize("verdict", [VERDICT_CONTINUE, VERDICT_WAITING])
def test_nonterminal_verdict_needs_no_failure_reason(verdict):
    t = _task(outputs="unfinished")
    submit_and_apply(task_status=verdict, act_recap="unfinished", ctx=_ctx(t))
    assert t.observer_outcome == verdict
    assert t.error is None


def test_waiting_parks_instead_of_requeueing():
    d = disposition_for(RunOutcome(kind=RunOutcomeKind.COMPLETED,
        verdict=VERDICT_WAITING, hitl_id="hit_wait"), retry_count=99, max_retries=0)
    assert d.status == "AWAITING_HUMAN"
    assert d.event_type == "TaskAwaitingHuman"
    assert d.payload == {"hitl_id": "hit_wait", "outcome": "waiting"}


@pytest.mark.parametrize("waiting_allowed", [False, True])
def test_waiting_requires_a_user_entrance(waiting_allowed):
    from ctx_weft.core.capabilities.control_tools import report_observation
    from ctx_weft.core.loop.steps.observe import ObserveStep

    submission = report_observation("asked user", task_status="waiting")
    error = ObserveStep()._submission_error(submission, waiting_allowed=waiting_allowed)
    assert (error is None) == waiting_allowed


# ── ④ 那道机械护栏管不到「在问人」这件事 ──────────────────────────────────────


def test_the_no_outputs_guard_cannot_catch_a_question() -> None:
    """钉住一个容易被误信的事实：**park 的纯文本回合没有机械保险**。

    success-without-outputs 护栏读的是 `task.outputs`，而纯文本 park 之前
    `_synthesize_final_outputs` 已经把那段话写成了 outputs——哪怕它整段都是在问用户，
    outputs 也非空，护栏不触发，判 success 就会原样通过。

    所以「向用户要东西 → 一律 waiting」这条只能由 prompt（ROLE 的判断准则 + 工具字段描述 +
    cue 的边界提醒）保证。谁将来想省掉那几段文案，先看这条测试。
    """
    asking = _task(outputs="这两个方案你想选哪个？")
    submit_and_apply(task_status=VERDICT_SUCCESS, act_recap="问了用户", ctx=_ctx(asking))
    assert asking.observer_outcome == VERDICT_SUCCESS, (
        "护栏若在此触发，说明它的判据变了——这条测试的前提（只靠 prompt）要重估")

    # 对照：真的没有产出时，护栏照旧把 success 降成 continue。
    empty = _task(outputs=None)
    submit_and_apply(task_status=VERDICT_SUCCESS, act_recap="什么也没产出", ctx=_ctx(empty))
    assert empty.observer_outcome == VERDICT_CONTINUE


def test_the_guard_downgrades_to_continue_not_to_retry() -> None:
    t = _task(outputs=None)
    res = submit_and_apply(task_status=VERDICT_SUCCESS, act_recap="r", ctx=_ctx(t))
    assert res.metadata["observer_outcome"] == VERDICT_CONTINUE
    assert "retry" not in res.content, f"确认话术里不该再出现旧词：{res.content!r}"
