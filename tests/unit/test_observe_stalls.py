"""Observation recovery and live-but-stalled streams must release their locks."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from ctx_weft.core.control.reducers import reduce_events
from ctx_weft.core.control.tokens import CancelToken, PauseToken
from ctx_weft.core.loop.driver import ExecutionMode, StepDriver, StepExecution, make_event
from ctx_weft.core.loop.steps.observe import (
    ObservationResult,
    ObserveStep,
    await_foreground,
    begin,
    complete,
    request,
)
from ctx_weft.protocols import LLMChunk, LLMMessage, MemoryScope, load_records
from ctx_weft.protocols.events import EventType, PersistenceUnavailableError
from ctx_weft.providers.events import InProcessEventBus
from ctx_weft.providers.llm.adapters.anthropic import AnthropicAdapter
from ctx_weft.providers.llm.adapters.openai_compatible import OpenAICompatibleAdapter
from tests.assembler_testkit import scripted_inputs
from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_runtime
from tests.unit.test_observe_react_helper import _make_ctx, _make_state


@pytest.mark.parametrize("saved_completion", [False, True])
@pytest.mark.parametrize("foreground", [False, True])
async def test_foreground_recap_recovery_does_not_reenter_observe_lock(
    fake_state_ctx, monkeypatch, saved_completion, foreground,
):
    state, ctx = fake_state_ctx
    runtime = make_runtime(agent_provider=InlineAgentTemplateProvider())
    state.session.root_agent_id = state.agent.id
    ctx.task_manager._cancelled = False
    ctx.task_manager.is_task_tree_canceled = lambda _: False
    state.agent.loop_config.short_segment_token_threshold = 0
    ctx.steps = StepDriver({"observe": ObserveStep()})
    ctx.execution = StepExecution(options={"boundary": "dispatch", "judge_task": False})
    await request(state, ctx, "dispatch", False)
    calls = []

    async def report(*args):
        calls.append(True)
        return ObservationResult(act_recap="recovered report")

    monkeypatch.setattr(ObserveStep, "_llm_observe", report)
    if saved_completion:
        await begin(state, ctx, "dispatch", False)
        await complete(state, ctx, ObservationResult(act_recap="recovered report"))
    info = dict(ctx.execution.options)
    if saved_completion:
        info["completion"] = state.extra["observe_completion"]

    await asyncio.wait_for(runtime._resume_observe_finalization(
        state, ctx, info, foreground=foreground), timeout=1)

    assert len(calls) == (0 if saved_completion else 1)
    events = ctx.event_bus.emitted
    assert sum(e.type == EventType.OBSERVE_COMPLETED for e in events) == 1
    assert sum(e.type == EventType.OBSERVE_APPLIED for e in events) == 1
    assert not reduce_events(events, state.run_id).pending_observe
    assert not ctx.steps.executions._guards
    assert not runtime._run_tokens and not runtime._pending_actor_runs
    records = await load_records(ctx.memory, state.scope, MemoryScope.TASK, ctx.provider_ctx)
    assert any("recovered report" in str(r.content) for r in records)


class _HeartbeatBody(httpx.AsyncByteStream):
    def __init__(self, protocol):
        event = ({"choices": [{"delta": {"content": "unfinished report"}}]}
                 if protocol == "openai" else
                 {"type": "content_block_delta", "index": 0,
                  "delta": {"type": "text_delta", "text": "unfinished report"}})
        self.prefix = ("data: " + json.dumps(event) + "\n\n").encode()
        self.started = asyncio.Event()
        self.closed = False
        self.heartbeats = 0

    async def __aiter__(self):
        yield self.prefix
        self.started.set()
        while True:
            await asyncio.sleep(0.005)
            self.heartbeats += 1
            yield b": keep-alive\n\n"

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
async def test_background_observe_heartbeat_stall_keeps_raw_and_releases_lock(
    fake_state_ctx, protocol, caplog,
):
    state, ctx = fake_state_ctx
    state.agent.loop_config.short_segment_token_threshold = 0
    state.session.token_used = 0
    ctx.config = SimpleNamespace(observe_llm_idle_timeout_sec=0.05)
    ctx.cancel_token = CancelToken()
    body = _HeartbeatBody(protocol)
    base = OpenAICompatibleAdapter if protocol == "openai" else AnthropicAdapter

    class Offline(base):
        def _make_client(self):
            return httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _: httpx.Response(200, stream=body)))

    adapter = Offline(api_key="local-test")
    ctx.llm = adapter
    driver = StepDriver({"observe": ObserveStep()})
    before = await load_records(ctx.memory, state.scope, MemoryScope.TASK, ctx.provider_ctx)
    pending = await driver.start_durable("observe", state, ctx,
        options={"boundary": "interrupt", "judge_task": False})
    try:
        await asyncio.wait_for(body.started.wait(), timeout=1)
        # The report retains its independent lifetime after the actor stops.
        ctx.cancel_token.cancel()
        await asyncio.wait_for(pending, timeout=1)
        after = await load_records(ctx.memory, state.scope, MemoryScope.TASK, ctx.provider_ctx)
        assert [r.id for r in after] == [r.id for r in before]
        assert body.heartbeats > 0 and body.closed
        assert "no progress" in caplog.text
        events = ctx.event_bus.emitted
        completed, = [e for e in events if e.type == EventType.OBSERVE_COMPLETED]
        assert completed.payload["action"] == "keep_raw"
        assert completed.payload["accepted"] is False
        assert any(e.type == EventType.OBSERVE_APPLIED for e in events)
        assert not reduce_events(events, state.run_id).pending_observe
        assert not driver.executions._guards
        # Another observation can acquire the same task's guard immediately.
        async with asyncio.timeout(1):
            async with driver.executions.task_guard(state.task.id, "observe"):
                pass
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await adapter._client.aclose()


@pytest.mark.parametrize("kind", ["token", "reasoning", "tool_call_partial"])
async def test_observe_progress_extends_idle_timeout(kind):
    state, ctx = _make_state(), _make_ctx("accepted report")
    ctx.config = SimpleNamespace(observe_llm_idle_timeout_sec=0.15)

    async def healthy(request, stream=True):
        for _ in range(6):
            await asyncio.sleep(0.04)
            yield LLMChunk(kind=kind, text="progress" if kind != "tool_call_partial" else "")
        from ctx_weft.protocols import ToolCall
        yield LLMChunk(kind="tool_call", tool_call=ToolCall(
            id="report", name="control__report_observation", arguments={}))

    ctx.llm.complete = healthy
    submitted, _ = await asyncio.wait_for(ObserveStep()._run_react(
        state, ctx,
        inputs=scripted_inputs(system="SYS", messages=[LLMMessage(role="user", content="review")],
                               tools=[SimpleNamespace(name="control__report_observation")], purpose="observe"),
        max_rounds=1, terminal_tool_name="control__report_observation"), timeout=2)
    assert submitted.content == "accepted report"


async def test_empty_chunks_do_not_extend_observe_idle_timeout():
    state, ctx = _make_state(), _make_ctx("")
    ctx.config = SimpleNamespace(observe_llm_idle_timeout_sec=0.05)
    closed = asyncio.Event()

    async def stalled(request, stream=True):
        try:
            while True:
                yield LLMChunk(kind="token", text="")
                await asyncio.sleep(0.005)
        finally:
            closed.set()

    ctx.llm.complete = stalled
    with pytest.raises(TimeoutError, match="no progress"):
        await asyncio.wait_for(ObserveStep()._run_react(
            state, ctx,
            inputs=scripted_inputs(system="SYS", messages=[], tools=[], purpose="observe"),
            max_rounds=1, terminal_tool_name="control__report_observation"), timeout=1)
    assert closed.is_set()


async def test_foreground_fold_storage_failure_wins_over_concurrent_pause(fake_state_ctx):
    state, ctx = fake_state_ctx
    state.extra["observe_id"] = "pending-observe"
    ctx.pause_token = PauseToken()
    ctx.hitl = make_runtime(agent_provider=InlineAgentTemplateProvider()).hitl
    error = PersistenceUnavailableError("fold commit unavailable")

    async def failed_fold():
        ctx.pause_token.pause()
        raise error

    with pytest.raises(PersistenceUnavailableError) as caught:
        await await_foreground(failed_fold(), state, ctx)
    assert caught.value is error
    assert not ctx.event_bus.emitted


@pytest.mark.parametrize("already_parked", [False, True])
async def test_background_judgement_waits_for_its_producer_to_park(
    fake_state_ctx, monkeypatch, already_parked,
):
    state, ctx = fake_state_ctx
    ctx.event_bus = InProcessEventBus()
    ctx.execution = StepExecution(ExecutionMode.BACKGROUND, {
        "boundary": "plain_text", "judge_task": True, "waiting_hitl_id": "waiting"})
    ctx.hitl = SimpleNamespace(registry=SimpleNamespace(
        get=lambda identity: SimpleNamespace(effective_decision=None)))
    if already_parked:
        state.task.status = "AWAITING_HUMAN"
    started = asyncio.Event()
    subscription = ctx.event_bus.subscribe(None, lambda event: _started(event, started))
    calls = []
    step = ObserveStep()

    async def body(*args):
        calls.append(state.task.status)
        return step._outcome(ObservationResult())

    monkeypatch.setattr(step, "_background_body", body)
    pending = asyncio.create_task(step.execute(state, ctx))
    try:
        await asyncio.wait_for(started.wait(), 1)
        if not already_parked:
            assert not calls
            state.task.status = "AWAITING_HUMAN"
            await ctx.event_bus.emit(make_event(state, EventType.TASK_AWAITING_HUMAN, {}))
        await asyncio.wait_for(pending, 1)
        assert calls == ["AWAITING_HUMAN"]
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await subscription.unsubscribe()


async def _started(event, ready):
    if event.type == EventType.OBSERVE_STARTED:
        ready.set()


async def test_background_producer_wait_is_bounded(fake_state_ctx, monkeypatch):
    state, ctx = fake_state_ctx
    ctx.event_bus = InProcessEventBus()
    ctx.execution = StepExecution(ExecutionMode.BACKGROUND, {
        "boundary": "plain_text", "judge_task": True, "waiting_hitl_id": "waiting"})
    ctx.config = SimpleNamespace(observe_llm_idle_timeout_sec=0.02)
    ctx.hitl = SimpleNamespace(registry=SimpleNamespace(
        get=lambda identity: SimpleNamespace(effective_decision=None)))
    events = []

    async def record(event):
        events.append(event)

    subscription = ctx.event_bus.subscribe(None, record)
    step = ObserveStep()

    async def forbidden(*args):
        raise AssertionError("a stalled producer must not be judged")

    monkeypatch.setattr(step, "_background_body", forbidden)
    try:
        await asyncio.wait_for(step.execute(state, ctx), 1)
        completed, = [e for e in events if e.type == EventType.OBSERVE_COMPLETED]
        assert completed.payload["action"] == "keep_raw"
        assert completed.payload["accepted"] is False
        assert any(e.type == EventType.OBSERVE_APPLIED for e in events)
        assert not StepDriver.for_context(ctx).executions._finish_waiters
    finally:
        await subscription.unsubscribe()
