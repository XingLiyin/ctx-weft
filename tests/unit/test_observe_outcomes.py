"""observe 三态机 + report_task_outcome 枚举。

锁定：
- report_task_outcome 接受 success/retry/fail，写 task.observer_outcome + task.status
- 护栏：success 但无 outputs → 降级 retry
- 非法值（含旧 active/ask_human）→ retry
- ObserveStep 规则降级：机械退出(max_turns/context_limit) → retry；正常退出 → success
"""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.loop.steps.observe import ObserveStep
from ctx_weft.core.orchestrator.control_capability import ControlContext, report_task_outcome
from ctx_weft.core.state.models import Task


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
    report_task_outcome(task_status="success", act_recap="ok", ctx=_ctx(t))
    assert t.observer_outcome == "success"
    assert t.status == "FINISHED"


def test_assessment_fail() -> None:
    t = _task(outputs="x")
    report_task_outcome(
        task_status="fail", act_recap="bad", task_failure_reason="root cause", ctx=_ctx(t)
    )
    assert t.observer_outcome == "fail"
    assert t.status == "FAILED"
    assert t.error == "root cause"


def test_assessment_retry() -> None:
    t = _task(outputs="partial")
    report_task_outcome(
        task_status="retry", act_recap="more needed", next_step_hint="do X", ctx=_ctx(t)
    )
    assert t.observer_outcome == "retry"
    assert t.status == "PENDING"
    assert "do X" in t.process_report  # next_step_hint 并入 report


def test_assessment_success_without_outputs_downgrades_to_retry() -> None:
    t = _task(outputs=None)
    report_task_outcome(task_status="success", act_recap="claims done", ctx=_ctx(t))
    assert t.observer_outcome == "retry"  # 护栏：无终稿 → 重试
    assert t.status == "PENDING"


def test_assessment_invalid_defaults_to_retry() -> None:
    t = _task(outputs="x")
    report_task_outcome(task_status="active", act_recap="r", ctx=_ctx(t))
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


# ── _should_use_llm gate tests ────────────────────────────────────────────────


def _llm_gate_state(exit_reason: str, parent_task_id, has_role: bool = True):
    identity = {"observe": SimpleNamespace()} if has_role else {}
    return SimpleNamespace(
        extra={"template": SimpleNamespace(identity=identity)},
        act_exit_reason=exit_reason,
        task=SimpleNamespace(parent_task_id=parent_task_id),
    )


def test_max_turns_forces_llm_even_for_root() -> None:
    s = _llm_gate_state("max_turns", None, has_role=True)
    assert ObserveStep()._should_use_llm(s) is True


def test_root_normal_exit_stays_rule() -> None:
    s = _llm_gate_state("normal", None, has_role=True)
    assert ObserveStep()._should_use_llm(s) is False


def test_no_role_stays_rule_even_at_max_turns() -> None:
    s = _llm_gate_state("max_turns", None, has_role=False)
    assert ObserveStep()._should_use_llm(s) is False


def test_delegated_normal_exit_uses_llm() -> None:
    s = _llm_gate_state("normal", "parent1", has_role=True)
    assert ObserveStep()._should_use_llm(s) is True


def test_verdict_has_act_recap_and_task_summary_fields():
    from ctx_weft.core.loop.steps.observe import Verdict
    v = Verdict(task_outcome="success", act_recap="did X", task_summary="whole journey")
    assert v.act_recap == "did X"
    assert v.task_summary == "whole journey"
    assert v.reported is False
    # task_summary 默认空
    assert Verdict(task_outcome="retry", act_recap="r").task_summary == ""


def test_task_model_has_task_summary_field():
    from ctx_weft.core.state.models import Task
    t = Task(id="t1", session_id="s1", status="ACTIVE")
    assert t.task_summary is None
    t.task_summary = "comprehensive"
    assert t.task_summary == "comprehensive"


def test_report_task_outcome_writes_act_recap_and_task_summary() -> None:
    task = _task(outputs="done")
    ctx = _ctx(task)
    report_task_outcome(
        task_status="success",
        act_recap="本轮我创建了 skill 文件并验证",
        task_summary="整段：看模板→写 SKILL.md→写脚本→验证，已就绪",
        ctx=ctx,
    )
    assert task.process_report == "本轮我创建了 skill 文件并验证"
    assert task.task_summary == "整段：看模板→写 SKILL.md→写脚本→验证，已就绪"
    assert task.observer_outcome == "success"
