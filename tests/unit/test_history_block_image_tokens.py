from datetime import UTC, datetime

from ctx_weft.core.assembler.sources._history import record_to_history_block
from ctx_weft.protocols import (
    ImagePart, MemoryAddress, MemoryKind, MemoryRecord, MemoryScope, TextPart,
)
from tests.assembler_testkit import compile_prompt, prompt_request

_BASE = datetime(2026, 8, 1, tzinfo=UTC)


def _record(content):
    return MemoryRecord(
        id="mem_1", type=None, kind=MemoryKind.CONVERSATION_TURN,
        scope=MemoryScope.TASK,
        address=MemoryAddress(session_id="s", task_id="t1", agent_id="a"),
        content=content, timestamp=_BASE, role="user", metadata={"task_id": "t1"},
    )


def test_history_block_does_not_precompute_prompt_tokens():
    record = _record("hello")
    block = record_to_history_block(record, "task_conversation", 0)

    assert block.value is record
    assert not hasattr(block, "token_estimate")


def test_builder_counts_image_parts_when_compiling():
    content = [TextPart(text="look"), ImagePart(data="ZGF0YQ==", media_type="image/png")]
    block = record_to_history_block(_record(content), "task_conversation", 0)
    prompt = compile_prompt([block], prompt_request(task_id="t1"))

    assert prompt.token_count >= len("look") + 1600


def test_stale_stored_token_count_does_not_override_builder_accounting():
    record = _record([
        TextPart(text="look"),
        ImagePart(data="ZGF0YQ==", media_type="image/png"),
    ])
    record.metadata["token_count"] = 7
    block = record_to_history_block(record, "task_conversation", 0)
    prompt = compile_prompt([block], prompt_request(task_id="t1"))

    assert prompt.token_count >= len("look") + 1600
