"""should_recognize_intent predicate + launch_recognize_intent concurrency."""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.loop.steps.recognize_intent import (
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
        from ctx_weft.core.loop.driver import StepOutcome
        seen["run_id"] = state.run_id
        seen["bound"] = state.extra.get("bound_capabilities")
        return StepOutcome(next_step=None)

    monkeypatch.setattr(RecognizeIntentStep, "execute", _fake_execute)

    state = SimpleNamespace(
        run_id="orig-run",
        sequence_counter=5,
        session=SimpleNamespace(id="s1", tenant_id="default"),
        # status: 总账 C5（批次二 Task 5）—— `_run()` 现在也发 RunStarted/
        # RunFinished，`final_status` 取 `snapshot.task.status`（照抄 `_run_loop`
        # 的实际发射点），缺了这个字段会 AttributeError 直接炸穿。
        task=SimpleNamespace(id="t1", parent_task_id=None, title="", status="ACTIVE"),
        agent=SimpleNamespace(id="a1"),
        scope=SimpleNamespace(),
        assembled_prompt=object(),
        transcript=[object()],
        verdict=object(),
        extra={"template": SimpleNamespace(), "bound_capabilities": ["CAP"]},
        resolved_model=SimpleNamespace(model="mock", account=""),
    )

    class _FakeEventBus:
        def __init__(self) -> None:
            self.emitted: list = []

        async def emit(self, event) -> None:
            self.emitted.append(event)

    ctx = SimpleNamespace(task_manager=None, event_bus=_FakeEventBus())

    task = launch_recognize_intent(state, ctx)
    await task

    assert seen["bound"] == ["CAP"]
    assert seen["run_id"] != "orig-run"  # ran on a fresh snapshot, not the live state
