"""Recovered observe results may mutate business state only after TM arbitration."""
from copy import copy
from types import SimpleNamespace as NS

import pytest

from ctx_weft.core.loop.steps.observe import ObservationResult, Verdict, apply_foreground_assessment, submit_verdict
from ctx_weft.core.models.task import Task
from ctx_weft.core.orchestrator.task.manager import TaskManager


@pytest.mark.parametrize("status", ["CANCELED", "FAILED"])
async def test_obsolete_recovered_close_preserves_winning_terminal_fields(status):
    task = Task(id="task", session_id="session", status=status,
                outputs="winning result", error="winning reason", observer_outcome="fail")
    manager = TaskManager("session")
    manager.restore([task], {task.id}, enqueue=False)
    original = copy(task)
    producer = copy(task)
    producer.outputs = "obsolete actor output"
    state = NS(task=task, extra={"observe_id": "observe", "observe_context_task": producer,
        "observe_completion": {"accepted": True, "action": "close",
            "result": {"verdict": {"task_outcome": "success", "failure_reason": ""}},
            "disposition": {"status": "FINISHED", "event_type": "TaskFinished", "payload": {}}}})
    assert not await submit_verdict(state, NS(task_manager=manager), {})
    assert task == original
    apply_foreground_assessment(state,
        ObservationResult(verdict=Verdict("success", "obsolete report", reported=True)), "finish")
    assert task == original
