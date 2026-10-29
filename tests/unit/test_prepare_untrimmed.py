"""Compact sees full inputs; only the sending gateway enforces the prompt budget."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from ctx_weft.core.assembler import ContextAssembler, ContextRequest
from ctx_weft.core.assembler.assembler import AssemblerDeps
from ctx_weft.core.assembler.sources.agent_recall import AgentRecallSource
from ctx_weft.core.loop.driver import LoopContext, LoopState, make_event
from ctx_weft.core.loop.llm_gateway import build_llm_request
from ctx_weft.core.loop.steps.prepare import PrepareStep, _estimate_assembled_tokens
from ctx_weft.core.models.agent import Agent, LoopGuard
from ctx_weft.core.models.errors import ContextOverflowError
from ctx_weft.core.models.session import Session
from ctx_weft.core.models.task import Task
from ctx_weft.protocols import MemoryAddress, MemoryEvent, MemoryKind, MemoryScope, ProviderContext
from ctx_weft.protocols.events import EventType
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider


async def harness(*, current_is_large=False):
    memory = InMemoryMemoryProvider()
    scope = MemoryAddress(session_id="s", task_id="current", agent_id="a")
    provider_ctx = ProviderContext(session_id="s", task_id="current", agent_id="a")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    for task_id, body in (("previous", "OLD_HISTORY " + "x" * 12000),
                          ("current", "CURRENT " + ("y" * 12000 if current_is_large else "hello"))):
        await memory.ingest(MemoryEvent(
            id=task_id, kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK,
            address=MemoryAddress(session_id="s", task_id=task_id, agent_id="a"),
            role="user", content=body, timestamp=now,
            metadata={"task_id": task_id}), provider_ctx)
        now += timedelta(seconds=1)
    session = Session(id="s", user_prompt="", context_limit=6000, reserved_output_tokens=256)
    task = Task(id="current", session_id="s", status="ACTIVE", title="Current task",
                user_prompt_in_memory=True)
    agent = Agent(id="a", session_id="s", template_id="none",
                  loop_guard=LoopGuard(context_limit=6000, reserved_output_tokens=256))
    state = LoopState(run_id="r", session=session, task=task, agent=agent, scope=scope,
                      resolved_model=NS(model="mock", account=""))
    ctx = LoopContext(
        assembler=ContextAssembler([AgentRecallSource()], AssemblerDeps(memory, [], provider_ctx)),
        memory=memory, provider_ctx=provider_ctx, event_bus=NS(emit=AsyncMock()),
        llm=NS(tokenizer=NS(count=len), context_limit=6000))
    return state, ctx


async def assemble(state, ctx):
    return await ctx.assembler.assemble(ContextRequest(
        purpose="act", scope=state.scope, task=state.task, agent=state.agent,
        session=state.session, template=None, bound_capabilities=[]))


@pytest.mark.parametrize("baseline", [0, 100])
async def test_compact_estimates_history_that_sending_would_discard(monkeypatch, baseline):
    state, ctx = await harness()
    initial = await assemble(state, ctx)
    bounded = await ctx.assembler.compile(initial, token_counter=len)
    threshold = (state.session.context_limit - state.session.reserved_output_tokens) * 0.8
    assert _estimate_assembled_tokens(bounded, len) < threshold
    assert "OLD_HISTORY" not in str(bounded.messages)
    state.agent.loop_guard.context_tokens = baseline
    state.agent.loop_guard.context_message_count = 2
    compact = AsyncMock(return_value=[])
    monkeypatch.setattr("ctx_weft.core.loop.steps.prepare.resolve_and_bind", AsyncMock(return_value=[]))
    monkeypatch.setattr("ctx_weft.core.loop.steps.prepare.commit_round", AsyncMock())
    monkeypatch.setattr("ctx_weft.core.loop.steps.compact.escalating_compact", compact)

    outcome = await PrepareStep().execute(state, ctx)

    compact.assert_awaited_once()
    assert compact.await_args.kwargs["token_estimate"] > threshold
    inputs = outcome.state_patch["prompt_inputs"]
    untrimmed = inputs._compiled_base[-1]
    assert untrimmed.token_budget == 0
    assert "OLD_HISTORY" in str(untrimmed.messages)
    assert untrimmed.token_count > state.session.context_limit
    request = await build_llm_request(ctx, state, inputs)
    assert inputs._compiled_base[-1].token_budget == 5744
    assert "OLD_HISTORY" not in str(request.messages)
    assert "CURRENT" in str(request.messages)
    assert request.messages != untrimmed.messages
    # Re-expanding a saved request still compiles with the normal window budget.
    restored = await ctx.assembler.compile(inputs.for_request([], request.tool_refs).to_payload())
    assert restored.system == request.system and restored.messages == request.messages


async def test_required_overflow_can_be_compacted_before_sending(monkeypatch):
    state, ctx = await harness(current_is_large=True)

    async def compact(state, ctx, *, token_estimate, trigger):
        assert token_estimate > state.session.context_limit
        await ctx.memory.fold(["current", "previous"], [MemoryEvent(
            id="summary", kind=MemoryKind.SUMMARY, scope=MemoryScope.TASK, address=state.scope,
            role="user", content="COMPACTED summary", timestamp=datetime.now(UTC),
            metadata={"task_id": state.task.id})], ctx.provider_ctx)
        return [make_event(state, EventType.MEMORY_COMPACTED)]

    monkeypatch.setattr("ctx_weft.core.loop.steps.prepare.resolve_and_bind", AsyncMock(return_value=[]))
    monkeypatch.setattr("ctx_weft.core.loop.steps.prepare.commit_round", AsyncMock())
    compact_mock = AsyncMock(side_effect=compact)
    monkeypatch.setattr("ctx_weft.core.loop.steps.compact.escalating_compact", compact_mock)

    outcome = await PrepareStep().execute(state, ctx)

    compact_mock.assert_awaited_once()
    request = await build_llm_request(ctx, state, outcome.state_patch["prompt_inputs"])
    assert "COMPACTED summary" in str(request.messages)
    assert "OLD_HISTORY" not in str(request.messages)


async def test_untrimmed_cache_cannot_bypass_required_overflow_at_gateway():
    state, ctx = await harness(current_is_large=True)
    inputs = await assemble(state, ctx)
    untrimmed = await ctx.assembler.compile(inputs, token_counter=len, trim=False, reuse=True)
    assert untrimmed.token_count > state.session.context_limit
    with pytest.raises(ContextOverflowError):
        await build_llm_request(ctx, state, inputs)
    ctx.event_bus.emit.assert_not_awaited()


async def test_compiling_for_estimation_and_sending_reads_records_once():
    state, ctx = await harness()
    inputs = await assemble(state, ctx)
    ctx.memory.read_records = AsyncMock(wraps=ctx.memory.read_records)
    await ctx.assembler.compile(inputs, token_counter=len, trim=False, reuse=True)
    first = await build_llm_request(ctx, state, inputs)
    second = await build_llm_request(ctx, state, inputs)
    assert first.messages == second.messages
    ctx.memory.read_records.assert_awaited_once()


async def test_cached_compilation_tracks_the_numeric_budget():
    state, ctx = await harness()
    inputs = await assemble(state, ctx)
    unbounded = await ctx.assembler.compile(inputs, token_counter=len, trim=False, reuse=True)
    assert unbounded.token_budget == 0
    assert "OLD_HISTORY" in str(unbounded.messages)

    bounded = await ctx.assembler.compile(inputs, token_counter=len, reuse=True)
    assert bounded.token_budget == 5744
    assert bounded is not unbounded
    assert "OLD_HISTORY" not in str(bounded.messages)
    # The same numeric budget shares the compiled result.
    assert await ctx.assembler.compile(inputs, token_counter=len, reuse=True) is bounded

    inputs.context_limit = 20_000
    expanded = await ctx.assembler.compile(inputs, token_counter=len, reuse=True)
    assert expanded.token_budget == 19_744
    assert expanded is not bounded
    assert "OLD_HISTORY" in str(expanded.messages)


async def test_exhausted_context_budget_cannot_reuse_unbounded_cache():
    state, ctx = await harness()
    inputs = await assemble(state, ctx)
    inputs.reserved_output_tokens = inputs.context_limit
    unbounded = await ctx.assembler.compile(inputs, token_counter=len, trim=False, reuse=True)
    assert unbounded.token_budget == 0
    with pytest.raises(ContextOverflowError) as exc:
        await ctx.assembler.compile(inputs, token_counter=len, reuse=True)
    assert exc.value.effective_limit == 0


async def test_tighter_budget_can_reuse_bounded_result_that_still_fits():
    state, ctx = await harness()
    inputs = await assemble(state, ctx)
    bounded = await ctx.assembler.compile(inputs, token_counter=len, reuse=True)
    assert "OLD_HISTORY" not in str(bounded.messages)
    assert bounded.token_budget == 5744
    inputs.context_limit = bounded.token_count + inputs.reserved_output_tokens
    assert inputs.context_limit < 6000
    assert await ctx.assembler.compile(inputs, token_counter=len, reuse=True) is bounded
    # Preserve the actual compilation budget, even when reused under a tighter one.
    assert bounded.token_budget == 5744


async def test_untrimmed_compilation_restores_discarded_history():
    state, ctx = await harness()
    inputs = await assemble(state, ctx)
    bounded = await ctx.assembler.compile(inputs, token_counter=len, reuse=True)
    assert "OLD_HISTORY" not in str(bounded.messages)
    complete = await ctx.assembler.compile(inputs, token_counter=len, trim=False, reuse=True)
    assert complete is not bounded
    assert complete.token_budget == 0
    assert "OLD_HISTORY" in str(complete.messages)
