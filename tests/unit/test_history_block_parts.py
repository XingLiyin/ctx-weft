from datetime import datetime, UTC
from types import SimpleNamespace

from ctx_weft.core.assembler.sources._history import record_to_history_block
from ctx_weft.protocols import (
    ImagePart, MemoryAddress, MemoryKind, MemoryRecord, MemoryScope, TextPart,
)

_BASE = datetime(2026, 8, 23, tzinfo=UTC)


def _record(content, kind=MemoryKind.CONVERSATION_TURN, role="user"):
    return MemoryRecord(
        id="mem_1", type=None, kind=kind, scope=MemoryScope.TASK,
        address=MemoryAddress(session_id="s", task_id="t1", agent_id="a"),
        content=content, timestamp=_BASE, role=role, metadata={"task_id": "t1"},
    )


def _request():
    return SimpleNamespace(token_counter=len, task=SimpleNamespace(id="t1"))


def _blk(rec):
    return record_to_history_block(rec, "task_conversation", 0,
                                   request=_request(), current_task_id="t1")


def test_plain_text_block_content_unchanged():
    assert _blk(_record("hello")).content == "hello"


def test_conversation_turn_keeps_parts():
    content = [TextPart(text="看这张图"), ImagePart(data="ZGF0YQ==", media_type="image/png")]
    blk = _blk(_record(content))
    assert blk.content == content, "非摘要记录必须原样保留 parts"


def test_summary_record_stays_text():
    """spec §8：SUMMARY 恒为纯文本，包装器仍走字符串路径。"""
    blk = _blk(_record("段落摘要", kind=MemoryKind.SUMMARY, role="assistant"))
    assert isinstance(blk.content, str)


def test_token_estimate_still_counts_images():
    """Phase 0 的图片补充项不得被本次改动破坏。"""
    content = [TextPart(text="look"), ImagePart(data="ZGF0YQ==", media_type="image/png")]
    assert _blk(_record(content)).token_estimate == len("look") + 1600
