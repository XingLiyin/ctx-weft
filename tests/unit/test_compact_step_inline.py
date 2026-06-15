"""CompactStep runs standalone against state.scope: folds over-budget layers with one summary."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from loomex_core.core.loop.steps.compact import CompactStep
from loomex_core.protocols import MemoryEventType as T, MemoryScope


class _FakeMemory:
    def __init__(self, counts):
        self._counts = counts  # {frozenset(types): n}
        self.applied = []  # (layer, summary)

    async def count_recent(self, scope, types, ctx):
        return self._counts.get(frozenset(types), 0)

    async def apply_compact(self, scope, summary, keep_last, ctx, layer):
        self.applied.append((layer.value, summary))
        return SimpleNamespace(events_before=10, events_after=keep_last,
                               summary_event_id="s1")


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
                           provider_ctx=SimpleNamespace())


async def test_compact_folds_overbudget_layers_with_one_summary():
    # task layer has 5 foldable (> keep_last=2); agent layer has 0
    mem = _FakeMemory({
        frozenset([T.USER_PROMPT, T.LLM_RESPONSE, T.TOOL_RESULT]): 5,
        frozenset([T.TASK_DISPATCH, T.TASK_DISPATCH_RESULT]): 0,
    })
    outcome = await CompactStep().execute(_state(), _ctx(mem))
    assert outcome.next_step is None
    assert [layer for layer, _ in mem.applied] == ["task"]
    assert mem.applied[0][1] == "SUMMARY"


async def test_compact_noop_when_nothing_foldable():
    mem = _FakeMemory({
        frozenset([T.USER_PROMPT, T.LLM_RESPONSE, T.TOOL_RESULT]): 1,
        frozenset([T.TASK_DISPATCH, T.TASK_DISPATCH_RESULT]): 0,
    })
    outcome = await CompactStep().execute(_state(), _ctx(mem))
    assert outcome.next_step is None
    assert mem.applied == []
