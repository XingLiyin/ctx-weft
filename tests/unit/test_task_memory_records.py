from dataclasses import replace
from datetime import UTC, datetime

import pytest

from ctx_weft.core.assembler import prompts
from tests.task_memory_testkit import project_task_history, render_task_record
from ctx_weft.core.loop.task_memory_fold import task_record_kind
from tests.compaction_testkit import _dispatch_finish_sets, _split_for_collapse
from ctx_weft.core.loop.task_memory_fold import (
    build_finish_slots,
    find_finish_target,
    replace_finish_report,
)
from ctx_weft.protocols import (
    MemoryAddress,
    MemoryEvent,
    MemoryKind,
    MemoryScope,
    ProviderContext,
    load_records,
)
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider

SCOPE = MemoryAddress(session_id="s", agent_id="a")
CTX = ProviderContext(session_id="s", agent_id="a")
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def slots(reply="Delivered answer"):
    events = build_finish_slots(
        scope=SCOPE,
        task_id="t",
        parent_task_id=None,
        title="Title",
        outcome="success",
        act_recap="Actual work",
        task_summary="Report",
        final_reply=reply,
        base=NOW,
        tool_call_id="closure",
    )
    policy = {
        "slot_count": len(events),
        "preserve_raw": not bool(reply),
        "outcome": "success",
        "observe_id": "obs",
    }
    return [
        replace(e, id=f"slot-{i}", metadata={**e.metadata, "finish_policy": policy})
        for i, e in enumerate(events)
    ]


@pytest.mark.parametrize("reply", ["", "Delivered answer"])
def test_new_closures_are_tagged_plain_messages(reply):
    records = slots(reply)
    assert all(
        not r.metadata.get("tool_calls") and not r.metadata.get("tool_call_id") for r in records
    )
    assert all(r.role != "tool" for r in records)
    assert task_record_kind(records[-1]) == "finish"
    assert "<task_finish" in records[-1].content
    assert sum(reply in r.content for r in records) == (1 if reply else len(records))
    assert _dispatch_finish_sets(records) == (set(), {"t"})
    assert _split_for_collapse(records, 1)[0] == []


def test_new_content_is_not_rendered_again():
    records = slots()
    assert project_task_history(records) == records


def test_legacy_pair_is_projected_without_changing_ids_or_source_records():
    call = {"id": "c", "name": "control__finish_task", "input": {}}
    assistant = MemoryEvent(
        id="old-a",
        kind=MemoryKind.CONVERSATION_TURN,
        scope=MemoryScope.AGENT,
        address=SCOPE,
        timestamp=NOW,
        role="assistant",
        content="Original answer",
        metadata={"origin_task_id": "t", "final_reply": True, "tool_calls": [call]},
    )
    report = replace(
        assistant,
        id="old-r",
        role="tool",
        content="Real process report",
        metadata={"origin_task_id": "t", "tool_call_id": "c"},
    )
    projected = project_task_history([assistant, report])
    assert [r.id for r in projected] == ["old-a", "old-r"]
    assert [r.role for r in projected] == ["assistant", "user"]
    assert all(
        not r.metadata.get("tool_calls") and not r.metadata.get("tool_call_id") for r in projected
    )
    assert "Original answer" in projected[0].content
    assert "Real process report" in projected[1].content
    assert assistant.metadata["tool_calls"] == [call]
    task_record = replace(
        assistant,
        scope=MemoryScope.TASK,
        address=MemoryAddress(session_id="s", agent_id="a", task_id="t"),
    )
    assert project_task_history([task_record])[0].metadata["tool_calls"] == [call]


def test_legacy_start_uses_structured_arguments():
    record = MemoryEvent(
        id="start",
        kind=MemoryKind.CONVERSATION_TURN,
        scope=MemoryScope.AGENT,
        address=SCOPE,
        timestamp=NOW,
        role="assistant",
        content="",
        metadata={
            "origin_task_id": "parent",
            "child_task_id": "child",
            "tool_call_id": "call",
            "tool_calls": [
                {
                    "id": "call",
                    "name": "control__start_task",
                    "input": {
                        "task_id": "child",
                        "title": 'A "quoted" task',
                        "description": "Do work",
                    },
                }
            ],
        },
    )
    (projected,) = project_task_history([record])
    assert 'task_id="child"' in projected.content
    assert "&#34;quoted&#34;" in projected.content
    assert "Do work" in projected.content
    assert projected.role == "user"


def test_real_plan_envelopes_are_not_converted():
    record = MemoryEvent(
        id="plan",
        kind=MemoryKind.CONVERSATION_TURN,
        scope=MemoryScope.AGENT,
        address=SCOPE,
        timestamp=NOW,
        role="assistant",
        content="",
        metadata={"tool_calls": [{"id": "plan", "name": "control__delegate_plan", "input": {}}]},
    )
    assert project_task_history([record]) == [record]


def test_writing_and_legacy_projection_share_the_configurable_jinja(monkeypatch):
    environment = prompts._JINJA.overlay(
        loader=__import__("jinja2").DictLoader(
            {
                "runtime/tasks.en.jinja": "{% macro task_memory_record(record) %}CUSTOM {{ record.kind }}: {{ record.body }}{% endmacro %}",
            }
        )
    )
    monkeypatch.setattr(prompts, "_JINJA", environment)
    assert render_task_record("finish", body="Report") == "CUSTOM finish: Report"
    assert slots()[-1].content == "CUSTOM finish: Report"
    legacy = MemoryEvent(
        kind=MemoryKind.CONVERSATION_TURN,
        scope=MemoryScope.AGENT,
        address=SCOPE,
        timestamp=NOW,
        role="assistant",
        content="Old recap",
        metadata={
            "origin_task_id": "t",
            "tool_calls": [{"id": "old", "name": "control__finish_task", "input": {}}],
        },
    )
    assert project_task_history([legacy])[0].content == "CUSTOM recap: Old recap"


@pytest.mark.asyncio
async def test_closure_read_replace_and_replay_are_idempotent():
    memory = InMemoryMemoryProvider()
    for event in slots():
        await memory.ingest(event, CTX)
    target = await find_finish_target(memory, CTX, SCOPE, "t")
    assert target[1] == "closure" and not target[2]["preserve_raw"]
    for _ in range(2):
        await replace_finish_report(
            memory,
            CTX,
            SCOPE,
            "t",
            "closure",
            "Updated recap",
            "Updated report",
            "success",
            "Title",
            operation_id="replace-obs",
        )
    records = await load_records(memory, SCOPE, MemoryScope.AGENT, CTX)
    assert len(records) == 3
    assert sum("Delivered answer" in r.content for r in records) == 1
    assert sum("Updated report" in r.content for r in records) == 1
    assert await find_finish_target(memory, CTX, SCOPE, "t") is not None


@pytest.mark.asyncio
async def test_incomplete_new_closure_is_not_ready_for_replacement():
    memory = InMemoryMemoryProvider()
    for event in slots()[:-1]:
        await memory.ingest(event, CTX)
    assert await find_finish_target(memory, CTX, SCOPE, "t") is None


@pytest.mark.asyncio
async def test_sqlite_upgrade_partial_legacy_closure_and_reopen(tmp_path):
    from types import SimpleNamespace
    from ctx_weft.core.loop.steps.finalize import _synthesize_dispatch_pair
    from ctx_weft.providers.memory.sql.provider import open_sqlite_memory

    task = SimpleNamespace(id="t", parent_task_id=None, title="Title")
    async with open_sqlite_memory(tmp_path / "memory.db") as memory:
        # A crash before upgrade wrote only the first old closure slot.
        await memory.ingest(
            MemoryEvent(
                id="legacy-partial",
                kind=MemoryKind.CONVERSATION_TURN,
                scope=MemoryScope.AGENT,
                address=SCOPE,
                timestamp=NOW,
                role="assistant",
                content="Old partial recap",
                metadata={
                    "origin_task_id": "t",
                    "finish_policy": {"observe_id": "upgrade"},
                    "tool_calls": [
                        {"id": "legacy-call", "name": "control__finish_task", "input": {}}
                    ],
                },
            ),
            CTX,
        )
        for _ in range(2):
            await _synthesize_dispatch_pair(
                memory,
                SCOPE,
                task,
                "Actual recap",
                "Report",
                "success",
                CTX,
                final_reply="Delivered answer",
                operation_id="upgrade",
                completed_at=NOW.isoformat(),
            )
        before = await load_records(memory, SCOPE, MemoryScope.AGENT, CTX)
        assert len(before) == 3
        assert all(task_record_kind(r) in {"recap", "final_reply", "finish"} for r in before)
        assert all(not r.metadata.get("tool_calls") for r in before)

    async with open_sqlite_memory(tmp_path / "memory.db") as memory:
        after = await load_records(memory, SCOPE, MemoryScope.AGENT, CTX)
        assert [(r.id, r.content, r.timestamp) for r in after] == [
            (r.id, r.content, r.timestamp) for r in before
        ]
        assert project_task_history(after) == after
        assert await find_finish_target(memory, CTX, SCOPE, "t") is not None


@pytest.mark.asyncio
async def test_empty_success_closes_dispatch_without_publishing_a_fake_output():
    from ctx_weft.core.loop.steps.finalize import apply_task_close, ensure_dispatch_frame_at_start
    from tests.unit.test_final_reply_anchor import _task, _state, _loop_ctx, _sc

    memory = InMemoryMemoryProvider()
    task = _task(outputs=None)
    task.parent_task_id = "parent"
    state, ctx = _state(task, _sc(task.id)), _loop_ctx(memory)
    await ensure_dispatch_frame_at_start(state, ctx)
    await apply_task_close(
        state, task, ctx, outcome="success", act_recap="", task_summary="", has_llm_summary=False
    )
    records = await load_records(
        memory, MemoryAddress(session_id="s1", agent_id="ag1"), MemoryScope.AGENT, ctx.provider_ctx
    )
    finishes = [r for r in records if task_record_kind(r) == "finish"]
    results = [r for r in records if task_record_kind(r) == "result"]
    assert len(finishes) == 1 and 'outcome="success"' in finishes[0].content
    assert len(results) == 1 and "running" not in results[0].content
    assert "</task_finish>" in finishes[0].content
    publications = await load_records(
        memory,
        MemoryAddress(session_id="s1"),
        MemoryScope.SESSION,
        ctx.provider_ctx,
        kinds=[MemoryKind.PUBLICATION],
    )
    assert publications == []


@pytest.mark.parametrize("switch", [False, True])
def test_legacy_three_slot_recap_and_switch_closure_are_labeled(switch):
    from datetime import timedelta
    from ctx_weft.core.loop.task_memory_fold import task_record_group

    recap = MemoryEvent(
        id="legacy-recap",
        kind=MemoryKind.CONVERSATION_TURN,
        scope=MemoryScope.AGENT,
        address=SCOPE,
        timestamp=NOW,
        role="assistant",
        content="Original recap",
        metadata={"origin_task_id": "t"},
    )
    closing = replace(
        recap,
        id="legacy-closing",
        timestamp=NOW + timedelta(microseconds=1),
        content="Original closing report",
        metadata={"origin_task_id": "t"},
    )
    if switch:
        policy = {"observe_id": "switched", "outcome": "fail", "slot_count": 2}
        recap.metadata["finish_policy"] = policy
        closing.metadata["finish_policy"] = policy
        projected = project_task_history([recap, closing])
        assert [task_record_kind(r) for r in projected] == ["recap", "finish"]
        assert 'outcome="fail"' in projected[-1].content
    else:
        closing.metadata.update(
            final_reply=True,
            tool_calls=[{"id": "old-call", "name": "control__finish_task", "input": {}}],
        )
        projected = project_task_history([recap, closing])
        assert [task_record_kind(r) for r in projected] == ["recap", "final_reply"]
    assert task_record_group(projected[0]) == task_record_group(projected[1])
    assert projected[0].content.startswith("<task_recap")
    assert "Original recap" in projected[0].content
    assert recap.content == "Original recap"
