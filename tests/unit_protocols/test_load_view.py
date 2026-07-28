"""load_view（v2 设计 §4 · P2c）：工作记忆回放——全量幸存、升序、半址过滤、双词汇视图。

同时锁定过渡期兼容：recall_recent 族 wrapper 用三元组匹配，v2 行可被旧 type 请求读到。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider
from ctx_weft.protocols import (
    MemoryAddress,
    MemoryEvent,
    MemoryEventType,
    MemoryScope,
    ProviderContext,
)
from ctx_weft.protocols.memory_compat import MemoryKind

_T0 = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)


def _ctx() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default")


def _legacy(type_: MemoryEventType, content: str, minute: int, *,
            task_id: str = "t1", agent_id: str = "a1", role: str | None = None) -> MemoryEvent:
    return MemoryEvent(
        type=type_, content=content, timestamp=_T0 + timedelta(minutes=minute),
        address=MemoryAddress(session_id="s1", task_id=task_id, agent_id=agent_id), role=role,
    )


def _v2(kind: MemoryKind, layer: MemoryScope, content: str, minute: int, *,
        task_id: str | None = "t1", agent_id: str | None = "a1",
        role: str | None = None) -> MemoryEvent:
    return MemoryEvent(
        kind=kind, scope=layer, content=content, timestamp=_T0 + timedelta(minutes=minute),
        address=MemoryAddress(session_id="s1", task_id=task_id, agent_id=agent_id), role=role,
    )


async def test_load_view_ascending_and_default_kinds() -> None:
    m = InMemoryMemoryProvider()
    await m.ingest(_legacy(MemoryEventType.USER_PROMPT, "up", 0, role="user"), _ctx())
    await m.ingest(_legacy(MemoryEventType.TOOL_INVOCATION, "audit", 1), _ctx())
    await m.ingest(_legacy(MemoryEventType.LLM_RESPONSE, "resp", 2, role="assistant"), _ctx())

    view = await m.load_view(MemoryAddress(session_id="s1", task_id="t1", agent_id="a1"),
                             MemoryScope.TASK, _ctx())
    # 默认 kinds = CONVERSATION_TURN + SUMMARY：audit 不在；升序（旧→新）
    assert [r.content for r in view] == ["up", "resp"]
    assert all(r.kind is MemoryKind.CONVERSATION_TURN for r in view)

    audit = await m.load_view(MemoryAddress(session_id="s1", task_id="t1", agent_id="a1"),
                              MemoryScope.TASK, _ctx(), kinds=[MemoryKind.TOOL_AUDIT])
    assert [r.content for r in audit] == ["audit"]


async def test_load_view_cross_task_by_agent() -> None:
    m = InMemoryMemoryProvider()
    await m.ingest(_legacy(MemoryEventType.USER_PROMPT, "one", 0, task_id="tA", role="user"), _ctx())
    await m.ingest(_legacy(MemoryEventType.USER_PROMPT, "two", 1, task_id="tB", role="user"), _ctx())

    view = await m.load_view(MemoryAddress(session_id="s1", agent_id="a1"),
                             MemoryScope.TASK, _ctx())
    assert [r.content for r in view] == ["one", "two"]
    assert [r.address.task_id for r in view] == ["tA", "tB"]  # 来源回显


async def test_load_view_half_address_validation() -> None:
    m = InMemoryMemoryProvider()
    with pytest.raises(ValueError):
        await m.load_view(MemoryAddress(session_id="s1"), MemoryScope.TASK, _ctx())
    with pytest.raises(ValueError):
        await m.load_view(MemoryAddress(session_id="s1", task_id="t1", agent_id="a1"),
                          MemoryScope.AGENT, _ctx())
    with pytest.raises(ValueError):
        await m.load_view(MemoryAddress(session_id="s1", agent_id="a1"),
                          MemoryScope.SESSION, _ctx())


async def test_load_view_sees_both_vocabularies() -> None:
    m = InMemoryMemoryProvider()
    await m.ingest(_legacy(MemoryEventType.USER_PROMPT, "old-vocab", 0, role="user"), _ctx())
    await m.ingest(_v2(MemoryKind.CONVERSATION_TURN, MemoryScope.TASK, "new-vocab", 1,
                       role="assistant"), _ctx())

    view = await m.load_view(MemoryAddress(session_id="s1", task_id="t1", agent_id="a1"),
                             MemoryScope.TASK, _ctx())
    assert [r.content for r in view] == ["old-vocab", "new-vocab"]
    assert all(r.kind is MemoryKind.CONVERSATION_TURN for r in view)  # kind 统一重打


async def test_load_view_excludes_superseded_and_dead_types() -> None:
    m = InMemoryMemoryProvider()
    rid = await m.ingest(_legacy(MemoryEventType.USER_PROMPT, "gone", 0, role="user"), _ctx())
    await m.ingest(MemoryEvent(  # 死类型 OBSERVER_SUMMARY：永不见于视图
        type=MemoryEventType.OBSERVER_SUMMARY, content="dead", timestamp=_T0,
        address=MemoryAddress(session_id="s1", task_id="t1", agent_id="a1"), role="assistant",
    ), _ctx())
    await m.supersede([rid], _ctx())

    view = await m.load_view(MemoryAddress(session_id="s1", task_id="t1", agent_id="a1"),
                             MemoryScope.TASK, _ctx())
    assert view == []


async def test_recall_recent_wrapper_matches_v2_rows() -> None:
    """过渡期桥接：v2 行可被旧 type 请求读到；newest-first + limit 语义不变。"""
    m = InMemoryMemoryProvider()
    await m.ingest(_legacy(MemoryEventType.USER_PROMPT, "l-up", 0, role="user"), _ctx())
    await m.ingest(_v2(MemoryKind.CONVERSATION_TURN, MemoryScope.TASK, "v-up", 1, role="user"), _ctx())
    await m.ingest(_v2(MemoryKind.CONVERSATION_TURN, MemoryScope.TASK, "v-resp", 2,
                       role="assistant"), _ctx())

    recs = await m.recall_recent(
        MemoryAddress(session_id="s1", task_id="t1", agent_id="a1"),
        [MemoryEventType.USER_PROMPT], 10, _ctx())
    assert [r.content for r in recs] == ["v-up", "l-up"]  # newest-first、role=assistant 不混入

    counted = await m.count_recent(
        MemoryAddress(session_id="s1", task_id="t1", agent_id="a1"),
        [MemoryEventType.USER_PROMPT, MemoryEventType.LLM_RESPONSE], _ctx())
    assert counted == 3
