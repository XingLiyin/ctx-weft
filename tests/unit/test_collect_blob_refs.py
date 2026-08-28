"""blob_refs 字段 + collect_blob_refs（mark 判据）。

缺陷记录：docs/superpowers/specs/2026-08-27-l05-demotion-drops-blob-reference.md
"""

from __future__ import annotations

from datetime import datetime, timezone

from ctx_weft.protocols import (
    ImagePart, MemoryAddress, MemoryEvent, MemoryKind, MemoryRecord,
    MemoryScope, TextPart,
)


def _addr() -> MemoryAddress:
    return MemoryAddress(session_id="ses_1", agent_id="agt_1", task_id="tsk_1")


def _event(**kw) -> MemoryEvent:
    base = dict(
        kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK, address=_addr(),
        role="user", timestamp=datetime.now(timezone.utc), content="hi",
    )
    base.update(kw)
    return MemoryEvent(**base)


def test_event_blob_refs_defaults_empty() -> None:
    assert _event().blob_refs == []


def test_record_blob_refs_defaults_empty() -> None:
    rec = MemoryRecord(id="rec_1", type=None, content="hi", timestamp=datetime.now(timezone.utc))
    assert rec.blob_refs == []


def test_blob_refs_is_not_shared_between_instances() -> None:
    """default_factory 而非可变默认值——共享 list 会让一条记录的 ref 污染另一条。"""
    a, b = _event(), _event()
    a.blob_refs.append("blob:aaa")
    assert b.blob_refs == []
