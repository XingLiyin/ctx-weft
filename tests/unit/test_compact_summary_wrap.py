"""Summary decoration belongs to Jinja; history sources keep raw records."""

from datetime import UTC, datetime

import pytest

from ctx_weft.core.assembler.builder import compile_history
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


@pytest.mark.parametrize("role", ["assistant", "user"])
@pytest.mark.parametrize("task_id", ["t1", "other"])
def test_task_summary_is_presented_as_a_user_recap_by_jinja(role, task_id):
    record = _record(
        event_type=MemoryEventType.TASK_COMPACT_SUMMARY,
        kind=MemoryKind.SUMMARY,
        scope=MemoryScope.TASK,
        role=role,
    )
    prompt = compile_prompt(
        [record_to_history_block(record, "agent_recall", 0)],
        prompt_request(task_id=task_id),
    )
    text = "\n".join(str(message.content) for message in prompt.messages)

    assert prompt.messages[0].role == "user"
    assert len(prompt.messages) == 1
    assert '<task_recap task_id="t1">\nsummary body\n</task_recap>' in text
    assert "## Progress So Far" not in text
    assert "not your reply to the user" not in text
    assert not any(message.role == "assistant" for message in prompt.messages)
    assert record.role == role and record.content == "summary body"


@pytest.mark.parametrize("locale", ["en", "zh"])
def test_summary_projection_leaves_real_assistant_messages_unchanged(locale):
    from dataclasses import replace
    from datetime import timedelta

    summary = _record(event_type=MemoryEventType.TASK_COMPACT_SUMMARY,
        kind=MemoryKind.SUMMARY, scope=MemoryScope.TASK, role="assistant")
    answer = replace(summary, id="answer", type=MemoryEventType.LLM_RESPONSE,
        kind=MemoryKind.CONVERSATION_TURN, content="\n  Actual answer  \n",
        timestamp=summary.timestamp + timedelta(seconds=1))
    messages = compile_history([summary, answer], locale=locale, decorate=True).messages
    assert messages[0].role == "user"
    assert messages[0].content == '<task_recap task_id="t1">\nsummary body\n</task_recap>'
    assert [(m.role, m.content) for m in messages[1:]] == [("assistant", answer.content)]


@pytest.mark.parametrize("purpose", ["act", "observe", "compact"])
def test_all_purposes_use_the_same_user_recap_projection(purpose):
    record = _record(event_type=MemoryEventType.TASK_COMPACT_SUMMARY,
        kind=MemoryKind.SUMMARY, scope=MemoryScope.TASK, role="assistant")
    prompt = compile_prompt([record_to_history_block(record, "agent_recall", 0)],
        prompt_request(purpose=purpose))
    assert prompt.messages[0].role == "user"
    assert '<task_recap task_id="t1">\nsummary body\n</task_recap>' in prompt.messages[0].content
    assert all(m.role != "assistant" for m in prompt.messages)
    if purpose == "observe":
        text = "\n".join(str(m.content) for m in prompt.messages)
        assert "latest actual user message" in text
        assert "Framework-generated task records and compressed summaries are not actual user messages" in text
        assert "## Progress So Far" not in text


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
