"""Organize contract, exercised against in-memory and SQLite backends."""

from datetime import UTC, datetime, timedelta

import pytest

from ctx_weft.protocols import (
    MemoryAddress,
    MemoryEvent,
    MemoryKind,
    MemoryScope,
    ProviderContext,
    load_records,
)
from tests.unit.test_memory_conformance import memory as memory

CTX = ProviderContext(session_id="org", agent_id="a", task_id="current")
ADDR = MemoryAddress(session_id="org", agent_id="a", task_id="current")
BASE = datetime(2026, 1, 1, tzinfo=UTC)


async def put(memory, id, role, text, n, *, kind=MemoryKind.CONVERSATION_TURN, metadata=None):
    return await memory.ingest(
        MemoryEvent(
            id=id,
            address=ADDR,
            scope=MemoryScope.TASK,
            kind=kind,
            role=role,
            content=text,
            timestamp=BASE + timedelta(seconds=n),
            metadata=metadata or {},
        ),
        CTX,
    )


async def view(memory):
    return await load_records(
        memory,
        ADDR,
        MemoryScope.TASK,
        CTX,
        kinds=[MemoryKind.CONVERSATION_TURN, MemoryKind.SUMMARY],
    )


async def test_segment_retains_users_prior_summary_and_later_input(memory):
    await put(memory, "u1", "user", "first", 0)
    await put(memory, "a1", "assistant", "first answer", 1)
    await put(memory, "u2", "user", "second", 2)
    await put(memory, "prior", "assistant", "prior digest", 3, kind=MemoryKind.SUMMARY)
    await put(memory, "a2", "assistant", "second answer", 4)
    inputs = [r.id for r in await view(memory)]
    await put(memory, "u3", "user", "late user", 5)
    result = await memory.organize(
        CTX, operation="fold_segment", record_ids=inputs, summary="SEGMENT"
    )
    assert result.superseded_ids == ("a2",)
    assert [r.content for r in await view(memory)] == [
        "first",
        "first answer",
        "second",
        "prior digest",
        "SEGMENT",
        "late user",
    ]
    repeated = await memory.organize(
        CTX, operation="fold_segment", record_ids=inputs, summary="SEGMENT"
    )
    assert not repeated.changed
    assert len([r for r in await view(memory) if r.content == "SEGMENT"]) == 1


async def test_current_material_excludes_retained_window_and_keeps_original(memory):
    for i, role in enumerate(["user", "assistant", "user", "assistant", "user", "assistant"]):
        await put(memory, f"r{i}", role, f"text-{i}", i)
    material = await memory.recall_for_organize(CTX, operation="fold_current", keep_last=2)
    assert [r.id for r in material] == ["r0", "r1", "r2", "r3"]
    result = await memory.organize(
        CTX, operation="fold_current", record_ids=[r.id for r in material], summary="PREFIX"
    )
    assert result.changed
    rows = await view(memory)
    assert "text-0" in rows[0].content and "PREFIX" in rows[0].content
    assert [r.content for r in rows[1:]] == ["text-4", "text-5"]
    assert not any(r.content == "text-2" for r in rows)


async def test_unfinished_tool_round_is_protected_without_external_ids(memory):
    await put(memory, "u", "user", "request", 0)
    await put(memory, "done", "assistant", "done", 1)
    await put(
        memory,
        "pending",
        "assistant",
        "calling",
        2,
        metadata={"tool_calls": [{"id": "call", "name": "tool"}]},
    )
    material = await memory.recall_for_organize(CTX, operation="fold_current", keep_last=0)
    assert [r.id for r in material] == ["u", "done"]
    result = await memory.organize(
        CTX, operation="fold_segment", record_ids=["u", "done", "pending"], summary="unsafe"
    )
    assert result.status == "deferred"
    assert [r.id for r in await view(memory)] == ["u", "done", "pending"]


async def test_obsolete_current_material_does_not_append_stale_summary(memory):
    await put(memory, "u", "user", "request", 0)
    await put(memory, "a", "assistant", "answer", 1)
    material = await memory.recall_for_organize(CTX, operation="fold_current")
    await memory.fold(["a"], [], CTX)
    result = await memory.organize(
        CTX, operation="fold_current", record_ids=[r.id for r in material], summary="STALE"
    )
    assert result.status == "deferred" and not result.changed
    assert [r.content for r in await view(memory)] == ["request"]


async def test_conditional_commit_rolls_back_partial_source_change(memory):
    await put(memory, "one", "assistant", "one", 0)
    await put(memory, "two", "assistant", "two", 1)
    await memory.fold(["one"], [], CTX)
    replacement = MemoryEvent(
        id="wrong",
        address=ADDR,
        scope=MemoryScope.TASK,
        kind=MemoryKind.SUMMARY,
        role="assistant",
        content="wrong",
        timestamp=BASE,
    )
    assert await memory._commit_organize(["one", "two"], [replacement], CTX) is None
    assert [r.id for r in await view(memory)] == ["two"]


async def test_invalid_range_and_missing_summary_do_not_write(memory):
    await put(memory, "a", "assistant", "answer", 0)
    with pytest.raises(ValueError):
        await memory.organize(CTX, operation="fold_segment", record_ids=["a"], summary=" ")
    with pytest.raises(ValueError):
        await memory.organize(CTX, operation="fold_current", record_ids=["missing"], summary="x")
    assert [r.id for r in await view(memory)] == ["a"]


async def test_image_operation_keeps_recent_images_and_preserves_refs(memory):
    from ctx_weft.core.utils.content import collect_blob_refs
    from ctx_weft.protocols import ImagePart

    old_ref, recent_ref = "blob:" + "a" * 64, "blob:" + "b" * 64
    await put(
        memory,
        "old-image",
        "user",
        [ImagePart(data=old_ref, source_type="ref", media_type="image/png")],
        0,
    )
    await put(
        memory,
        "recent-image",
        "user",
        [ImagePart(data=recent_ref, source_type="ref", media_type="image/png")],
        1,
    )
    result = await memory.organize(CTX, operation="demote_images", keep_recent_images=1)
    assert result.demoted_images == 1 and result.superseded_ids == ("old-image",)
    assert result.replacements[0].id != "old-image"
    assert old_ref in collect_blob_refs(result.replacements[0])
    rows = await view(memory)
    assert any(r.id == "recent-image" for r in rows)
    assert len(rows) == 2


async def test_recall_returns_demoted_source_ids_before_summary(memory):
    from ctx_weft.protocols import ImagePart

    ref = "blob:" + "c" * 64
    await put(
        memory,
        "image-user",
        "user",
        [ImagePart(data=ref, source_type="ref", media_type="image/png")],
        0,
    )
    await put(memory, "old-answer", "assistant", "answer", 1)
    await put(memory, "new-user", "user", "RETAINED_INPUT", 2)
    await put(memory, "new-answer", "assistant", "RETAINED_ANSWER", 3)
    rows = await memory.recall_for_organize(CTX, operation="fold_current", keep_last=2)
    assert "image-user" in {r.id for r in rows}  # recall does not write
    demoted = await memory.organize(
        CTX, operation="demote_images", record_ids=[r.id for r in rows], keep_recent_images=0
    )
    rows = [r for r in rows if r.id not in demoted.superseded_ids] + list(demoted.replacements)
    rows.sort(key=lambda r: r.timestamp)
    assert "image-user" not in {r.id for r in rows}
    assert ref in str(rows[0].content)
    assert all("RETAINED" not in str(r.content) for r in rows)
    result = await memory.organize(
        CTX, operation="fold_current", record_ids=[r.id for r in rows], summary="digest"
    )
    assert result.changed
    active = await view(memory)
    assert len(active) == 3 and ref in active[0].content
    assert active[0].blob_refs == [ref]


async def test_concurrent_same_source_is_applied_once(memory):
    import asyncio

    await put(memory, "u", "user", "request", 0)
    await put(memory, "a", "assistant", "answer", 1)

    async def fold():
        return await memory.organize(
            CTX, operation="fold_segment", record_ids=["u", "a"], summary="once"
        )

    results = await asyncio.gather(fold(), fold())
    assert sum(r.changed for r in results) == 1
    assert [r.content for r in await view(memory)] == ["request", "once"]


async def test_image_ties_do_not_cross_scope_boundaries(memory):
    from ctx_weft.protocols import ImagePart

    ref = "blob:" + "d" * 64
    await put(
        memory, "old", "user", [ImagePart(data=ref, source_type="ref", media_type="image/png")], 0
    )
    await put(
        memory,
        "recent",
        "user",
        [ImagePart(data=ref, source_type="ref", media_type="image/png")],
        1,
    )
    await memory.ingest(
        MemoryEvent(
            id="agent-tie",
            kind=MemoryKind.CONVERSATION_TURN,
            scope=MemoryScope.AGENT,
            address=MemoryAddress(session_id=CTX.session_id, agent_id=CTX.agent_id),
            role="user",
            content="unrelated same timestamp",
            timestamp=BASE,
        ),
        CTX,
    )
    result = await memory.organize(CTX, operation="demote_images", keep_recent_images=1)
    assert result.demoted_images == 1 and result.superseded_ids == ("old",)
    agent = await load_records(
        memory,
        MemoryAddress(session_id=CTX.session_id, agent_id=CTX.agent_id),
        MemoryScope.AGENT,
        CTX,
    )
    assert [r.id for r in agent] == ["agent-tie"]


async def test_legacy_unknown_dispatch_is_conservatively_retained(memory):
    await put(memory, "u", "user", "request", 0)
    await put(memory, "a", "assistant", "old answer", 1)
    await put(memory, "legacy", "assistant", "dispatching", 2, metadata={"tool_call_count": 1})
    rows = await memory.recall_for_organize(CTX, operation="fold_current", keep_last=0)
    assert [r.id for r in rows] == ["u", "a"]
