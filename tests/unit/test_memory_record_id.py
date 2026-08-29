"""ingest 的调用方预生成 record id 契约（v2 设计 §4 · 2026-07-27 增补）。

覆盖：
1. event.id 给定时 provider 采用并原样回显，recall 返回同 id。
2. 按 id 幂等：同 id 重复 ingest = no-op（不重复写入、不推进计数器）。
3. event.id=None 时 provider 自行生成（v1 行为不变）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from ctx_weft.protocols import (
    MemoryEvent,
    MemoryEventType,
    MemoryAddress,
    ProviderContext,
)


def _ctx() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default")


def _event(content: str, id: str | None = None) -> MemoryEvent:
    return MemoryEvent(
        type=MemoryEventType.USER_PROMPT,
        address=MemoryAddress(session_id="s1", task_id="t1", agent_id="a1"),
        content=content,
        timestamp=datetime.now(timezone.utc),
        id=id,
        role="user",
    )


async def test_ingest_adopts_caller_supplied_id() -> None:
    provider = InMemoryMemoryProvider()
    returned = await provider.ingest(_event("hello", id="mem_pre_001"), _ctx())
    assert returned == "mem_pre_001"

    records = await provider.recall_recent(
        MemoryAddress(session_id="s1", task_id="t1", agent_id="a1"),
        [MemoryEventType.USER_PROMPT], 10, _ctx(),
    )
    assert [r.id for r in records] == ["mem_pre_001"]


async def test_ingest_same_id_twice_is_noop() -> None:
    provider = InMemoryMemoryProvider()
    ctx = _ctx()
    first = await provider.ingest(_event("hello", id="mem_pre_001"), ctx)
    second = await provider.ingest(_event("changed content", id="mem_pre_001"), ctx)
    assert first == second == "mem_pre_001"

    records = await provider.recall_recent(
        MemoryAddress(session_id="s1", task_id="t1", agent_id="a1"),
        [MemoryEventType.USER_PROMPT], 10, _ctx(),
    )
    # 只有一条：重复 ingest 不写入、不比对内容（id 即身份，原内容保留）
    assert len(records) == 1
    assert records[0].content == "hello"
    # no-op 不推进 seq 计数器：下一条正常写入的 seq_no 紧随第一条
    third = await provider.ingest(_event("next"), ctx)
    assert third != "mem_pre_001"
    records = await provider.recall_recent(
        MemoryAddress(session_id="s1", task_id="t1", agent_id="a1"),
        [MemoryEventType.USER_PROMPT], 10, _ctx(),
    )
    seqs = sorted(r.metadata["seq_no"] for r in records)
    assert seqs == [1, 2]


async def test_ingest_superseded_id_stays_noop() -> None:
    """身份包含已 superseded 的记录：折叠后同 id 重放不得复活或重写。"""
    provider = InMemoryMemoryProvider()
    ctx = _ctx()
    await provider.ingest(_event("hello", id="mem_pre_001"), ctx)
    await provider.supersede(["mem_pre_001"], ctx)

    returned = await provider.ingest(_event("hello", id="mem_pre_001"), ctx)
    assert returned == "mem_pre_001"
    records = await provider.recall_recent(
        MemoryAddress(session_id="s1", task_id="t1", agent_id="a1"),
        [MemoryEventType.USER_PROMPT], 10, _ctx(),
    )
    assert records == []  # 仍是 superseded，未复活


async def test_ingest_without_id_generates_unique_ids() -> None:
    provider = InMemoryMemoryProvider()
    ctx = _ctx()
    id1 = await provider.ingest(_event("one"), ctx)
    id2 = await provider.ingest(_event("two"), ctx)
    assert id1 and id2 and id1 != id2
