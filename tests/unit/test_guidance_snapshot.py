from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from ctx_weft.core.assembler.composer_guidance import (
    build_act_guidance,
    build_resume_cue,
    collect_guidance_context,
    collect_resume_context,
)
from ctx_weft.core.assembler.prompts import load_prompt_template
from ctx_weft.core.assembler.sources.guidance import GuidanceSource
from ctx_weft.core.models.task import NormalTaskSettings, Task


@pytest.mark.parametrize("unattended", [True, False])
@pytest.mark.parametrize("has_child", [True, False])
@pytest.mark.asyncio
async def test_snapshot_renders_identical_text_and_budget_after_hint_consumption(unattended, has_child):
    task = Task(id="t", session_id="s", title="T", status="ACTIVE", unattended=unattended,
                next_step_hint="one shot", settings=NormalTaskSettings())
    child = Task(id="child", session_id="s", title="Child", parent_task_id="t", status="FINISHED",
                 outputs="Result", settings=NormalTaskSettings())
    manager = NS(all_tasks=lambda: [task, child] if has_child else [task])
    guidance = build_act_guidance(task, manager)
    resume = build_resume_cue(task, manager)
    snapshot = collect_guidance_context(task, manager)
    resume_snapshot = collect_resume_context(task, manager)
    task.next_step_hint = None
    child.outputs = "changed"
    request = NS(extra={"act_guidance_context": snapshot}, token_counter=len)
    first = [b async for b in GuidanceSource().fetch(request, None)]
    again = [b async for b in GuidanceSource().fetch(request, None)]
    assert first[0].content == again[0].content == guidance
    assert first[0].token_estimate == len(guidance)
    assert load_prompt_template("act").render("resume_snapshot", resume_snapshot) == resume
    assert "one shot" not in build_act_guidance(task, manager)


@pytest.mark.asyncio
async def test_real_prepare_reuses_snapshot_after_compact_and_consumes_hint(monkeypatch):
    import ctx_weft.core.loop.steps.act as act
    import ctx_weft.core.loop.steps.compact as compact
    import ctx_weft.core.loop.steps.prepare as prepare

    task = Task(id="t", session_id="s", title="T", status="ACTIVE",
                next_step_hint="one shot", settings=NormalTaskSettings())
    state = NS(task=task, agent=NS(), session=NS(), scope=NS(), extra={})
    texts = []

    async def assemble(request):
        assert task.next_step_hint is None
        blocks = [b async for b in GuidanceSource().fetch(request, None)]
        texts.append(blocks[0].content)
        return NS(token_count=1)

    ctx = NS(task_manager=None, llm=NS(tokenizer=NS(count=len)),
             assembler=NS(assemble=assemble), event_bus=NS(emit=AsyncMock()))
    step = prepare.PrepareStep()
    monkeypatch.setattr(step, "_estimate_tokens", AsyncMock(return_value=(10, True)))
    monkeypatch.setattr(step, "_load_skill_instructions", AsyncMock(return_value=""))
    monkeypatch.setattr(step, "_should_compact", AsyncMock(return_value=True))
    monkeypatch.setattr(prepare, "resolve_and_bind", AsyncMock(return_value=[]))
    monkeypatch.setattr(prepare, "make_event", lambda *args, **kw: NS())
    monkeypatch.setattr("ctx_weft.core.loop.steps.prepare.commit_round", AsyncMock())
    monkeypatch.setattr(compact, "escalating_compact", AsyncMock(return_value=[]))
    await step.execute(state, ctx)
    assert len(texts) == 2 and texts[0] == texts[1]
    assert "one shot" in texts[0]
    await step.execute(state, ctx)
    assert "one shot" not in texts[-1]
