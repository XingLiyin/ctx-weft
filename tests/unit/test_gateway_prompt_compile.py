"""Compile once per base; preserve live continuations and historical event inputs."""

import json
from copy import copy
from dataclasses import asdict
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import pytest

from ctx_weft.core.assembler import ContextAssembler
from ctx_weft.core.assembler.assembler import AssemblerDeps
from ctx_weft.core.assembler.builder import PromptBuilder
from ctx_weft.core.capabilities.cache import CapabilityCache
from ctx_weft.core.loop.driver import LoopState
from ctx_weft.core.loop.llm_gateway import build_llm_request, stream_llm_resilient
from ctx_weft.core.loop.llm_stream import ensure_collected_stream
from ctx_weft.protocols import LLMChunk, LLMMessage, LLMUsage, ProviderContext
from ctx_weft.protocols.capability import ToolCapability
from ctx_weft.protocols.events import EventOrigin, EventType
from ctx_weft.providers.llm.tokenizer import HeuristicTokenizer
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from tests.unit.test_prompt_input_snapshot import ADDRESS, CTX, assembled, prompt_providers


async def setup(purpose="act"):
    memory = InMemoryMemoryProvider()
    prompt, request, _ = await assembled(memory, purpose)
    assembler = ContextAssembler([], AssemblerDeps(memory, [], CTX, capability_provider_index=prompt_providers()))
    request.agent.loop_guard = NS(context_tokens=0, context_limit=100_000)
    request.session.id, request.session.tenant_id = "s", "default"
    state = LoopState(
        run_id="run", session=request.session, task=request.task, agent=request.agent,
        scope=ADDRESS, extra={"template": request.template},
        resolved_model=NS(model="mock", account=""), origin=EventOrigin.LOOP_ACT)
    events = []

    async def emit(event):
        events.append(event)

    ctx = NS(assembler=assembler, llm=NS(tokenizer=HeuristicTokenizer(), context_limit=100_000),
             event_bus=NS(emit=emit), provider_ctx=CTX)
    return prompt.inputs, state, ctx, events


async def test_untrimmed_estimate_is_reused_when_it_fits_the_sending_budget():
    inputs, state, ctx, _ = await setup()
    with patch.object(PromptBuilder, "compile", autospec=True, side_effect=PromptBuilder.compile) as render:
        budget = await ctx.assembler.compile(
            inputs, template=state.extra["template"], token_counter=ctx.llm.tokenizer.count,
            reuse=True, trim=False)
        first = await build_llm_request(ctx, state, inputs)
        assistant = LLMMessage(
            role="assistant", content="checking", reasoning_content="live reasoning",
            provider_state={"signature": "live signature"},
            tool_calls=[{"id": "call", "name": "read", "input": {}}])
        suffix = [assistant, LLMMessage(role="tool", content="result", tool_call_id="call")]
        state.agent.loop_guard.context_tokens = 1234
        second = await build_llm_request(
            ctx, state, inputs, appended_messages=suffix, baseline_msg_count=len(first.messages), turn=2)
        # The complete estimate already fits; sending and later rounds need
        # no additional compilation.
        assert render.call_count == 1
        assert inputs._compiled_base[-1] is budget
        assert budget.token_budget == 0
    assert second.messages[-2] is assistant
    assert second.messages[-2].provider_state == {"signature": "live signature"}
    assert first.messages == budget.messages
    assert second.messages[:-2] == budget.messages
    assert second.prompt_token_estimate > 1234
    assert second.metadata["prompt_est_base"] == 1234
    assert inputs.appended_messages == [] and inputs.tools is None
    assert "_compiled_base" not in asdict(inputs)
    assert "live signature" not in json.dumps(inputs.to_payload())


async def test_dynamic_tools_do_not_change_the_cached_base():
    inputs, state, ctx, _ = await setup()
    ctx.capability_cache = CapabilityCache()
    initial = ToolCapability(id="test:read", name="read", purposes=["act"])
    pinned = ToolCapability(id="test:extra", name="extra", purposes=["act"])
    ctx.capability_cache.put("a", [initial])
    first = await build_llm_request(ctx, state, inputs)
    ctx.capability_cache.pin("a", "t", [pinned])
    ctx.assembler.deps.capability_provider_index["test"].capabilities.append(pinned)
    second = await build_llm_request(ctx, state, inputs)
    assert len(second.tools) == len(first.tools) + 1
    assert second.system == first.system and second.messages == first.messages
    assert len(inputs._compiled_base[-1].tools) == 1


@pytest.mark.parametrize("change", ["template", "counter", "provider", "reassemble"])
async def test_base_cache_does_not_survive_changed_compilation_dependencies(change):
    inputs, state, ctx, _ = await setup()
    template = state.extra["template"]
    counter = ctx.llm.tokenizer.count
    first = await ctx.assembler.compile(inputs, template=template, token_counter=counter, reuse=True)
    if change == "template":
        template = copy(template)
    elif change == "counter":
        counter = len
    elif change == "provider":
        other = InMemoryMemoryProvider()
        await assembled(other)
        ctx.assembler.deps.memory = other
    else:
        inputs, _, _, _ = await setup()
    second = await ctx.assembler.compile(inputs, template=template, token_counter=counter, reuse=True)
    assert second is not first


async def test_compiled_cache_cannot_bypass_tenant_or_session_checks():
    inputs, state, ctx, _ = await setup()
    await build_llm_request(ctx, state, inputs)
    for provider_ctx in (ProviderContext(session_id="s", tenant_id="other"),
                         ProviderContext(session_id="other")):
        other = ContextAssembler([], AssemblerDeps(ctx.assembler.deps.memory, [], provider_ctx))
        with pytest.raises(ValueError):
            await other.compile(inputs, template=state.extra["template"],
                                token_counter=ctx.llm.tokenizer.count, reuse=True)


async def test_gateway_defers_serialization_and_replays_cumulative_continuations():
    inputs, state, ctx, events = await setup("observe")
    assistant = LLMMessage(role="assistant", content="checking",
                           reasoning_content="private reasoning", provider_state={"signature": "private"},
                           tool_calls=[{"id": "call", "name": "read", "input": {}}])
    suffix = [assistant, LLMMessage(role="tool", content="result", tool_call_id="call")]
    request = await build_llm_request(ctx, state, inputs, appended_messages=suffix, turn=2)
    assert request.prompt_inputs is None and events == []
    calls = []

    async def complete(request, stream=True):
        calls.append(request)
        yield LLMChunk(kind="token", text="done")
        yield LLMChunk(kind="usage", usage=LLMUsage(prompt_tokens=10))

    ctx.llm.complete = complete
    stream = ensure_collected_stream(stream_llm_resilient(
        ctx, state, request, inputs=inputs, appended_messages=suffix))
    async for _ in stream:
        stream.accept_current()
    payload = next(e.payload for e in events if e.type == EventType.LLM_PROMPT_SENT)
    assert payload["version"] == 1 and payload["turn"] == 2
    assert len(payload["appended_messages"]) == 2
    assert "private reasoning" not in json.dumps(payload) and '"signature"' not in json.dumps(payload)
    assert calls[0].prompt_inputs is None
    assert any(m.provider_state == {"signature": "private"} for m in calls[0].messages)
    restored = await ctx.assembler.compile(payload, template=state.extra["template"])
    assert restored.system == request.system
    assert [(m.role, m.content, m.tool_calls) for m in restored.messages] == [
        (m.role, m.content, m.tool_calls) for m in request.messages]


async def test_compile_failure_emits_no_llm_request_events():
    inputs, state, ctx, events = await setup()
    ctx.assembler.compile = AsyncMock(side_effect=ValueError("missing memory"))
    with pytest.raises(ValueError, match="missing memory"):
        await build_llm_request(ctx, state, inputs)
    assert events == []
