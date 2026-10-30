
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from ctx_weft.core.assembler.sources.guidance import GuidanceSource
from ctx_weft.core.models.task import NormalTaskSettings, Task
from tests.assembler_testkit import compile_prompt, compiled_test_inputs, prompt_request


async def _snapshot(request, manager):
    blocks = [
        block
        async for block in GuidanceSource().fetch(
            request,
            NS(task_manager=manager),
        )
    ]
    return blocks[0].value


@pytest.mark.asyncio
async def test_guidance_source_collects_a_detached_task_state_snapshot():
    task = Task(
        id="t",
        session_id="s",
        title="T",
        status="ACTIVE",
        next_step_hint="one shot",
        settings=NormalTaskSettings(),
    )
    child = Task(
        id="child",
        session_id="s",
        title="Child",
        parent_task_id="t",
        status="FINISHED",
        outputs="Result",
        settings=NormalTaskSettings(),
    )
    manager = NS(all_tasks=lambda: [task, child])
    request = NS(purpose="act", task=task, extra={})

    snapshot = await _snapshot(request, manager)
    task.next_step_hint = None
    child.outputs = "changed"

    assert snapshot["guidance"]["review_note"] == "one shot"
    assert snapshot["guidance"]["completed_tasks"][0]["result"] == "Result"
    assert snapshot["resume"]["completed_tasks"] is True


@pytest.mark.asyncio
async def test_plan_renders_user_message_content_before_metadata_is_filled():
    task = Task(
        id="t", session_id="s", status="ACTIVE",
        user_prompt="修复登录问题\n保留现有接口",
    )
    other = Task(
        id="other", session_id="s", status="PENDING",
        user_prompt="检查权限",
    )
    request = prompt_request(task=task)
    manager = NS(all_tasks=lambda: [task, other])
    blocks = [
        block async for block in GuidanceSource().fetch(request, NS(task_manager=manager))
    ]

    prompt = compile_prompt(blocks, request)
    text = "\n".join(str(message.content) for message in prompt.messages)
    plan = text.split("## The overall plan", 1)[1]

    assert "- [ACTIVE] ▶ '修复登录问题' (t)" in plan
    assert "- [PENDING] '检查权限' (other)" in plan
    assert "User Message" not in plan
    assert task.title == ""


@pytest.mark.asyncio
async def test_prepare_collects_through_assembler_and_consumes_hint_after_reassembly(monkeypatch):
    import ctx_weft.core.loop.steps.compact as compact
    import ctx_weft.core.loop.steps.prepare as prepare

    task = Task(
        id="t",
        session_id="s",
        title="T",
        status="ACTIVE",
        next_step_hint="one shot",
        settings=NormalTaskSettings(),
    )
    manager = NS(all_tasks=lambda: [task])
    state = NS(task=task, agent=NS(), session=NS(), scope=NS(), extra={})
    snapshots = []

    async def assemble(request):
        snapshots.append(await _snapshot(request, manager))
        return NS(system="", messages=[], tools=[], token_count=1)

    ctx = NS(
        task_manager=manager,
        llm=NS(tokenizer=NS(count=len)),
        assembler=NS(assemble=assemble, compile=compiled_test_inputs),
        event_bus=NS(emit=AsyncMock()),
    )
    step = prepare.PrepareStep()
    monkeypatch.setattr(step, "_estimate_tokens", AsyncMock(return_value=(10, True)))
    monkeypatch.setattr(step, "_should_compact", AsyncMock(return_value=True))
    monkeypatch.setattr(prepare, "resolve_and_bind", AsyncMock(return_value=[]))
    monkeypatch.setattr(prepare, "make_event", lambda *args, **kwargs: NS())
    monkeypatch.setattr("ctx_weft.core.loop.steps.prepare.commit_round", AsyncMock())
    monkeypatch.setattr(compact, "escalating_compact", AsyncMock(return_value=[]))

    await step.execute(state, ctx)

    assert [item["guidance"]["review_note"] for item in snapshots] == [
        "one shot",
        "one shot",
    ]
    assert task.next_step_hint is None
