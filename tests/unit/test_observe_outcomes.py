"""observe 三态机 + report_task_outcome 枚举。

锁定：
- report_task_outcome 接受 success/retry/fail，写 task.observer_outcome + task.status
- 护栏：success 但无 outputs → 降级 retry
- 非法值（含旧 active/ask_human）→ retry
- ObserveStep 规则降级：机械退出(max_turns/context_limit) → retry；正常退出 → success
"""

from __future__ import annotations

from types import SimpleNamespace

from loomex_core.core.loop.steps.observe import ObserveStep
from loomex_core.core.orchestrator.control_capability import ControlContext, report_task_outcome
from loomex_core.core.state.models import Task


def _task(**kw) -> Task:
    return Task(id="t1", session_id="s1", status="ACTIVE", **kw)


def _ctx(task: Task) -> ControlContext:
    return ControlContext(
        session_id="s1", task_id="t1", agent_id="a1", task=task,
        task_manager=None, session=None,
    )


# ── report_task_outcome 五态 ──────────────────────────────────────────────────


def test_assessment_success() -> None:
    t = _task(outputs="done")
    report_task_outcome(task_status="success", task_process_report="ok", ctx=_ctx(t))
    assert t.observer_outcome == "success"
    assert t.status == "FINISHED"


def test_assessment_fail() -> None:
    t = _task(outputs="x")
    report_task_outcome(
        task_status="fail", task_process_report="bad", task_failure_reason="root cause", ctx=_ctx(t)
    )
    assert t.observer_outcome == "fail"
    assert t.status == "FAILED"
    assert t.error == "root cause"


def test_assessment_retry() -> None:
    t = _task(outputs="partial")
    report_task_outcome(
        task_status="retry", task_process_report="more needed", next_step_hint="do X", ctx=_ctx(t)
    )
    assert t.observer_outcome == "retry"
    assert t.status == "PENDING"
    assert "do X" in t.process_report  # next_step_hint 并入 report


def test_assessment_success_without_outputs_downgrades_to_retry() -> None:
    t = _task(outputs=None)
    report_task_outcome(task_status="success", task_process_report="claims done", ctx=_ctx(t))
    assert t.observer_outcome == "retry"  # 护栏：无终稿 → 重试
    assert t.status == "PENDING"


def test_assessment_invalid_defaults_to_retry() -> None:
    t = _task(outputs="x")
    report_task_outcome(task_status="active", task_process_report="r", ctx=_ctx(t))
    assert t.observer_outcome == "retry"  # 'active' 不在工具允许集


# ── ObserveStep 规则降级 ─────────────────────────────────────────────────────────


def _state(exit_reason: str, task: Task):
    return SimpleNamespace(
        transcript=[SimpleNamespace(tool_calls=[])],
        act_exit_reason=exit_reason,
        task=task,
    )


def test_rule_observe_mechanical_exit_is_retry() -> None:
    for reason in ("max_turns", "context_limit"):
        t = _task()
        v = ObserveStep()._rule_observe(_state(reason, t))
        assert v.task_outcome == "retry", reason
        assert t.status == "PENDING"
        assert t.observer_outcome == "retry"


def test_rule_observe_normal_exit_is_success() -> None:
    t = _task()
    v = ObserveStep()._rule_observe(_state("normal", t))
    assert v.task_outcome == "success"
    assert t.status == "FINISHED"


def test_rule_observe_no_transcript_is_fail() -> None:
    t = _task()
    state = SimpleNamespace(transcript=[], act_exit_reason="normal", task=t)
    v = ObserveStep()._rule_observe(state)
    assert v.task_outcome == "fail"
    assert t.status == "FAILED"
