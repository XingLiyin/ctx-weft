"""CompactStep runs standalone against state.scope: folds over-budget layers with one summary."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ctx_weft.core.loop.steps.compact import CompactStep
from ctx_weft.protocols import MemoryEventType as T, MemoryScope


_BASE_DT = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _FakeMemory:
    def __init__(self, counts):
        self._counts = counts  # {frozenset(types): n}
        self.applied = []    # kept for backward-compat (no longer populated)
        self.ingested = []   # collapsed USER_PROMPT events written by collapse_task_layer
        self.superseded = []

    async def count_recent(self, scope, types, ctx):
        return self._counts.get(frozenset(types), 0)

    async def recall_recent(self, scope, types, limit, ctx):
        # Return empty for agent-layer queries (no root residues in these unit tests)
        if T.AGENT_CONVERSATION_TURN in types:
            return []
        # For task-layer queries: find the max count among sets intersecting the requested types
        max_n = max(
            (v for k, v in self._counts.items() if k & frozenset(types)),
            default=0,
        )
        if max_n == 0:
            return []
        # Return fake USER_PROMPT records newest-first (as InMemoryMemoryProvider does)
        return list(reversed([
            SimpleNamespace(
                id=str(i), type=T.USER_PROMPT, content=f"msg {i}", role="user",
                metadata={},
                timestamp=_BASE_DT + timedelta(seconds=i),
            )
            for i in range(max_n)
        ]))

    async def recall_recent_by_agent(self, scope, types, limit, ctx):
        return []  # no cross-task records by default

    async def apply_compact(self, scope, summary, keep_last, ctx, layer, protect_types=()):
        # No longer called by _compact_scope; kept for interface completeness
        self.applied.append((layer.value, summary))
        return SimpleNamespace(events_before=10, events_after=keep_last,
                               summary_event_id="s1")

    async def supersede(self, ids, ctx):
        self.superseded.extend(ids)

    async def ingest(self, event, ctx):
        self.ingested.append(event)
        return event


class _FakeAssembler:
    async def assemble(self, request):
        return SimpleNamespace(system="SYS", messages=[], tools=[])


class _FakeLLM:
    async def complete(self, request, stream=True):
        yield SimpleNamespace(kind="token", text="SUMMARY", usage=None, tool_call=None)


def _state():
    agent = SimpleNamespace(id="agt1", loop_config=SimpleNamespace(compact_keep_last=2),
                            runtime={"llm_model": "mock"})
    return SimpleNamespace(
        run_id="r1",
        agent=agent,
        session=SimpleNamespace(id="s1", tenant_id="default"),
        task=SimpleNamespace(id="t1"),
        scope=MemoryScope(session_id="s1", task_id="t1", agent_id="agt1"),
        transcript=[],
        sequence_counter=0,
        extra={"template": None, "bound_capabilities": []},
    )


def _ctx(memory):
    return SimpleNamespace(memory=memory, assembler=_FakeAssembler(), llm=_FakeLLM(),
                           provider_ctx=SimpleNamespace(), task_manager=None)


async def test_compact_folds_overbudget_layers_with_one_summary():
    # task layer has 5 foldable (> keep_last=2); agent layer has 0
    mem = _FakeMemory({
        frozenset([T.USER_PROMPT, T.LLM_RESPONSE, T.TOOL_RESULT]): 5,
        frozenset([T.TASK_DISPATCH, T.TASK_DISPATCH_RESULT]): 0,
    })
    outcome = await CompactStep().execute(_state(), _ctx(mem))
    assert outcome.next_step is None
    # task layer collapsed via collapse_task_layer → one collapsed USER_PROMPT ingested (not apply_compact)
    assert len(mem.ingested) == 1
    assert mem.ingested[0].metadata.get("collapsed") is True
    assert "SUMMARY" in mem.ingested[0].content


async def test_compact_noop_when_nothing_foldable():
    mem = _FakeMemory({
        frozenset([T.USER_PROMPT, T.LLM_RESPONSE, T.TOOL_RESULT]): 1,
        frozenset([T.TASK_DISPATCH, T.TASK_DISPATCH_RESULT]): 0,
    })
    outcome = await CompactStep().execute(_state(), _ctx(mem))
    assert outcome.next_step is None
    assert mem.ingested == []
    assert mem.superseded == []


async def test_summarize_for_compact_returns_llm_text():
    from ctx_weft.core.loop.steps.compact import summarize_for_compact
    out = await summarize_for_compact(_state(), _ctx(_FakeMemory({})))
    assert out == "SUMMARY"
