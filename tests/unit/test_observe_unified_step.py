"""ObserveStep owns both modes, regardless of chain/standalone scheduling."""
import dataclasses
from types import SimpleNamespace as NS

import pytest

from ctx_weft.core.capabilities.control_tools import report_observation
from ctx_weft.core.loop.driver import ExecutionMode, StepExecution
from ctx_weft.core.loop.steps import observe as observing
from ctx_weft.core.loop.steps.observe import ObservationResult, ObserveStep, Verdict
from ctx_weft.core.models.task import Task
from ctx_weft.protocols import IdentityFacet
from tests.unit.test_observe_react_helper import (
    _make_ctx,
    _make_state,
    _make_tool_call_chunk,
)


def state():
    result = _make_state()
    result.task = Task(id="t1", session_id="s1", status="ACTIVE", outputs="Delivered",
                       parent_task_id="parent")
    result.extra["template"] = NS(identity={"observe": IdentityFacet(text="ROLE")})
    result.act_exit_reason = "actor_done"
    result.transcript = [NS()]
    return result


@pytest.mark.parametrize("standalone", [False, True])
@pytest.mark.parametrize("judge_task", [False, True])
async def test_same_tool_and_purpose_with_optional_judgement(monkeypatch, standalone, judge_task):
    current = state()
    before = dataclasses.asdict(current.task)
    calls, requests = [], []

    class Gateway:
        async def invoke(self, **kwargs):
            assert dataclasses.asdict(current.task) == before
            calls.append(kwargs)
            submitted = report_observation(
                act_recap="incremental", task_summary="whole task", task_status="success",
                next_step_hint="hint", task_failure_reason="ignored if recap",
            )
            return NS(content=submitted.content, metadata=submitted.metadata, is_error=False)

    ctx = _make_ctx("", gateway=Gateway())
    original = ctx.assembler.assemble

    async def assemble(request):
        requests.append(request)
        return await original(request)

    ctx.assembler.assemble = assemble

    async def stream(*args):
        yield _make_tool_call_chunk("control__report_observation")

    monkeypatch.setattr(observing, "stream_llm_resilient", stream)
    outcome = await _collect(current, ctx, judge_task=judge_task, background=standalone)
    observed = outcome.state_patch["observation"]
    assert requests[0].purpose == "observe"
    assert requests[0].intent.judge_task == judge_task
    assert len(calls) == 1
    assert observed.act_recap == "incremental"
    assert observed.task_summary == "whole task"
    assert (observed.verdict is not None) == judge_task
    assert outcome.next_step == ("finalize" if judge_task and not standalone else None)
    if standalone or not judge_task:
        assert dataclasses.asdict(current.task) == before
    else:
        assert current.task.observer_outcome == "success"
        assert current.task.process_report == "incremental"


async def test_missing_verdict_is_retried_before_accepting_submission(monkeypatch):
    current = state()
    received = []

    class Gateway:
        async def invoke(self, **kwargs):
            submitted = report_observation(
                act_recap="recap", task_status="continue" if len(received) > 1 else None)
            return NS(content=submitted.content, metadata=submitted.metadata, is_error=False)

    async def stream(ctx, state, request):
        received.append(request)
        yield _make_tool_call_chunk("control__report_observation")

    monkeypatch.setattr(observing, "stream_llm_resilient", stream)
    outcome = await _collect(current, _make_ctx("", gateway=Gateway()), background=True)
    assert len(received) == 2
    assert "task_status" in received[1].messages[-1].content
    assert outcome.state_patch["verdict"].task_outcome == "continue"
    assert outcome.state_patch["verdict"].reported
    assert current.task.observer_outcome is None


@pytest.mark.parametrize("standalone", [False, True])
@pytest.mark.parametrize("boundary", ["max_turns", "context_limit"])
async def test_step_overrides_success_at_mechanical_limits(monkeypatch, standalone, boundary):
    current = state()
    current.act_exit_reason = boundary

    async def observe(*args):
        return ObservationResult("recap", "", Verdict("success", "recap", reported=True))

    async def fold(*args):
        pass

    monkeypatch.setattr(ObserveStep, "_llm_observe", observe)
    monkeypatch.setattr(ObserveStep, "_fold_retry_segment", fold)
    outcome = await _collect(current, _make_ctx(""), background=standalone)
    assert outcome.state_patch["verdict"].task_outcome == "continue"
    assert outcome.state_patch["observation"].act_recap == "recap"
    assert current.task.observer_outcome == (None if standalone else "continue")


@pytest.mark.parametrize("boundary", ["plain_text", "finish_park"])
async def test_background_failure_does_not_close_a_parked_task(monkeypatch, boundary):
    current = state()
    current.task.status = "AWAITING_HUMAN"

    async def fail(*args):
        raise RuntimeError("model failed")

    monkeypatch.setattr(ObserveStep, "_llm_observe", fail)
    outcome = await _collect(current, _make_ctx(""), background=True, boundary=boundary)
    observation = outcome.state_patch["observation"]
    assert observation.verdict.task_outcome == "continue"
    assert not observation.verdict.reported
    assert observation.act_recap == observation.task_summary == ""
    assert current.task.status == "AWAITING_HUMAN"
    assert current.task.observer_outcome is None


async def test_unadvertised_ask_user_is_never_dispatched(monkeypatch):
    current = state()

    class Gateway:
        async def invoke(self, **kwargs):
            pytest.fail("ask_user must not reach the gateway")

    async def stream(*args):
        yield _make_tool_call_chunk("control__ask_user")

    monkeypatch.setattr(observing, "stream_llm_resilient", stream)
    outcome = await _collect(current, _make_ctx("", gateway=Gateway()), judge_task=False, background=True)
    assert outcome.state_patch["observation"].verdict is None
    assert current.task.observer_outcome is None

async def _collect(current, ctx, *, background=False, judge_task=True, boundary=None):
    """Exercise observation decision logic; settlement is covered by background tests."""
    boundary = boundary or current.act_exit_reason
    ctx.execution = StepExecution(
        ExecutionMode.BACKGROUND if background else ExecutionMode.FOREGROUND,
        {"judge_task": judge_task, "boundary": boundary},
    )
    step = ObserveStep()
    if background:
        result = await step._observe(current, ctx, [], judge_task=judge_task, boundary=boundary)
        return step._outcome(result)
    return await step.execute(current, ctx)
