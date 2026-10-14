"""Unified observation submission is inert; the step owns all judgement rules."""
import dataclasses
from types import SimpleNamespace as NS

import pytest

from ctx_weft.core.capabilities.control_tools import (
    ControlCapabilityProvider,
    ControlContext,
    report_observation,
)
from ctx_weft.core.capabilities.control_tools import (
    ControlMetaKey as K,
)
from ctx_weft.core.loop.steps.observe import ObserveStep
from ctx_weft.core.models.task import Task
from ctx_weft.protocols.context import ProviderContext


def submit(task, **arguments):
    ctx = ControlContext(session_id="s1", task_id=task.id, agent_id="a1",
                         task=task, task_manager=None, session=None)
    before = dataclasses.asdict(task)
    result = report_observation(ctx=ctx, **arguments)
    assert dataclasses.asdict(task) == before
    return result


@pytest.mark.parametrize("status", [None, "success", "continue", "fail"])
def test_submission_never_mutates_task(status):
    task = Task(id="t1", session_id="s1", status="ACTIVE", outputs="deliverable", error="previous error")
    result = submit(task, act_recap="segment", task_summary="whole task",
                    task_status=status, next_step_hint="hint", task_failure_reason="reason")
    assert result.metadata[K.OBSERVER_ACT_RECAP] == "segment"
    assert result.metadata[K.OBSERVER_TASK_SUMMARY] == "whole task"
    if status is None:
        assert K.OBSERVER_OUTCOME not in result.metadata
    else:
        assert result.metadata[K.OBSERVER_OUTCOME] == status
        assert result.metadata[K.OBSERVER_NEXT_STEP_HINT] == "hint"


def test_summary_only_ignores_all_verdict_fields():
    task = Task(id="t1", session_id="s1", status="ACTIVE")
    result = submit(task, act_recap="segment", task_summary="whole", task_status="fail",
                    task_failure_reason="reason", next_step_hint="hint")
    step = ObserveStep()
    observation = step._interpret_submission(NS(task=task), result, judge_task=False)
    assert observation.verdict is None
    assert observation.metadata() == {
        K.OBSERVER_ACT_RECAP: "segment", K.OBSERVER_TASK_SUMMARY: "whole"}


def test_judgement_requires_status_but_recap_does_not():
    result = report_observation(act_recap="segment")
    assert ObserveStep()._submission_error(result)
    assert ObserveStep()._submission_error(result, judge_task=False) is None


def test_no_output_guard_is_owned_by_step_and_does_not_mutate_task():
    task = Task(id="t1", session_id="s1", status="ACTIVE")
    submitted = submit(task, act_recap="segment", task_status="success")
    assert submitted.metadata[K.OBSERVER_OUTCOME] == "success"
    observation = ObserveStep()._interpret_submission(NS(task=task), submitted)
    assert observation.verdict.task_outcome == "continue"
    assert "without a final output" in observation.verdict.next_step_hint
    assert task.observer_outcome is None


async def test_one_purpose_one_submission_tool_and_no_ask_user():
    tools = await ControlCapabilityProvider().list(ProviderContext(session_id="s1"))
    observed = [t for t in tools if "observe" in t.purposes]
    assert [t.id for t in observed] == ["control:report_observation"]
    schema = observed[0].input_schema
    assert schema["required"] == ["act_recap"]
    assert "task_status" in schema["properties"]
    assert not any(set(t.purposes) & {"background_observe", "background_recap"} for t in tools)
