"""MemoryEvent/MemoryRecord 过渡形态（v2 设计 §3 · P2b）。

- v2-native 构造：kind+layer 显式、type=None；须满足 §4 ingest 全址不变量
  （TASK → task_id+agent_id；AGENT → agent_id；SESSION → 仅 session_id）
- legacy 构造：type 给定，行为逐字节不变（不做 kind 补全、不加全址强制）
- 基础校验：type|kind 至少一、scope/content/timestamp 必给
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ctx_weft.protocols import (
    MemoryAddress,
    MemoryEvent,
    MemoryEventType,
    MemoryScope,
    MemoryRecord,
)
from ctx_weft.protocols.memory_compat import MemoryKind

_FULL = MemoryAddress(session_id="s1", task_id="t1", agent_id="a1")
_TS = datetime.now(timezone.utc)


def test_v2_native_event_needs_no_type() -> None:
    ev = MemoryEvent(kind=MemoryKind.CONVERSATION_TURN, layer=MemoryScope.TASK,
                     scope=_FULL, content="hi", timestamp=_TS, role="user")
    assert ev.type is None
    assert ev.kind is MemoryKind.CONVERSATION_TURN
    assert ev.layer is MemoryScope.TASK


def test_event_requires_type_or_kind() -> None:
    with pytest.raises(ValueError):
        MemoryEvent(scope=_FULL, content="x", timestamp=_TS)


def test_event_requires_scope_content_timestamp() -> None:
    with pytest.raises(ValueError):
        MemoryEvent(type=MemoryEventType.USER_PROMPT, content="x", timestamp=_TS)
    with pytest.raises(ValueError):
        MemoryEvent(type=MemoryEventType.USER_PROMPT, scope=_FULL, timestamp=_TS)
    with pytest.raises(ValueError):
        MemoryEvent(type=MemoryEventType.USER_PROMPT, scope=_FULL, content="x")


def test_legacy_event_unchanged() -> None:
    """legacy 构造不做 kind 补全（写入保真，归一化在读侧）、不加全址强制。"""
    ev = MemoryEvent(type=MemoryEventType.USER_PROMPT,
                     scope=MemoryAddress(session_id="s1", task_id="t1"),  # 无 agent_id，v1 合法
                     content="x", timestamp=_TS)
    assert ev.kind is None and ev.layer is None


def test_v2_native_full_address_invariant() -> None:
    # TASK 层缺 agent_id → ValueError
    with pytest.raises(ValueError):
        MemoryEvent(kind=MemoryKind.CONVERSATION_TURN, layer=MemoryScope.TASK,
                    scope=MemoryAddress(session_id="s1", task_id="t1"),
                    content="x", timestamp=_TS, role="user")
    # AGENT 层缺 agent_id → ValueError
    with pytest.raises(ValueError):
        MemoryEvent(kind=MemoryKind.SUMMARY, layer=MemoryScope.AGENT,
                    scope=MemoryAddress(session_id="s1"),
                    content="x", timestamp=_TS)
    # SESSION 层仅 session_id 合法
    ev = MemoryEvent(kind=MemoryKind.PUBLICATION, layer=MemoryScope.SESSION,
                     scope=MemoryAddress(session_id="s1"),
                     content="x", timestamp=_TS, topic="tp")
    assert ev.layer is MemoryScope.SESSION


def test_v2_native_requires_layer() -> None:
    """kind 给定而 layer 缺失 → ValueError（v2 词汇必须显式归属）。"""
    with pytest.raises(ValueError):
        MemoryEvent(kind=MemoryKind.SUMMARY, scope=_FULL, content="x", timestamp=_TS)


def test_memory_record_transitional_fields() -> None:
    rec = MemoryRecord(id="r1", type=None, content="c", timestamp=_TS,
                       kind=MemoryKind.CONVERSATION_TURN, layer=MemoryScope.TASK,
                       address=_FULL, role="user")
    assert rec.kind is MemoryKind.CONVERSATION_TURN
    assert rec.address is _FULL
    # legacy 构造不带新字段仍合法
    old = MemoryRecord(id="r2", type=MemoryEventType.USER_PROMPT, content="c", timestamp=_TS)
    assert old.kind is None and old.layer is None and old.address is None
