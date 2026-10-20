from datetime import UTC, datetime

from ctx_weft.core.assembler.sources._history import record_to_history_block
from ctx_weft.protocols import (
    ImagePart, MemoryAddress, MemoryKind, MemoryRecord, MemoryScope, TextPart,
)
from tests.assembler_testkit import compile_prompt, prompt_request

_BASE = datetime(2026, 8, 23, tzinfo=UTC)


def _record(content, kind=MemoryKind.CONVERSATION_TURN, role="user"):
    return MemoryRecord(
        id="mem_1", type=None, kind=kind, scope=MemoryScope.TASK,
        address=MemoryAddress(session_id="s", task_id="t1", agent_id="a"),
        content=content, timestamp=_BASE, role=role, metadata={"task_id": "t1"},
    )


def _block(record):
    return record_to_history_block(record, "task_conversation", 0)


def test_history_source_keeps_raw_record():
    record = _record("hello")
    block = _block(record)

    assert block.value is record
    assert block.kind == "history"


def test_prompt_builder_keeps_multimodal_parts():
    image = ImagePart(data="ZGF0YQ==", media_type="image/png")
    prompt = compile_prompt(
        [_block(_record([TextPart(text="看这张图"), image]))],
        prompt_request(task_id="t1"),
    )

    parts = prompt.messages[0].content
    assert isinstance(parts, list)
    assert any(isinstance(part, ImagePart) for part in parts)


def test_summary_decoration_happens_during_compile():
    block = _block(_record("段落摘要", kind=MemoryKind.SUMMARY, role="assistant"))
    assert block.value.content == "段落摘要"

    prompt = compile_prompt([block], prompt_request(task_id="t1"))
    text = "\n".join(str(message.content) for message in prompt.messages)
    assert "## Progress So Far" in text
    assert "段落摘要" in text
