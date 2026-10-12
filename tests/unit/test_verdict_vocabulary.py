"""复核裁决的值域：`success | continue | fail`，别名与未知值的去向（2026-09-28 改名）。

## 为什么第二态从 `retry` 改成 `continue`

那一态要表达的是「**这个 task 还没结束**」，有三个来源：机械退出（max_turns / context_limit）、
复核判本段不合格、以及**actor 在问用户话、正等着回答**。`retry` 只贴合中间那一种，对第三种是
误导——而第三种是交互产品里每天都在发生的形态。

误导的后果不是文字不美观，是**把观察者推去判 success**：词面上「重试」暗示这次失败了，观察者
看着一段礼貌的澄清提问被要求打上"重试"，会觉得标签不对而转投 `success`；字段上
`task_failure_reason` 对 `retry` 曾是必填，一个正常的提问没有 blocker，要么编一个、要么换个
status。判 success 的代价是实打实的：task 就此终结，用户正要说的那一轮被丢掉。

纯文本 park 这条路上**没有机械护栏**能挡住它：`report_task_outcome` 那道
success-without-outputs 护栏读 `task.outputs`，而 park 前合成的 outputs 正是那段提问本身、非空
——护栏原地失效（本文件最后一条钉住这个事实，免得有人以为还有一层保险）。

## 本文件钉三件事

1. 归一：`retry` 是永久别名；认不出的值归 `continue`，**决不归 `fail`**。
2. 处置表四支显式，尤其「未知值不判死」——改名前那里是个 catch-all，`fail` 与任何拼错的值
   共用它，一个错别字就让 task 无声判死。
3. 工具边界：模型吐 `retry`（prompt 缓存 / 老 transcript / 模型先验都会）照样归到 `continue`。
"""

from __future__ import annotations

import logging

import pytest

from ctx_weft.core.capabilities.control_tools import ControlContext, report_task_outcome
from ctx_weft.core.models.task import Task
from ctx_weft.core.orchestrator.task.disposition import (
    RunOutcome,
    RunOutcomeKind,
    disposition_for,
)
from ctx_weft.core.utils.verdict import (
    VERDICT_CONTINUE,
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


def test_the_vocabulary_is_exactly_three_words() -> None:
    assert VERDICTS == (VERDICT_SUCCESS, VERDICT_CONTINUE, VERDICT_FAIL)
    assert VERDICTS == ("success", "continue", "fail")


@pytest.mark.parametrize("value", ["success", "continue", "fail"])
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
    """方向：拿不准时继续，不判死。判死是不可逆的，而『继续』有 max_retries 自己收口。"""
    assert normalize_verdict(raw) == VERDICT_CONTINUE
    assert normalize_verdict(raw) != VERDICT_FAIL


# ── ② 处置表四支 ──────────────────────────────────────────────────────────────


def test_success_finishes() -> None:
    d = disposition_for(_completed(VERDICT_SUCCESS), retry_count=0, max_retries=3)
    assert (d.status, d.payload["outcome"]) == ("FINISHED", VERDICT_SUCCESS)


def test_continue_with_budget_requeues_and_says_continue_in_the_payload() -> None:
    d = disposition_for(_completed(VERDICT_CONTINUE), retry_count=1, max_retries=3)
    assert d.status == "PENDING"
    assert d.event_type == "TaskRequeued"
    assert d.payload["outcome"] == VERDICT_CONTINUE, "payload 的词面也要跟着改，不留旧词"
    assert d.payload["retry_count"] == 2


def test_continue_without_budget_is_the_exhaustion_path() -> None:
    d = disposition_for(_completed(VERDICT_CONTINUE), retry_count=3, max_retries=3)
    assert d.status == "FAILED"
    assert d.payload["error_code"] == "TASK_FAILED_RETRY_EXHAUSTED"


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
    res = report_task_outcome(task_status="retry", act_recap="还差一步", ctx=_ctx(t))
    assert t.observer_outcome == VERDICT_CONTINUE
    assert res.metadata["observer_outcome"] == VERDICT_CONTINUE


def test_the_tool_normalizes_garbage_to_continue() -> None:
    t = _task(outputs="partial")
    report_task_outcome(task_status="mostly done?", act_recap="r", ctx=_ctx(t))
    assert t.observer_outcome == VERDICT_CONTINUE


def test_a_continue_verdict_needs_no_failure_reason() -> None:
    """契约的另一半：`continue` 不再必填受阻原因。

    必填是把观察者推去判 success 的第二只手——一个正常的澄清提问没有任何 blocker，要填这个
    字段就只能编。空着不得引起降级、不得写脏 task.error。
    """
    t = _task(outputs="一段提问")
    report_task_outcome(task_status=VERDICT_CONTINUE, act_recap="在等用户回答", ctx=_ctx(t))
    assert t.observer_outcome == VERDICT_CONTINUE
    assert t.error is None, "没有受阻原因时不该在 task 上留下空的死因"


# ── ④ 那道机械护栏管不到「在问人」这件事 ──────────────────────────────────────


def test_the_no_outputs_guard_cannot_catch_a_question() -> None:
    """钉住一个容易被误信的事实：**park 的纯文本回合没有机械保险**。

    success-without-outputs 护栏读的是 `task.outputs`，而纯文本 park 之前
    `_synthesize_final_outputs` 已经把那段话写成了 outputs——哪怕它整段都是在问用户，
    outputs 也非空，护栏不触发，判 success 就会原样通过。

    所以「向用户要东西 → 一律 continue」这条只能由 prompt（ROLE 的判断准则 + 工具字段描述 +
    cue 的边界提醒）保证。谁将来想省掉那几段文案，先看这条测试。
    """
    asking = _task(outputs="这两个方案你想选哪个？")
    report_task_outcome(task_status=VERDICT_SUCCESS, act_recap="问了用户", ctx=_ctx(asking))
    assert asking.observer_outcome == VERDICT_SUCCESS, (
        "护栏若在此触发，说明它的判据变了——这条测试的前提（只靠 prompt）要重估")

    # 对照：真的没有产出时，护栏照旧把 success 降成 continue。
    empty = _task(outputs=None)
    report_task_outcome(task_status=VERDICT_SUCCESS, act_recap="什么也没产出", ctx=_ctx(empty))
    assert empty.observer_outcome == VERDICT_CONTINUE


def test_the_guard_downgrades_to_continue_not_to_retry() -> None:
    t = _task(outputs=None)
    res = report_task_outcome(task_status=VERDICT_SUCCESS, act_recap="r", ctx=_ctx(t))
    assert res.metadata["observer_outcome"] == VERDICT_CONTINUE
    assert "retry" not in res.content, f"确认话术里不该再出现旧词：{res.content!r}"
