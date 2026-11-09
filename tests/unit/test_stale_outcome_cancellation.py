"""Late run completion cannot override a canceled task or its tree marker."""

from unittest.mock import AsyncMock

import pytest

from ctx_weft.core.orchestrator.lifecycle.agent_state import AGENT_CANCEL_REASON
from ctx_weft.core.orchestrator.task.disposition import RunOutcome, RunOutcomeKind
from ctx_weft.protocols.events import EventType
from tests.unit.test_task_disposition_publication import setup


@pytest.mark.parametrize("verdict", ["continue", "waiting", "success", "fail"])
@pytest.mark.parametrize("status", ["ACTIVE", "CANCELED"])
async def test_cancellation_outranks_saved_completed_disposition(verdict, status):
    tm, task, _, store = setup()
    task.status = status
    task.error = f"{AGENT_CANCEL_REASON}operator stopped the work"
    outcome = RunOutcome(RunOutcomeKind.COMPLETED, verdict=verdict, observe_id="obs1",
        observe_disposition={"status": "PENDING", "event_type": "TaskRequeued", "payload": {}})
    assert await tm.apply_run_outcome(task.id, outcome) == "CANCELED"
    assert task.status == "CANCELED"
    rows = await store.read_range("s1")
    assert [r.event.type for r in rows] == ([EventType.TASK_CANCELED] if status == "ACTIVE" else [])
    assert task.error.startswith(AGENT_CANCEL_REASON)


async def test_cancel_before_recovery_coroutine_starts_does_not_publish_task_started():
    tm, task, _, store = setup()
    tm.set_runner(AsyncMock())
    task.status = "INTERRUPTED"
    retired = []

    async def retire_observation():
        retired.append(task.status)
        return RunOutcome(RunOutcomeKind.COMPLETED, verdict="continue")

    job = tm.start_foreground_recovery(task.id, "agent1", retire_observation)
    await tm.cancel_task_tree(task.id, f"{AGENT_CANCEL_REASON}stopped before recovery started")
    await job
    assert retired == ["CANCELED"] and task.status == "CANCELED"
    events = [r.event.type for r in await store.read_range("s1")]
    assert events == [EventType.TASK_CANCELED]
    assert not tm.running_task_ids() and not tm._dispatch_tasks
