"""Force lock contention and cancellation; every competing operation is bounded."""

import asyncio
from types import SimpleNamespace

import pytest

from ctx_weft.core.control.reducers import snapshot_facts_from_event
from ctx_weft.core.control.snapshot_writer import SnapshotWriter
from ctx_weft.core.loop.steps.compact import escalating_compact
from ctx_weft.core.loop.steps.observe import capture_context
from ctx_weft.protocols.events import EventType
from ctx_weft.providers.events.store.sql import open_sqlite_event_store
from ctx_weft.providers.memory.sql import open_sqlite_memory
from tests.integration.test_assistant_memory_pending import fail_write, options, setup, turn
from tests.integration.test_assistant_memory_recovery import (
    SESSION,
    InlineAgentTemplateProvider,
    make_runtime,
)


async def stop(*tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_recovery_waiting_for_writer_does_not_deadlock_next_writer(monkeypatch):
    runtime, memory = setup()
    await runtime._ensure_assistant_memory(SESSION)
    first, second = turn(runtime, memory), turn(runtime, memory)
    writing, scanned, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    real_ingest, real_read = memory.ingest, memory.read_records

    async def ingest(event, ctx):
        if event.id == first[3].id:
            writing.set()
            await release.wait()
        return await real_ingest(event, ctx)

    async def read(ids, ctx):
        records = await real_read(ids, ctx)
        if first[3].id in ids and not records:
            scanned.set()
        return records

    monkeypatch.setattr(memory, "ingest", ingest)
    monkeypatch.setattr(memory, "read_records", read)
    writer = asyncio.create_task(runtime._persist_assistant_turn(*first))
    tasks = [writer]
    try:
        await asyncio.wait_for(writing.wait(), 2)
        recovery = asyncio.create_task(runtime._restore_assistant_messages(SESSION))
        tasks.append(recovery)
        await asyncio.wait_for(scanned.wait(), 2)
        # Recovery owns the session initialization lock and waits for agent A.
        assert not runtime._assistant_memory_ready and not recovery.done()
        next_writer = asyncio.create_task(runtime._persist_assistant_turn(*second))
        tasks.append(next_writer)
        await asyncio.sleep(0)
        assert not next_writer.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 3)
        assert len(await real_read([first[3].id, second[3].id], first[1].provider_ctx)) == 2
        assert not any(runtime._assistant_pending.values())
    finally:
        release.set()
        await stop(*tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("waiter_kind", ["observer", "recovery"])
async def test_canceling_a_lock_waiter_does_not_strand_the_owner(monkeypatch, waiter_kind):
    runtime, memory = setup()
    await runtime._ensure_assistant_memory(SESSION)
    values = turn(runtime, memory)
    writing, release = asyncio.Event(), asyncio.Event()
    real = memory.ingest

    async def ingest(event, ctx):
        writing.set()
        await release.wait()
        return await real(event, ctx)

    monkeypatch.setattr(memory, "ingest", ingest)
    writer = asyncio.create_task(runtime._persist_assistant_turn(*values))
    tasks = [writer]
    try:
        await asyncio.wait_for(writing.wait(), 2)
        operation = (capture_context(values[0], values[1], options()) if waiter_kind == "observer"
                     else runtime._restore_assistant_messages(SESSION))
        waiter = asyncio.create_task(operation)
        tasks.append(waiter)
        await asyncio.sleep(0)
        assert not waiter.done()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(waiter, 2)
        release.set()
        await asyncio.wait_for(writer, 2)
        # Re-initialization may be required after canceling recovery. Neither
        # its session lock nor the canceled agent waiter may survive cancellation.
        next_values = turn(runtime, memory)
        await asyncio.wait_for(runtime._persist_assistant_turn(*next_values), 2)
        await asyncio.wait_for(capture_context(next_values[0], next_values[1], options()), 2)
    finally:
        release.set()
        await stop(*tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["repair", "capture"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_failure_inside_checkpoint_releases_agent_lock(monkeypatch, stage, cancel):
    runtime, memory = setup()
    values = await fail_write(runtime, memory, monkeypatch)
    entered = asyncio.Event()
    method = "ingest" if stage == "repair" else "load_view_ids"
    real = getattr(memory, method)

    async def fail(*args, **kwargs):
        entered.set()
        if cancel:
            await asyncio.Future()
        raise OSError("fault inside agent checkpoint")

    monkeypatch.setattr(memory, method, fail)
    task = asyncio.create_task(capture_context(values[0], values[1], options()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else OSError):
            await asyncio.wait_for(task, 2)
        monkeypatch.setattr(memory, method, real)
        await asyncio.wait_for(capture_context(values[0], values[1], options()), 2)
        await asyncio.wait_for(runtime._persist_assistant_turn(*turn(runtime, memory)), 2)
        assert not any(runtime._assistant_pending.values())
    finally:
        await stop(task)


@pytest.mark.asyncio
async def test_inline_snapshot_callback_does_not_reenter_agent_lock(tmp_path):
    # Two independent SQL providers share one real SQLite file, as a host may do.
    path = tmp_path / "shared.db"
    async with open_sqlite_event_store(path) as store, open_sqlite_memory(path) as memory:
        runtime = make_runtime(event_store=store, agent_provider=InlineAgentTemplateProvider())
        runtime.providers.register_memory(memory)
        values = turn(runtime, memory)
        writer = SnapshotWriter(store, get_memory=lambda: memory)
        observed = []

        async def snapshot_inside_emit(event):
            key = runtime._assistant_key(values[1].provider_ctx, values[0].scope)
            assert runtime._assistant_agent_lock(key).locked()
            await writer._write(SESSION.id, event, "inside_assistant_emit")
            observed.append(True)

        subscription = runtime.event_bus.subscribe(
            EventType.LLM_RESPONSE_FINISHED, snapshot_inside_emit, provisional=True, required=True)
        try:
            await asyncio.wait_for(runtime._persist_assistant_turn(*values), 5)
            assert observed
            snap = await store.read_last_of_type(SESSION.id, EventType.STATE_SNAPSHOT)
            assert snapshot_facts_from_event(snap.event).assistant_memory_verified_position == 0
            await asyncio.wait_for(capture_context(values[0], values[1], options()), 5)
            await asyncio.wait_for(writer._write(SESSION.id, values[2], "after_assistant_write"), 5)
            snap = await store.read_last_of_type(SESSION.id, EventType.STATE_SNAPSHOT)
            proof = snapshot_facts_from_event(snap.event)
            assert proof.assistant_memory_verified_position == proof.last_commit_position
        finally:
            await subscription.unsubscribe()


@pytest.mark.asyncio
async def test_compact_model_await_does_not_hold_agent_lock(monkeypatch):
    from ctx_weft.core.loop.steps import compact

    runtime, memory = setup()
    state, ctx, _, event = await fail_write(runtime, memory, monkeypatch)
    state.agent.loop_config = SimpleNamespace(compact_target_ratio=0.5, compact_token_ratio=0.8,
                                              compact_keep_last=0, collapse_keep_last=0)
    state.resolved_model = SimpleNamespace(effective_input_limit=1000)
    ctx.llm = SimpleNamespace(tokenizer=SimpleNamespace(count=len))
    called = []

    async def summarize(state, ctx, scope):
        key = runtime._assistant_key(ctx.provider_ctx, state.scope)
        assert not runtime._assistant_agent_lock(key).locked()
        # Simulate work arriving while the model is awaited. It needs the same
        # agent lock and must finish before the model can return its summary.
        await runtime._persist_assistant_turn(*turn(runtime, memory))
        called.append(True)
        return "summary"

    monkeypatch.setattr(compact, "summarize_for_compact", summarize)
    await asyncio.wait_for(escalating_compact(state, ctx, token_estimate=1000, force=True), 2)
    assert called and not any(runtime._assistant_pending.values())
    assert await memory.read_records([event.id], ctx.provider_ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize("sql", [False, True])
async def test_writers_observers_and_compact_complete_under_contention(tmp_path, sql):
    from contextlib import AsyncExitStack

    async with AsyncExitStack() as stack:
        if sql:
            path = tmp_path / "contended.db"
            store = await stack.enter_async_context(open_sqlite_event_store(path))
            memory = await stack.enter_async_context(open_sqlite_memory(path))
            runtime = make_runtime(event_store=store, agent_provider=InlineAgentTemplateProvider())
            runtime.providers.register_memory(memory)
        else:
            runtime, memory = setup()
        await runtime._ensure_assistant_memory(SESSION)
        start = asyncio.Event()
        values = [turn(runtime, memory, f"agent-{i % 3}") for i in range(18)]

        async def run(value):
            await start.wait()
            state, ctx, finished, event = value
            state.agent.loop_config = SimpleNamespace(
                compact_target_ratio=0.5, compact_token_ratio=0.8,
                compact_keep_last=1000, collapse_keep_last=1000)
            state.resolved_model = SimpleNamespace(effective_input_limit=1000)
            await runtime._persist_assistant_turn(state, ctx, finished, event)
            await asyncio.gather(capture_context(state, ctx, options()),
                                 escalating_compact(state, ctx, token_estimate=1000, force=True))

        tasks = [asyncio.create_task(run(value)) for value in values]
        try:
            start.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 10)
            assert not any(runtime._assistant_pending.values())
            assert all(not lock.locked() for lock in runtime._assistant_agent_locks.values())
            assert all(not lock.locked() for lock in runtime._assistant_memory_locks.values())
            records = await memory.read_records([value[3].id for value in values], values[0][1].provider_ctx)
            assert len(records) == len(values)
        finally:
            await stop(*tasks)
