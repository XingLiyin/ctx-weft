from datetime import datetime, UTC
from types import SimpleNamespace

from ctx_weft.core.assembler.sources._history import record_to_history_block
from ctx_weft.protocols import (
    ImagePart, MemoryAddress, MemoryKind, MemoryRecord, MemoryScope, TextPart,
)

_BASE = datetime(2026, 8, 1, tzinfo=UTC)


def _record(content):
    # type=None = v2 行形态；record_to_history_block 会经
    # legacy_type_of(kind, scope, role) 派生 etype。该字段无默认值，必须显式给出。
    return MemoryRecord(
        id="mem_1",
        type=None,
        kind=MemoryKind.CONVERSATION_TURN,
        scope=MemoryScope.TASK,
        address=MemoryAddress(session_id="s", task_id="t1", agent_id="a"),
        content=content,
        timestamp=_BASE,
        role="user",
        metadata={"task_id": "t1"},
    )


def _request():
    return SimpleNamespace(
        token_counter=len,
        task=SimpleNamespace(id="t1"),
    )


def test_plain_text_block_estimate_unchanged():
    """纯文本：估算值 == token_counter(text)，与改造前逐字节相同。"""
    blk = record_to_history_block(
        _record("hello"), "task_conversation", 0,
        request=_request(), current_task_id="t1",
    )
    assert blk.token_estimate == len("hello")


def test_image_part_counted_in_block_estimate():
    content = [TextPart(text="look"), ImagePart(data="ZGF0YQ==", media_type="image/png")]
    blk = record_to_history_block(
        _record(content), "task_conversation", 0,
        request=_request(), current_task_id="t1",
    )
    assert blk.token_estimate == len("look") + 1600


def test_stored_token_count_still_gets_image_supplement():
    """metadata 里存量的 token_count 是改造前写的（纯文本口径），仍须补图片。"""
    rec = _record([TextPart(text="look"), ImagePart(data="ZGF0YQ==", media_type="image/png")])
    rec.metadata["token_count"] = 7
    blk = record_to_history_block(
        rec, "task_conversation", 0, request=_request(), current_task_id="t1",
    )
    assert blk.token_estimate == 7 + 1600
