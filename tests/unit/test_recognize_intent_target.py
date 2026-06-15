"""RecognizeIntentStep targets state.task directly (no MetadataFillerTaskSettings)."""

from __future__ import annotations

from types import SimpleNamespace

from loomex_core.core.loop.steps.recognize_intent import RecognizeIntentStep


async def test_skips_when_title_present():
    emitted = []
    async def _emit(ev):
        emitted.append(ev)
    state = SimpleNamespace(
        task=SimpleNamespace(id="t1", title="already set", settings=SimpleNamespace()),
        agent=SimpleNamespace(id="a1"),
        session=SimpleNamespace(id="s1", tenant_id="te1"),
        scope=SimpleNamespace(session_id="s1", task_id="t1", agent_id="a1"),
        extra={"template": None},
        run_id="r1",
        sequence_counter=0,
    )
    ctx = SimpleNamespace(task_manager=None, event_bus=SimpleNamespace(emit=_emit))
    outcome = await RecognizeIntentStep().execute(state, ctx)
    assert outcome.next_step is None
