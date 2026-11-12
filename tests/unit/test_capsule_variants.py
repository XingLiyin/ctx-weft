"""A closed task has one full view or one minimal view, including after recovery."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ctx_weft.core.assembler.builder import PromptBuilder
from ctx_weft.core.assembler.sources._history import record_to_history_block
from ctx_weft.core.assembler.sources.agent_recall import _staged_record
from tests.task_memory_testkit import history_view
from ctx_weft.core.loop.task_memory_fold import task_record_kind
from tests.compaction_testkit import demote_kept_capsules
from ctx_weft.core.loop.steps.finalize import _synthesize_dispatch_pair
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
from ctx_weft.providers.memory.sql.provider import open_sqlite_memory
from tests.assembler_testkit import compile_prompt, prompt_request
from tests.model_testkit import model_snapshot


NOW = datetime(2026, 11, 1, tzinfo=UTC)
SCOPE = MemoryAddress(session_id="s", agent_id="a")
CTX = ProviderContext(session_id="s", agent_id="a")


def body(task_id="t", text="Delivered answer", *, seconds=0):
    scope = MemoryAddress(session_id="s", agent_id="a", task_id=task_id)
    return [
        MemoryEvent(
            id=f"{task_id}-input",
            kind=MemoryKind.CONVERSATION_TURN,
            scope=MemoryScope.TASK,
            address=scope,
            timestamp=NOW + timedelta(seconds=seconds),
            role="user",
            content="Original goal",
            metadata={"task_id": task_id},
        ),
        MemoryEvent(
            id=f"{task_id}-reply",
            kind=MemoryKind.CONVERSATION_TURN,
            scope=MemoryScope.TASK,
            address=scope,
            timestamp=NOW + timedelta(seconds=seconds + 1),
            role="assistant",
            content=text,
            metadata={"task_id": task_id, "tool_call_count": 0},
        ),
    ]


def closure(task_id="t", *, reply="Delivered answer", parent=None, seconds=2):
    events = build_finish_slots(
        scope=SCOPE,
        task_id=task_id,
        parent_task_id=parent,
        title="Original goal",
        outcome="success",
        act_recap="Actual work",
        task_summary="Goal achieved; essential result",
        final_reply=reply,
        base=NOW + timedelta(seconds=seconds),
        tool_call_id=f"close-{task_id}",
    )
    policy = {
        "outcome": "success",
        "slot_count": len(events),
        "preserve_raw": not reply,
        "final_reply_record_id": f"{task_id}-reply",
    }
    return [
        replace(e, id=f"{task_id}-close-{i}", metadata={**e.metadata, "finish_policy": policy})
        for i, e in enumerate(events)
    ]


def blocks(records):
    return [
        record_to_history_block(_staged_record(r), source="agent_recall", idx=i)
        for i, r in enumerate(records)
    ]


@pytest.mark.parametrize("retained_raw", [True, False])
def test_full_capsule_has_one_answer_and_one_finish(retained_raw):
    raw = body()
    records = raw + closure(reply="" if retained_raw else "Delivered answer")
    visible, capsules = history_view(records)
    assert len(capsules) == 1
    assert sum(r.role == "assistant" and r.content == "Delivered answer" for r in visible) == 1
    assert all("<task_final_reply" not in str(r.content) for r in visible)
    assert sum(task_record_kind(r) == "finish" for r in visible) == 1
    assert visible[-1].role == "user"
    assert visible[-1].content == '<task_finish task_id="t" outcome="success" />'
    assert "Goal achieved; essential result" not in visible[-1].content
    assert "Goal achieved; essential result" in capsules[0].lean[0].content
    assert "Goal achieved; essential result" in records[-1].content
    assert sum("Delivered answer" in r.content for r in visible) == 1
    assert raw[-1].content == "Delivered answer"  # read projection is immutable
    assert len(capsules[0].lean) == 1 and "<task_finish" in capsules[0].lean[0].content


def test_minimal_capsule_has_only_the_finish_even_with_old_stray_recap():
    records = closure(reply="")
    visible, capsules = history_view(records)
    assert [task_record_kind(r) for r in visible] == ["finish"]
    assert capsules == []


@pytest.mark.parametrize("locale", ["en", "zh"])
def test_finish_marker_keeps_child_identity_without_exposing_the_report(locale):
    from ctx_weft.core.assembler.builder import compile_history

    records = body() + closure(parent="parent")
    messages = compile_history(records, locale=locale).messages
    (finish,) = [m for m in messages if "<task_finish" in str(m.content)]
    assert finish.role == "user"
    assert finish.content == '<task_finish task_id="t" parent_task_id="parent" outcome="success" />'
    assert "Goal achieved; essential result" in records[-1].content


def test_partial_closure_does_not_hide_its_last_surviving_record():
    records = closure()
    visible, capsules = history_view([records[-1]])
    assert visible == [records[-1]]
    assert capsules == []


def test_legacy_raw_answer_is_labeled_without_converting_business_tools():
    raw = body()
    old = MemoryEvent(
        id="old-call",
        kind=MemoryKind.CONVERSATION_TURN,
        scope=MemoryScope.AGENT,
        address=SCOPE,
        timestamp=NOW + timedelta(seconds=2),
        role="assistant",
        content="Real recap",
        metadata={
            "origin_task_id": "t",
            "tool_calls": [{"id": "old", "name": "control__finish_task", "input": {}}],
        },
    )
    report = replace(
        old,
        id="old-result",
        role="tool",
        content="Whole task report",
        metadata={"origin_task_id": "t", "tool_call_id": "old"},
    )
    tool = replace(
        raw[-1],
        id="business",
        content="Executing",
        timestamp=NOW,
        metadata={"task_id": "t", "tool_calls": [{"id": "real", "name": "fs__read", "input": {}}]},
    )
    visible, _ = history_view([*raw, tool, old, report])
    assert any(r.id == "t-reply" and r.content == "Delivered answer" for r in visible)
    assert (
        next(r for r in visible if r.id == "business").metadata["tool_calls"][0]["name"]
        == "fs__read"
    )
    assert sum(task_record_kind(r) == "finish" for r in visible) == 1


def test_budget_replaces_the_entire_capsule_and_preserves_the_new_request():
    raw = body(text="Delivered answer " * 500)
    records = raw + closure(reply="")
    active = body("next", seconds=10)[0]
    active.content = "New request"
    records.append(active)
    request = prompt_request(task_id="next")
    full = compile_prompt(blocks(records), request)
    base = compile_prompt(blocks([active]), request).token_count
    budget = base + 600
    request.resolved_model = model_snapshot(budget, 0)
    lean = compile_prompt(blocks(records), request)
    full_text = "\n".join(str(m.content) for m in full.messages)
    lean_text = "\n".join(str(m.content) for m in lean.messages)
    assert "Delivered answer" in full_text and full_text.count("<task_finish") == 1
    assert "Goal achieved; essential result" not in full_text
    assert "<task_finish" in lean_text
    assert "<task_recap" not in lean_text and "<task_final_reply" not in lean_text
    assert "Delivered answer" not in lean_text
    assert lean_text.count("Goal achieved; essential result") == 1
    assert "New request" in lean_text and lean.token_count <= budget
    assert lean_text.index("<task_finish") < lean_text.index("New request")


def test_required_capsule_is_not_silently_demoted_by_the_budget():
    builder = PromptBuilder(len)
    from ctx_weft.core.models.errors import ContextOverflowError
    from ctx_weft.protocols import LLMMessage

    builder.message(
        LLMMessage(role="assistant", content="Full answer" * 50),
        priority=0,
        required=True,
        group="capsule",
    )
    builder.alternative("capsule", caller=lambda: (builder.message(
        LLMMessage(role="user", content="Lean"), priority=1, group="capsule"), "")[1])
    with pytest.raises(ContextOverflowError):
        builder.build(20)


def test_even_a_minimal_capsule_can_be_dropped_without_stranding_its_details():
    active = body("next", seconds=10)[0]
    records = body(text="Long answer " * 500) + closure(reply="") + [active]
    request = prompt_request(task_id="next")
    budget = compile_prompt(blocks([active]), request).token_count + 10
    request.resolved_model = model_snapshot(budget, 0)
    prompt = compile_prompt(blocks(records), request)
    text = "\n".join(str(m.content) for m in prompt.messages)
    assert all(tag not in text for tag in ("<task_finish", "<task_recap", "<task_final_reply"))
    assert "Long answer" not in text and prompt.token_count <= budget
    assert "Original goal" in text  # the current task's original input is pinned


def test_capsule_selection_does_not_depend_on_the_jinja_label_spelling(monkeypatch):
    from jinja2 import ChoiceLoader, DictLoader
    from ctx_weft.core.assembler import prompts

    environment = prompts._JINJA.overlay(
        loader=ChoiceLoader(
            [
                DictLoader(
                    {
                        "runtime/tasks.en.jinja": "{% macro task_memory_record(record) %}"
                        "CUSTOM {{ record.kind }}: {{ record.body }}{% endmacro %}"
                    }
                ),
                prompts._JINJA.loader,
            ]
        )
    )
    monkeypatch.setattr(prompts, "_JINJA", environment)
    visible, capsules = history_view(body() + closure(reply=""))
    assert any(r.content == "CUSTOM final_reply: Delivered answer" for r in visible)
    assert sum("CUSTOM finish" in r.content for r in visible) == 1
    assert capsules[0].lean[0].content == "CUSTOM finish: Goal achieved; essential result"


def test_canceled_partial_tool_output_is_not_invented_as_a_final_answer():
    records = body(text="Half finished")
    records[-1].metadata["interrupted"] = True
    closed = closure(reply="")
    for record in closed:
        record.metadata["finish_policy"]["outcome"] = "cancelled"
    visible, _ = history_view(records + closed)
    assert all(task_record_kind(r) != "final_reply" for r in visible)
    assert any(r.content == "Half finished" for r in visible)


@pytest.mark.asyncio
async def test_failed_observation_cannot_demote_uncovered_raw_history():
    memory = InMemoryMemoryProvider()
    closed = closure(reply="")
    for record in closed:
        record.metadata["finish_policy"]["has_summary"] = False
    records = body() + closed
    visible, capsules = history_view(records)
    assert all(task_record_kind(r) != "recap" for r in visible)
    assert any(r.role == "assistant" and r.content == "Delivered answer" for r in visible)
    assert capsules == []  # The template does not register an unusable alternative.
    for record in records:
        await memory.ingest(record, CTX)
    assert (
        await demote_kept_capsules(
            SimpleNamespace(scope=SCOPE), SimpleNamespace(memory=memory, provider_ctx=CTX), {"t"}
        )
        == 0
    )
    assert len(await load_records(memory, SCOPE, MemoryScope.TASK, CTX)) == 2

    # A later successful observation restores a usable minimal representation.
    scope, group, _ = await find_finish_target(memory, CTX, SCOPE, "t")
    await replace_finish_report(
        memory,
        CTX,
        scope,
        "t",
        group,
        "Recovered recap",
        "Goal achieved; essential result",
        "success",
        "Original goal",
    )
    assert (
        await demote_kept_capsules(
            SimpleNamespace(scope=SCOPE), SimpleNamespace(memory=memory, provider_ctx=CTX), {"t"}
        )
        > 0
    )


@pytest.mark.asyncio
async def test_demotion_also_archives_the_tasks_inherited_snapshot():
    memory = InMemoryMemoryProvider()
    inherited = MemoryEvent(
        id="inherited",
        kind=MemoryKind.CONVERSATION_TURN,
        scope=MemoryScope.AGENT,
        address=MemoryAddress(session_id="s", agent_id="a", task_id="t"),
        content="Inherited context",
        role="user",
        timestamp=NOW,
        metadata={"inherited_from_agent_id": "parent-agent"},
    )
    for record in [*body(), *closure(), inherited]:
        await memory.ingest(record, CTX)
    assert (
        await demote_kept_capsules(
            SimpleNamespace(scope=SCOPE), SimpleNamespace(memory=memory, provider_ctx=CTX), {"t"}
        )
        > 0
    )
    records = await load_records(memory, SCOPE, MemoryScope.AGENT, CTX)
    assert len(records) == 1 and task_record_kind(records[0]) == "finish"


@pytest.mark.asyncio
@pytest.mark.parametrize("lean", [False, True])
async def test_inherited_snapshot_matches_the_parents_selected_capsule_view(lean):
    from ctx_weft.core.runtime import _copy_memory_for_inherit

    memory = InMemoryMemoryProvider()
    for record in [*body(), *closure()]:
        await memory.ingest(record, CTX)
    if lean:
        await demote_kept_capsules(
            SimpleNamespace(scope=SCOPE), SimpleNamespace(memory=memory, provider_ctx=CTX), {"t"}
        )
    await _copy_memory_for_inherit(
        source_agent_id="a",
        child_task=SimpleNamespace(id="child"),
        sub_agent=SimpleNamespace(id="b"),
        memory=memory,
        session_id="s",
        tenant_id="default",
    )
    inherited = await load_records(
        memory, MemoryAddress(session_id="s", agent_id="b"), MemoryScope.AGENT, CTX
    )
    text = "\n".join(r.content for r in inherited)
    assert text.count("<task_finish") == 1
    assert "<task_final_reply" not in text
    assert text.count("Delivered answer") == (0 if lean else 1)


@pytest.mark.asyncio
async def test_demotion_is_atomic_idempotent_and_includes_the_child_subtree():
    memory = InMemoryMemoryProvider()
    records = (
        body() + closure() + body("child", seconds=3) + closure("child", parent="t", seconds=5)
    )
    active = body("next", seconds=10)[0]
    for r in [*records, active]:
        await memory.ingest(r, CTX)
    state, ctx = SimpleNamespace(scope=SCOPE), SimpleNamespace(memory=memory, provider_ctx=CTX)
    assert await demote_kept_capsules(state, ctx, {"t"}) > 0
    assert await demote_kept_capsules(state, ctx, {"t"}) == 0
    turns = await load_records(memory, SCOPE, MemoryScope.AGENT, CTX)
    assert len(turns) == 1 and task_record_kind(turns[0]) == "finish"
    assert turns[0].metadata["origin_task_id"] == "t"
    raw = await load_records(memory, SCOPE, MemoryScope.TASK, CTX)
    assert [r.id for r in raw] == [active.id]
    scope, group, policy = await find_finish_target(memory, CTX, SCOPE, "t")
    assert policy["capsule_state"] == "lean" and policy["slot_count"] == 1
    await replace_finish_report(
        memory,
        CTX,
        scope,
        "t",
        group,
        "Late recap",
        "Updated essential result",
        "success",
        "Original goal",
        final_reply="Must not resurrect a reply",
        operation_id="late",
    )
    turns = await load_records(memory, SCOPE, MemoryScope.AGENT, CTX)
    assert len(turns) == 1 and "Updated essential result" in turns[0].content
    assert "Must not resurrect" not in turns[0].content


@pytest.mark.asyncio
async def test_raw_answer_reference_and_lean_state_survive_sqlite_reopen(tmp_path):
    database = tmp_path / "memory.db"
    task = SimpleNamespace(
        id="t",
        title="Original goal",
        parent_task_id=None,
        assigned_agent_id="a",
        outputs="Delivered answer",
    )
    async with open_sqlite_memory(database) as memory:
        for r in body():
            await memory.ingest(r, CTX)
        await _synthesize_dispatch_pair(
            memory,
            SCOPE,
            task,
            "Actual work",
            "Essential result",
            "success",
            CTX,
            preserve_raw=True,
            operation_id="obs",
            completed_at=NOW.isoformat(),
        )
    async with open_sqlite_memory(database) as memory:
        agent = await load_records(memory, SCOPE, MemoryScope.AGENT, CTX)
        raw = await load_records(memory, SCOPE, MemoryScope.TASK, CTX)
        assert agent[-1].metadata["finish_policy"]["final_reply_record_id"] == "t-reply"
        visible, _ = history_view([*agent, *raw])
        assert sum(r.role == "assistant" and r.content == "Delivered answer" for r in visible) == 1
        assert sum(task_record_kind(r) == "finish" for r in visible) == 1
        await demote_kept_capsules(
            SimpleNamespace(scope=SCOPE), SimpleNamespace(memory=memory, provider_ctx=CTX), {"t"}
        )
        before = await load_records(memory, SCOPE, MemoryScope.AGENT, CTX)
    async with open_sqlite_memory(database) as memory:
        after = await load_records(memory, SCOPE, MemoryScope.AGENT, CTX)
        assert [(r.id, r.content) for r in before] == [(r.id, r.content) for r in after]
        assert [task_record_kind(r) for r in history_view(after)[0]] == ["finish"]
