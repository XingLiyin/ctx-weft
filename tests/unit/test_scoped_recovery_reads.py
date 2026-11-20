"""Recovery reads stay scoped even when unrelated history grows."""
# ruff: noqa: F811 -- pytest fixtures are explicitly imported for parametrization.
import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest

from ctx_weft.core.control.reducers import (
    read_observe_request,
    read_task_view,
    reduce_events,
)
from ctx_weft.core.control.task_settlement import (
    advance_task_settlement,
    load_task_settlement_baseline,
)
from ctx_weft.core.loop.driver import ExecutionMode, StepExecution
from ctx_weft.core.loop.steps.observe import wait_disposition
from ctx_weft.core.runtime import CtxWeftRuntime
from ctx_weft.protocols.events import Event, EventOrigin, EventReadProjection, EventType
from tests._snapshot_helpers import seed_snapshot
from tests.unit.test_read_primitives_conformance import store  # noqa: F401
from tests.unit.test_runtime_initialization_queries import container, runtime


def ev(identity, kind, *, owner="target", run="actor", **payload):
    return Event(id=identity, session_id="session", task_id=owner, agent_id="agent",
                 run_id=run, sequence=1, timestamp=datetime.now(UTC), type=kind,
                 origin=EventOrigin.LOOP_ACT, payload=payload)


async def seed(store, *events):
    await store.append_batch("session", events[0].id, list(events))


async def test_storage_selects_identity_and_payload_before_field_projection(store):
    await seed(store, ev("other", EventType.OBSERVE_REQUESTED, owner="other", observe_id="other"),
               ev("wanted", EventType.OBSERVE_REQUESTED, observe_id="obs", body="keep"),
               ev("numeric", EventType.OBSERVE_REQUESTED, observe_id=123),
               ev("nested", EventType.HITL_OPENED, observe_request={"observe_id": "obs"}))
    projection = EventReadProjection({EventType.OBSERVE_REQUESTED: (("body",),)})
    rows = await store.read_range("session", payload_equals=(("observe_id",), "obs"),
                                  projection=projection)
    assert [(row.event.id, row.event.payload) for row in rows] == [("wanted", {"body": "keep"})]
    assert not await store.read_range("session", payload_equals=(("observe_id",), "123"))
    assert [row.event.id for row in await store.read_range(
        "session", payload_equals=(("observe_request", "observe_id"), "obs"))] == ["nested"]
    assert [row.event.id for row in await store.read_range("session", event_ids=("wanted",))] == ["wanted"]
    assert not await store.read_range("foreign", event_ids=("wanted",))
    assert not await store.read_range("session", event_ids=("wanted",), run_id="different")
    assert not await store.read_range("session", event_ids=("wanted",), through_position=1)
    rows = await store.read_range("session", event_ids=("nested", "wanted", "wanted", "missing"))
    assert [row.event.id for row in rows] == ["wanted", "nested"]
    assert [row.event.id for row in await store.read_range("session",
        event_ids=("nested", "wanted"), after_position=2)] == ["nested"]
    for invalid in ("wanted", ("",), (1,)):
        with pytest.raises(ValueError, match="event_ids"):
            await store.read_range("session", event_ids=invalid)


async def test_memory_id_queries_do_not_iterate_session_history():
    from ctx_weft.providers.events import InMemoryEventStore

    store = InMemoryEventStore()
    await seed(store, ev("other", "Test"))
    await seed(store, ev("wanted", "Test"))

    class NoIteration(list):
        def __iter__(self):
            raise AssertionError("session history traversal")

    store._stored["session"] = NoIteration(store._stored["session"])
    assert [row.event.id for row in await store.read_range("session", event_ids=("wanted",))] == ["wanted"]


async def test_postgres_payload_selection_serializes_a_string_once():
    import json

    from sqlalchemy.dialects.postgresql import JSONB, dialect

    from ctx_weft.providers.events.store.sql.store import SqlEventStore

    factory = MagicMock()
    db = factory.return_value.__aenter__.return_value
    db.get_bind = MagicMock(return_value=NS(dialect=NS(name="postgresql")))
    db.execute = AsyncMock(return_value=NS(mappings=lambda: []))
    await SqlEventStore(factory).read_range("session", related_task_id="target",
        payload_equals=(("observe_request", "observe_id"), "obs"), projection=EventReadProjection({}))
    query = db.execute.await_args.args[0].compile(dialect=dialect())
    encoded = [bind.type.bind_processor(dialect())(bind.value)
               for bind in set(query.binds.values()) if isinstance(bind.type, JSONB)
               and isinstance(bind.value, str)]
    assert [json.loads(value) for value in encoded] == ["obs"]


async def test_payload_selection_keeps_literal_object_keys_and_rejects_nonobjects(store):
    key = 'quote".key'
    await seed(store, ev("literal", "Test", data={key: "中文"}),
               ev("string", "Test", data="not JSON"),
               ev("array", "Test", data=["中文"]),
               ev("null", "Test", data=None))
    rows = await store.read_range("session", payload_equals=(("data", key), "中文"))
    assert [row.event.id for row in rows] == ["literal"]
    assert not await store.read_range("session", payload_equals=(("data", "0"), "中文"))


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("bound_actor", [False, True])
async def test_output_recovery_never_loads_other_runs_or_later_task_changes(store, monkeypatch, nested, bound_actor):
    intent = {"task_id": "target", "observe_id": "obs"}
    if bound_actor:
        intent["actor_run_id"] = "actor"
    await seed(store,
        ev("created", EventType.TASK_CREATED, task={"id": "target", "user_prompt": "original"}),
        ev("old", EventType.LLM_RESPONSE_FINISHED, run="previous", content="old"),
        *(ev(f"gap-{i}", EventType.LLM_RESPONSE_FINISHED, owner="foreign", run="foreign",
              content="unrelated-body") for i in range(2100)),
        ev("answer", EventType.LLM_RESPONSE_FINISHED, content="correct", turn=1),
        ev("request", EventType.HITL_OPENED if nested else EventType.OBSERVE_REQUESTED,
           run=None if nested else "actor",
           **({"observe_request": intent} if nested else intent)),
        ev("changed", EventType.TASK_REQUEUED, user_prompt="later"),
        ev("later", EventType.LLM_RESPONSE_FINISHED, run="later-run", content="later"),
        *(ev(f"foreign-{i}", EventType.LLM_RESPONSE_FINISHED, owner="foreign", run=f"r-{i}",
              content="unrelated-body" * 1000) for i in range(30)))
    read = store.read_range
    observed = []

    async def guarded(sid, **kwargs):
        assert kwargs.get("event_ids") or kwargs.get("payload_equals") or kwargs.get("related_task_id")
        rows = await read(sid, **kwargs)
        assert "unrelated-body" not in repr(rows)
        observed.append((kwargs, [row.event.id for row in rows]))
        return rows

    monkeypatch.setattr(store, "read_range", guarded)
    owner = NS(event_store=store, _restore_task_prompts=AsyncMock())
    task = NS(id="target", tenant_id="default", outputs=None, suspend_requested=False)
    result = await CtxWeftRuntime._restore_observe_outputs(owner, "session", task, {"observe_id": "obs"})
    assert task.user_prompt == "original" and task.outputs == "correct"
    assert len(result["restored_transcript"]) == 1
    assert result["actor_run_id"] == "actor"
    body_reads = [ids for query, ids in observed if query.get("run_id")]
    assert body_reads == [["answer"]]
    metadata_reads = [query for query, _ in observed
                      if query.get("include_types") == (EventType.LLM_RESPONSE_FINISHED,)
                      and not query.get("run_id") and not query.get("event_ids")]
    assert len(metadata_reads) == (1 if nested and not bound_actor else 0)


@pytest.mark.parametrize("count", [20, 1001])
@pytest.mark.parametrize("run", [None, ""])
async def test_legacy_output_without_run_id_batches_exact_response_bodies(store, monkeypatch, count, run):
    await seed(store,
        ev("old-run", EventType.LLM_RESPONSE_FINISHED, run="old", content="wrong"),
        *(ev(f"legacy-{i}", EventType.LLM_RESPONSE_FINISHED, run=run, content=f"legacy answer-{i}")
          for i in range(count)),
        ev("request", "TaskRecapRequested", run=run, task_id="target", observe_id="obs"),
        ev("later", EventType.LLM_RESPONSE_FINISHED, run=run, content="too late"))
    read = store.read_range
    body_reads = []

    async def guarded(sid, **kwargs):
        rows = await read(sid, **kwargs)
        if any("content" in row.event.payload for row in rows):
            body_reads.append(kwargs.get("event_ids"))
            assert all(identity.startswith("legacy-") for identity in kwargs["event_ids"])
            assert len(kwargs["event_ids"]) <= 500
        return rows

    monkeypatch.setattr(store, "read_range", guarded)
    task = NS(id="target", outputs=None, suspend_requested=False)
    await CtxWeftRuntime._restore_observe_outputs(NS(event_store=store), "session", task,
                                                 {"observe_id": "obs"})
    assert task.outputs == f"legacy answer-{count - 1}"
    assert len(body_reads) == (count + 499) // 500
    assert [identity for batch in body_reads for identity in batch] == [f"legacy-{i}" for i in range(count)]


@pytest.mark.parametrize("legacy", [False, True])
async def test_task_snapshot_read_excludes_other_task_and_observe_bodies(store, monkeypatch, legacy):
    events = [
        ev("created", EventType.TASK_CREATED, task={"id": "target", "user_prompt": "keep"}),
        ev("foreign", EventType.TASK_CREATED, owner="foreign",
           task={"id": "foreign", "user_prompt": "unrelated-body" * 1000}),
        ev("request", EventType.OBSERVE_REQUESTED, task_id="target", observe_id="obs",
           context_record_ids=["unrelated-body"])]
    await seed(store, *events)
    view = reduce_events(events, "session")
    if legacy:
        view.pending_observe["obs"].pop("request_event_id")
        view.pending_observe["obs"].pop("actor_run_id")
    await seed_snapshot(store, "session", view, cut=3,
                        overrides={"projection_version": 11} if legacy else None)
    head = await store.committed_head("session")
    read_last = store.read_last_of_type

    async def guarded(*args, **kwargs):
        assert kwargs.get("projection") is not None
        row = await read_last(*args, **kwargs)
        assert "unrelated-body" not in repr(row)
        assert set(row.event.payload["state_blob"]["tasks"]) == {"target"}
        return row

    monkeypatch.setattr(store, "read_last_of_type", guarded)
    assert (await read_task_view(store, "session", "target", through_position=head)).user_prompt == "keep"
    request, _ = await read_observe_request(store, "session", "target", "obs",
        through_position=head, request_event_id=view.pending_observe["obs"].get("request_event_id", ""))
    assert request.event.id == "request"


async def test_pending_observe_uses_payload_owner_when_envelope_is_parent(store):
    await seed(store, ev("created", EventType.TASK_CREATED, task={"id": "target"}),
        ev("request", EventType.OBSERVE_REQUESTED, owner="parent", task_id="target", observe_id="obs"),
        ev("started", EventType.OBSERVE_STARTED, owner="parent", task_id="target", observe_id="obs", judge_task=True),
        ev("foreign", EventType.OBSERVE_REQUESTED, owner="foreign", task_id="foreign", observe_id="foreign"))
    head = await store.committed_head("session")
    progress = await load_task_settlement_baseline(store, "session", "target", through_position=head)
    assert set(progress.pending_observe) == {"obs"}
    task = await read_task_view(store, "session", "target", through_position=head)
    assert task.observe_count == 1
    await seed(store, ev("applied", EventType.OBSERVE_APPLIED, owner="parent", task_id="target", observe_id="obs"))
    progress = await advance_task_settlement(store, progress, through_position=await store.committed_head("session"))
    assert not progress.pending_observe


async def test_disposition_wait_advances_cursor_and_ignores_crash_calibration(store, monkeypatch):
    await seed(store, ev("request", EventType.OBSERVE_REQUESTED, task_id="target", observe_id="obs"),
               ev("crash", EventType.TASK_INTERRUPTED, reason="crash_recovery"),
               ev("foreign", EventType.TASK_FINISHED, owner="foreign"))
    first_read, ready = asyncio.Event(), asyncio.Event()
    read = store.read_range
    cursors = []

    async def guarded(sid, **kwargs):
        rows = await read(sid, **kwargs)
        if kwargs.get("related_task_id") and "after_position" in kwargs:
            assert kwargs["related_task_id"] == "target"
            cursors.append(kwargs["after_position"])
            first_read.set()
        return rows

    monkeypatch.setattr(store, "read_range", guarded)
    state = NS(session=NS(id="session"), task=NS(id="target"), extra={"observe_id": "obs"})
    ctx = NS(event_store=store, execution=StepExecution(ExecutionMode.BACKGROUND, {"verdict": True}))
    waiting = asyncio.create_task(wait_disposition(state, ctx, ready))
    try:
        await asyncio.wait_for(first_read.wait(), 2)
        assert not waiting.done()
        await seed(store, ev("settled", EventType.TASK_REQUEUED))
        ready.set()
        await asyncio.wait_for(waiting, 2)
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
    assert ctx.execution.options["settled"] and cursors == [1, 3]


async def test_hot_task_list_performs_no_storage_queries(monkeypatch):
    core = runtime()
    assert not hasattr(core, "list_tasks")
    handle = await container(core)
    turn = await core.dispatch_task(handle.session_id, "work", agent_id=handle.root_agent_id,
                                    unattended=True)
    for name in ("read_range", "read_last_of_type", "committed_head", "list_session_ids"):
        monkeypatch.setattr(core.event_store, name, AsyncMock(side_effect=AssertionError("cold read")))
    rows = await core.list_loaded_tasks(handle.session_id)
    assert [row.data.task_id for row in rows] == [turn.task_id]
    assert rows[0].loaded and rows[0].source == "memory"
    assert await core.list_loaded_tasks("not-loaded") == []
    assert await core.list_loaded_tasks(handle.session_id, agent_id="different") == []
