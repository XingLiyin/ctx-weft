"""Summary decoration belongs to Jinja; history sources keep raw records."""

from datetime import UTC, datetime

from ctx_weft.core.assembler.sources._history import record_to_history_block
from ctx_weft.protocols import (
    MemoryAddress,
    MemoryEventType,
    MemoryKind,
    MemoryRecord,
    MemoryScope,
)
from tests.assembler_testkit import compile_prompt, prompt_request


def _record(*, event_type, kind, scope, role, task_id="t1"):
    return MemoryRecord(
        id="record-1",
        type=event_type,
        kind=kind,
        scope=scope,
        address=MemoryAddress(session_id="s1", task_id=task_id, agent_id="a1"),
        content="summary body",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        role=role,
        metadata={"task_id": task_id, "seq_no": 7},
    )


def test_history_source_returns_the_raw_summary_record():
    record = _record(
        event_type=MemoryEventType.TASK_COMPACT_SUMMARY,
        kind=MemoryKind.SUMMARY,
        scope=MemoryScope.TASK,
        role="assistant",
    )
    block = record_to_history_block(record, "agent_recall", 0)

    assert block.kind == "history"
    assert block.value is record
    assert block.metadata == {"sequence": 7}


def test_current_task_assistant_summary_is_decorated_by_jinja():
    record = _record(
        event_type=MemoryEventType.TASK_COMPACT_SUMMARY,
        kind=MemoryKind.SUMMARY,
        scope=MemoryScope.TASK,
        role="assistant",
    )
    prompt = compile_prompt(
        [record_to_history_block(record, "agent_recall", 0)],
        prompt_request(task_id="t1"),
    )
    text = "\n".join(str(message.content) for message in prompt.messages)

    assert "## Progress So Far" in text
    assert "summary body" in text
    assert "not your reply to the user" in text


def test_agent_summary_is_framed_as_prior_context_not_a_user_command():
    record = _record(
        event_type=MemoryEventType.AGENT_COMPACT_SUMMARY,
        kind=MemoryKind.SUMMARY,
        scope=MemoryScope.AGENT,
        role="assistant",
    )
    prompt = compile_prompt(
        [record_to_history_block(record, "agent_recall", 0)],
        prompt_request(task_id="t1"),
    )

    assert prompt.messages[0].role == "user"
    assert "compressed summary of earlier conversation" in str(prompt.messages[0].content)
    assert "summary body" in str(prompt.messages[0].content)
