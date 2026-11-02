"""Task-result labels close child execution; only real tools require adjacency."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from ctx_weft.core.assembler.task_memory import render_task_record, task_record_metadata
from ctx_weft.core.loop.steps.finalize import _close_one, ensure_dispatch_frame_at_start
from ctx_weft.core.models.task import Task
from ctx_weft.protocols import MemoryAddress, MemoryEvent, MemoryKind, MemoryScope, load_records
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from ctx_weft.providers.memory.sql.provider import open_sqlite_memory
from tests.assembler_testkit import compile_prompt, prompt_request
from tests.unit.test_capsule_variants import CTX, NOW, SCOPE, blocks, body, closure


def dispatch(child="t", parent="p", *, seconds=-1, legacy=False):
    common = {"origin_task_id": parent, "child_task_id": child}
    start = MemoryEvent(
        id=f"start-{child}",
        kind=MemoryKind.CONVERSATION_TURN,
        scope=MemoryScope.AGENT,
        address=SCOPE,
        timestamp=NOW + timedelta(seconds=seconds),
        role="user",
        content=render_task_record("start", task_id=child, description="Child request"),
        metadata={**common, "task_memory": task_record_metadata("start", f"dispatch-{child}")},
    )
    result = replace(
        start,
        id=f"result-{child}",
        content=render_task_record("result", task_id=child, body="Child completed"),
        metadata={**common, "task_memory": task_record_metadata("result", f"dispatch-{child}")},
    )
    if legacy:
        start = replace(
            start,
            role="assistant",
            content="",
            metadata={
                **common,
                "tool_calls": [
                    {
                        "id": f"dispatch-{child}",
                        "name": "control__delegate_task",
                        "input": {"task_id": child},
                    }
                ],
            },
        )
        result = replace(
            result,
            role="tool",
            content="Child completed",
            metadata={**common, "tool_call_id": f"dispatch-{child}"},
        )
    return [start, result]


def text(records, task_id="p"):
    return "\n".join(
        str(m.content)
        for m in compile_prompt(blocks(records), prompt_request(task_id=task_id)).messages
    )


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("minimal", [False, True])
def test_old_result_at_start_is_projected_after_full_or_minimal_child(legacy, minimal):
    start, result = dispatch(legacy=legacy)
    closed = closure(parent="p")
    if minimal:
        closed = [closed[-1]]
        closed[0].metadata["finish_policy"].update(capsule_state="lean", slot_count=1)
    records = [start, result, *body(), *closed, body("p", seconds=10)[0]]
    rendered = text(records)
    final_tag = "task_finish" if minimal else "task_final_reply"
    assert (
        rendered.index('<task_start task_id="t"')
        < rendered.index(f'<{final_tag} task_id="t"')
        < rendered.index('<task_result task_id="t"')
        < rendered.index("## Current Task")
    )
    assert result.timestamp == start.timestamp  # read projection does not mutate storage


def test_nested_results_close_their_own_subtrees_before_parent_resumes():
    records = [
        *dispatch(),
        *body(),
        *dispatch("g", "t", seconds=2),
        *body("g", seconds=3),
        *closure("g", parent="t", seconds=5),
        *closure(parent="p", seconds=8),
        body("p", seconds=10)[0],
    ]
    rendered = text(records)
    tags = [
        '<task_start task_id="t"',
        '<task_start task_id="g"',
        '<task_final_reply task_id="g"',
        '<task_result task_id="g"',
        '<task_final_reply task_id="t"',
        '<task_result task_id="t"',
    ]
    positions = [rendered.index(tag) for tag in tags]
    assert positions == sorted(positions)


def test_running_ack_stays_at_start_without_inventing_a_completion():
    records = [*dispatch(), *body()]
    rendered = text(records)
    assert rendered.index('<task_start task_id="t"') < rendered.index('<task_result task_id="t"')
    assert rendered.index('<task_result task_id="t"') < rendered.index("Delivered answer")


@pytest.mark.asyncio
@pytest.mark.parametrize("same_agent", [True, False])
async def test_terminal_result_uses_durable_completion_time_after_sqlite_reopen(
    tmp_path, same_agent
):
    task = Task(
        id="t",
        session_id="s",
        status="FINISHED",
        parent_task_id="p",
        title="Child request",
        creator_agent_id="a",
        assigned_agent_id="a" if same_agent else "b",
        started_at=NOW,
        outputs="Delivered answer",
    )
    scope = MemoryAddress(session_id="s", agent_id=task.assigned_agent_id, task_id="t")
    completion = NOW + timedelta(seconds=5)
    state = SimpleNamespace(
        task=task,
        scope=scope,
        extra={"observe_id": "obs", "observe_completion": {"completed_at": completion.isoformat()}},
        run_id="run",
        sequence_counter=0,
        session=SimpleNamespace(id="s", tenant_id="default"),
        agent=SimpleNamespace(id=task.assigned_agent_id),
    )
    database = tmp_path / "memory.db"
    async with open_sqlite_memory(database) as memory:
        ctx = SimpleNamespace(memory=memory, provider_ctx=CTX)
        await ensure_dispatch_frame_at_start(state, ctx)
        await _close_one(
            memory,
            state,
            task,
            "Delivered answer; essential result",
            "success",
            ctx,
            short=False,
            act_recap="Actual work",
            task_summary="Goal achieved",
        )
    async with open_sqlite_memory(database) as memory:
        rows = await load_records(memory, SCOPE, MemoryScope.AGENT, CTX)
        result = next(r for r in rows if r.metadata.get("dispatch_state") == "closed")
        assert result.timestamp == completion + timedelta(microseconds=3)
        if same_agent:
            rendered = text(rows)
            assert rendered.index('<task_final_reply task_id="t"') < rendered.index(
                '<task_result task_id="t"'
            )
        else:
            assert "Delivered answer; essential result" in result.content
            assert not any(r.metadata.get("origin_task_id") == task.id for r in rows)
        before = [(r.id, r.timestamp) for r in rows]
        await _close_one(
            memory,
            state,
            task,
            "Delivered answer; essential result",
            "success",
            SimpleNamespace(memory=memory, provider_ctx=CTX),
            short=False,
            act_recap="Actual work",
            task_summary="Goal achieved",
        )
        after = await load_records(memory, SCOPE, MemoryScope.AGENT, CTX)
        assert [(r.id, r.timestamp) for r in after] == before


@pytest.mark.asyncio
async def test_inherited_snapshot_preserves_the_repaired_legacy_order():
    from ctx_weft.core.runtime import _copy_memory_for_inherit

    memory = InMemoryMemoryProvider()
    for record in [*dispatch(legacy=True), *body(), *closure(parent="p")]:
        await memory.ingest(record, CTX)
    await _copy_memory_for_inherit(
        source_agent_id="a",
        child_task=SimpleNamespace(id="inheritor"),
        sub_agent=SimpleNamespace(id="b"),
        memory=memory,
        session_id="s",
        tenant_id="default",
    )
    rows = await load_records(
        memory, MemoryAddress(session_id="s", agent_id="b"), MemoryScope.AGENT, CTX
    )
    rendered = text(rows, "inheritor")
    assert rendered.index('<task_final_reply task_id="t"') < rendered.index(
        '<task_result task_id="t"'
    )
