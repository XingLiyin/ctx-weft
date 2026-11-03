"""Real foreground transitions preserve pending tools and rebuild only once."""
from contextlib import aclosing
from types import SimpleNamespace as NS

import pytest

from ctx_weft.core.loop.driver import ACT_TURNS_USED_KEY, COMPACT_NOOP_KEY, StepDriver
from ctx_weft.core.loop.steps import act as act_mod
from ctx_weft.core.loop.steps.act import ActStep, PendingActTurn, TurnRecord
from ctx_weft.core.loop.steps.compact import CompactStep, collapse_task_layer
from ctx_weft.core.loop.steps.prepare import PrepareStep
from ctx_weft.core.utils.clock import now_utc
from ctx_weft.protocols import (
    LLMChunk, LLMMessage, LLMUsage, MemoryEvent, MemoryKind, MemoryScope, ToolCall, load_records,
)
from ctx_weft.protocols.events import EventType
from ctx_weft.providers.llm.tokenizer import HeuristicTokenizer
from tests.assembler_testkit import compiled_test_inputs, scripted_inputs
from tests.unit.test_llm_event_convergence import _prepare_compact_transition


class ScriptedLLM:
    tokenizer = HeuristicTokenizer()

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def complete(self, request, stream=True):
        self.requests.append(request)
        for chunk in next(self.responses):
            yield chunk


@pytest.mark.parametrize("suspend, max_turns", [(False, 2), (True, 2), (False, 1)])
async def test_predispatch_compact_resumes_original_tools_once(monkeypatch, suspend, max_turns, caplog):
    from ctx_weft.core.loop.capability_gateway import DISPATCH_TOOLS

    calls = [ToolCall(id="dispatch", name=next(iter(DISPATCH_TOOLS)), arguments={}),
             ToolCall(id="work", name="test__work", arguments={"x": 1})]
    llm = ScriptedLLM([
        [*(LLMChunk(kind="tool_call", tool_call=tc) for tc in calls),
         LLMChunk(kind="usage", usage=LLMUsage(prompt_tokens=60_000, completion_tokens=7))],
        [LLMChunk(kind="token", text="COMPACTED HISTORY")],
        [LLMChunk(kind="token", text="done"),
         LLMChunk(kind="usage", usage=LLMUsage(prompt_tokens=20, completion_tokens=5))],
    ])
    state, ctx, bus, _ = await _prepare_compact_transition(monkeypatch, llm=llm)
    state.task.parent_task_id = None
    state.task.title = "test"
    state.task.user_prompt_in_memory = True
    state.task.user_prompt = None
    state.task.switch_requested = False
    state.task.actor_done = False
    state.task.suspend_requested = False
    state.task.unattended = True
    state.agent.loop_config.max_turns_per_act = max_turns
    state.agent.loop_config.predispatch_compact_token_ratio = 0.5
    state.agent.loop_config.collapse_keep_last = 0

    async def estimate(*args):
        return 0, False

    monkeypatch.setattr(PrepareStep, "_estimate_tokens", estimate)
    assembled = []

    async def assemble(request):
        records = await load_records(ctx.memory, state.scope, MemoryScope.TASK, ctx.provider_ctx)
        records = [r for r in records if r.id not in request.extra.get("exclude_memory_ids", [])]
        assembled.append((request.purpose, [r.content for r in records]))
        return scripted_inputs(NS(system="SYS", messages=[LLMMessage(
            role=r.role, content=r.content, tool_calls=r.metadata.get("tool_calls", []),
            tool_call_id=r.metadata.get("tool_call_id"), memory_id=r.id,
        ) for r in records], tools=[], token_count=10), purpose=request.purpose)

    ctx.assembler = NS(assemble=assemble, compile=compiled_test_inputs)
    invoked = []

    async def execute_tools(current, local_ctx, tool_calls):
        assert len(llm.requests) == 2  # actor once, summary once; no regenerated tool turn
        records = await load_records(ctx.memory, state.scope, MemoryScope.TASK, ctx.provider_ctx)
        assert any(r.metadata.get("collapsed") for r in records)
        assert any(r.id == current.pending_act_turn.assistant_record_id for r in records)
        invoked.extend(tool_calls)
        results = []
        for tc in tool_calls:
            rid = await ctx.memory.ingest(MemoryEvent(
                kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK, address=state.scope,
                role="tool", content="tool output", timestamp=now_utc(),
                metadata={"tool_call_id": tc.id},
            ), ctx.provider_ctx)
            results.append({"tool_call_id": tc.id, "result": "tool output", "memory_id": rid})
        current.task.suspend_requested = suspend
        return results

    monkeypatch.setattr(act_mod, "_execute_tool_calls", execute_tools)
    driver = StepDriver({"prepare": PrepareStep(), "compact": CompactStep(), "act": ActStep()})
    outcomes = []
    async with aclosing(driver.run(state, ctx)) as chain:
        async for outcome in chain:
            outcomes.append(outcome)
            if outcome.next_step in {"observe", "suspend"}:
                break

    assert [out.next_step for out in outcomes] == [
        "act", "compact", "prepare", "act", "suspend" if suspend else "observe"]
    assert len(invoked) == 2 and len({tc.id for tc in invoked}) == 2
    assert [tc.name for tc in invoked] == [tc.name for tc in calls]
    finished_text = not suspend and max_turns > 1
    assert state.extra[ACT_TURNS_USED_KEY] == (2 if finished_text else 1)
    assert state.session.token_used == (60_032 if finished_text else 60_007)
    transcript = outcomes[-1].state_patch["transcript"]
    assert [record.turn for record in transcript] == ([1, 2] if finished_text else [1])
    assert len(transcript[0].tool_results) == 2
    assert outcomes[-1].state_patch["pending_act_turn"] is None
    assert len(llm.requests) == (3 if finished_text else 2)
    assert [purpose for purpose, _ in assembled] == ["act", "compact", "act"]
    assert "COMPACTED HISTORY" in " ".join(assembled[-1][1])
    assert "dangling tool_call" not in caplog.text
    assert not any(e.type == EventType.STEP_FAILED for e in bus.events)
    if not suspend:
        assert outcomes[-1].next_step_options == {"boundary": "normal" if finished_text else "max_turns", "judge_task": True}


async def test_pending_dispatch_assistant_survives_zero_keep_last(monkeypatch):
    state, ctx, _, _ = await _prepare_compact_transition(monkeypatch)
    # Dispatch calls are deliberately omitted from the task message metadata.
    record = MemoryEvent(kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK,
                         address=state.scope, role="assistant", content="dispatching", timestamp=now_utc())
    rid = await ctx.memory.ingest(record, ctx.provider_ctx)
    state.pending_act_turn = PendingActTurn(TurnRecord(1, [], "dispatching"), rid, False)
    from ctx_weft.core.assembler import ContextRequest
    from ctx_weft.core.assembler.sources.agent_recall import AgentRecallSource
    request = ContextRequest(purpose="compact", scope=state.scope,
                             task=state.task, agent=state.agent, session=state.session,
                             template=None, bound_capabilities=[], resolved_model=state.resolved_model,
                             extra={"exclude_memory_ids": [rid]})
    recalled = [block async for block in AgentRecallSource().fetch(request, ctx)]
    assert recalled and rid not in {block.memory_id for block in recalled}
    await collapse_task_layer(state, ctx, 0, "SUMMARY")
    records = await load_records(ctx.memory, state.scope, MemoryScope.TASK, ctx.provider_ctx)
    assert any(r.id == rid for r in records)
    assert any(r.metadata.get("collapsed") for r in records)


async def test_compact_noop_returns_to_prepare_once(monkeypatch):
    state, ctx, bus, driver = await _prepare_compact_transition(monkeypatch)
    state.agent.loop_config.collapse_keep_last = 20
    state, first = await driver._execute_foreground("prepare", state, ctx)
    state, folded = await driver._execute_foreground("compact", state, ctx, options=first.next_step_options)
    state, rebuilt = await driver._execute_foreground("prepare", state, ctx, options=folded.next_step_options)
    assert folded.next_step == "prepare" and rebuilt.next_step == "act"
    assert state.extra[COMPACT_NOOP_KEY]
    assert not any(e.type == EventType.LLM_REQUEST_STARTED for e in bus.events)
    assert state.agent.loop_guard.context_tokens == 0


@pytest.mark.parametrize("after", ["act", "observe", "", False])
async def test_compact_rejects_invalid_continuation_before_mutation(monkeypatch, after):
    state, ctx, _, driver = await _prepare_compact_transition(monkeypatch)
    state.agent.loop_guard.context_tokens = 123
    with pytest.raises(ValueError, match="continuation"):
        await driver.execute("compact", state, ctx, options={"after": after})
    assert state.agent.loop_guard.context_tokens == 123


async def test_failed_compact_invalidates_old_baseline(monkeypatch):
    from ctx_weft.core.loop.steps import compact as cm

    state, ctx, _, driver = await _prepare_compact_transition(monkeypatch)
    state.agent.loop_guard.context_tokens = 90_000
    state.agent.loop_guard.context_message_count = 5

    async def fail(*args, **kwargs):
        raise RuntimeError("later compact level failed")

    monkeypatch.setattr(cm, "escalating_compact", fail)
    with pytest.raises(RuntimeError):
        await driver.execute("compact", state, ctx, options={"after": "prepare"})
    assert state.agent.loop_guard.context_tokens == 0
    assert state.agent.loop_guard.context_message_count == 0
