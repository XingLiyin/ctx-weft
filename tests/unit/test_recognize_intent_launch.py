"""should_recognize_intent predicate + launch_recognize_intent concurrency."""

from __future__ import annotations

from types import SimpleNamespace

from loomex_core.core.loop.steps.recognize_intent import (
    RecognizeIntentStep,
    should_recognize_intent,
    launch_recognize_intent,
)


def test_should_fill_root_with_empty_title():
    assert should_recognize_intent(SimpleNamespace(parent_task_id=None, title=""))


def test_skip_when_title_present():
    assert not should_recognize_intent(SimpleNamespace(parent_task_id=None, title="x"))


def test_skip_when_subtask():
    assert not should_recognize_intent(SimpleNamespace(parent_task_id="p", title=""))


async def test_launch_runs_step_on_snapshot(monkeypatch):
    seen = {}

    async def _fake_execute(self, state, ctx):
        from loomex_core.core.loop.driver import StepOutcome
        seen["run_id"] = state.run_id
        seen["bound"] = state.extra.get("bound_capabilities")
        return StepOutcome(next_step=None)

    monkeypatch.setattr(RecognizeIntentStep, "execute", _fake_execute)

    state = SimpleNamespace(
        run_id="orig-run",
        sequence_counter=5,
        session=SimpleNamespace(id="s1"),
        task=SimpleNamespace(id="t1", parent_task_id=None, title=""),
        agent=SimpleNamespace(id="a1"),
        scope=SimpleNamespace(),
        assembled_prompt=object(),
        transcript=[object()],
        verdict=object(),
        extra={"template": SimpleNamespace(), "bound_capabilities": ["CAP"]},
    )
    ctx = SimpleNamespace(task_manager=None)

    task = launch_recognize_intent(state, ctx)
    await task

    assert seen["bound"] == ["CAP"]
    assert seen["run_id"] != "orig-run"  # ran on a fresh snapshot, not the live state
