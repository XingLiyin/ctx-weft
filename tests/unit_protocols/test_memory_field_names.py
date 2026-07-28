"""字段终名（v2 §3 收尾）：MemoryEvent.address/.scope、MemoryRecord.scope。

- MemoryEvent：address = 归档坐标（MemoryAddress），scope = 归属范围（MemoryScope 枚举）
- MemoryRecord：layer → scope（address 字段已有）
- 误型 loud：address 传入枚举 / scope 传入坐标 → TypeError 带迁移提示（防旧关键字静默换义）
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ctx_weft.protocols import (
    MemoryAddress,
    MemoryEvent,
    MemoryKind,
    MemoryRecord,
    MemoryScope,
)

_ADDR = MemoryAddress(session_id="s1", task_id="t1", agent_id="a1")
_TS = datetime.now(timezone.utc)


def test_event_final_field_names() -> None:
    ev = MemoryEvent(kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK,
                     address=_ADDR, content="hi", timestamp=_TS, role="user")
    assert ev.address is _ADDR
    assert ev.scope is MemoryScope.TASK
    assert not hasattr(ev, "layer")


def test_event_mistyped_kwargs_are_loud() -> None:
    # 旧习惯 scope=<坐标>：静默换义是最危险的错——必须 TypeError 指路
    with pytest.raises(TypeError):
        MemoryEvent(kind=MemoryKind.CONVERSATION_TURN, scope=_ADDR,
                    content="x", timestamp=_TS, role="user")
    # address 传入枚举同理
    with pytest.raises(TypeError):
        MemoryEvent(kind=MemoryKind.SUMMARY, scope=MemoryScope.TASK,
                    address=MemoryScope.TASK, content="x", timestamp=_TS)


def test_record_final_field_names() -> None:
    rec = MemoryRecord(id="r1", type=None, content="c", timestamp=_TS,
                       kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK,
                       address=_ADDR, role="user")
    assert rec.scope is MemoryScope.TASK
    assert not hasattr(rec, "layer")
