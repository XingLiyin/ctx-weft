"""Cancellation memory replays partially committed slots and parent acknowledgements."""
from datetime import timedelta

import pytest

from ctx_weft.core.loop.steps.finalize import (
    synthesize_cancel_closure, _ensure_dispatch_frame, _put_dispatch_result,
)
from ctx_weft.core.models.task import Task
from ctx_weft.core.utils.clock import now_utc
from ctx_weft.protocols import MemoryAddress, MemoryKind, MemoryScope, ProviderContext
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider


@pytest.mark.parametrize("parent", [None, "parent"])
async def test_cancel_replays_partial_finish_without_duplicate_slots(parent, monkeypatch):
    memory = InMemoryMemoryProvider()
    started = now_utc()
    task = Task(id="task", session_id="session", status="CANCELED",
        creator_agent_id="agent", assigned_agent_id="agent", parent_task_id=parent,
        started_at=started, finished_at=started + timedelta(seconds=10))
    ctx = ProviderContext(session_id="session", task_id=task.id, agent_id="agent", tenant_id="default")
    if parent:
        scope = MemoryAddress(session_id="session", agent_id="agent", task_id=parent)
        timestamp, call = await _ensure_dispatch_frame(memory, scope, task, ctx)
        await _put_dispatch_result(memory, scope, task, "running", timestamp, ctx,
                                   replace=False, tool_call_id=call)
    original = memory.ingest
    written = 0
    async def crash_after_first_slot(event, ctx):
        nonlocal written
        result = await original(event, ctx)
        written += 1
        if written == 1:
            raise OSError("crash after first cancellation slot")
        return result
    monkeypatch.setattr(memory, "ingest", crash_after_first_slot)
    with pytest.raises(OSError, match="first cancellation slot"):
        await synthesize_cancel_closure(memory, "session", task, ctx, "cancel")
    monkeypatch.setattr(memory, "ingest", original)
    task.started_at = None  # cold recovery has no process-local start timestamp
    await synthesize_cancel_closure(memory, "session", task, ctx, "cancel")
    address = MemoryAddress(session_id="session", agent_id="agent")
    before = await memory.load_view(address, MemoryScope.AGENT, ctx, kinds=[MemoryKind.CONVERSATION_TURN])
    await synthesize_cancel_closure(memory, "session", task, ctx, "cancel")
    after = await memory.load_view(address, MemoryScope.AGENT, ctx, kinds=[MemoryKind.CONVERSATION_TURN])
    assert [(r.id, r.timestamp, r.content) for r in before] == [(r.id, r.timestamp, r.content) for r in after]
    finish = [r for r in after if r.metadata.get("finish_policy")]
    assert len(finish) == 2
    assert min(r.timestamp for r in finish) == task.finished_at
    if parent:
        ack = [r for r in after if r.role == "tool" and r.metadata.get("child_task_id") == task.id]
        assert len(ack) == 1 and "running" not in str(ack[0].content)
