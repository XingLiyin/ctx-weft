"""CompactStep runs standalone against state.scope: folds over-budget layers with one summary."""

from __future__ import annotations

from tests.model_testkit import model_snapshot

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from ctx_weft.core.loop.steps.compact import CompactStep
from ctx_weft.protocols import MemoryAddress, MemoryEvent, MemoryKind, MemoryScope
from ctx_weft.protocols import MemoryEventType as T
from ctx_weft.providers.llm.tokenizer import HeuristicTokenizer
from tests.assembler_testkit import compiled_test_inputs, scripted_inputs

_BASE_DT = datetime(2026, 1, 1, tzinfo=timezone.utc)


from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from ctx_weft.protocols import ProviderContext


class _FakeMemory(InMemoryMemoryProvider):
    """Use the real organize implementation; record actual committed changes."""
    def __init__(self, counts):
        super().__init__()
        self._counts = counts
        self._seeded = False
        self.ingested = []
        self.superseded = []

    async def load_view_ids(self, address, scope, ctx, kinds=None, **kwargs):
        if not self._seeded:
            self._seeded = True
            n = max((v for k, v in self._counts.items() if T.AGENT_CONVERSATION_TURN not in k), default=0)
            for i in range(n):
                await super().ingest(MemoryEvent(id=f"seed-{i}", kind=MemoryKind.CONVERSATION_TURN,
                    scope=MemoryScope.TASK, address=MemoryAddress(session_id="s1", agent_id="agt1", task_id="t1"),
                    content=f"msg {i}", role="user", timestamp=_BASE_DT + timedelta(seconds=i)), ctx)
        return await super().load_view_ids(address, scope, ctx, kinds, **kwargs)

    async def _commit_organize(self, ids, events, ctx):
        added = await super()._commit_organize(ids, events, ctx)
        if added is not None:
            self.superseded.extend(ids)
            self.ingested.extend(events)
        return added


class _FakeAssembler:
    async def compile(self, inputs, **kwargs):
        return await compiled_test_inputs(inputs, **kwargs)

    async def assemble(self, request):
        return scripted_inputs(SimpleNamespace(system="SYS", messages=[], tools=[]), purpose=request.purpose)


class _FakeLLM:
    context_limit = 1_000_000  # apply_dynamic_max_tokens ceiling fallback (Task 2 网关接线)
    tokenizer = HeuristicTokenizer()

    async def complete(self, request, stream=True):
        yield SimpleNamespace(kind="token", text="SUMMARY", usage=None, tool_call=None)


def _state(context_tokens=1000):
    agent = SimpleNamespace(
        id="agt1",
        loop_config=SimpleNamespace(
            compact_keep_last=2, collapse_keep_last=2,
            compact_token_ratio=0.1, compact_target_ratio=0.0),
        loop_guard=SimpleNamespace(context_limit=1000, context_tokens=context_tokens),
        runtime={"llm_model": "mock"})
    return SimpleNamespace(
        run_id="r1",
        agent=agent,
        session=SimpleNamespace(id="s1", tenant_id="default"),
        task=SimpleNamespace(id="t1"),
        scope=MemoryAddress(session_id="s1", task_id="t1", agent_id="agt1"),
        transcript=[],
        sequence_counter=0,
        extra={"template": None, "bound_capabilities": []},
        # resolve_llm_identity 的真值来源（task-4 复审第二轮：summarize_for_compact
        # 不再读 agent.runtime.get("llm_model")，那个键正常任务执行永远不会被写入）。
        resolved_model=model_snapshot(model="mock", account="", context_limit=1000),
    )


def _ctx(memory):
    return SimpleNamespace(memory=memory, assembler=_FakeAssembler(), llm=_FakeLLM(),
                           provider_ctx=ProviderContext(session_id="s1", agent_id="agt1", task_id="t1"), task_manager=None)


async def test_compact_folds_overbudget_layers_with_one_summary():
    # task layer has 5 foldable (> keep_last=2); agent layer has 0
    # L3 guard 计数用 _TASK_LAYER_TYPES（含 TOOL_INVOCATION/TASK_COMPACT_SUMMARY）——key 须匹配全集
    mem = _FakeMemory({
        frozenset([T.USER_PROMPT, T.LLM_RESPONSE, T.TOOL_INVOCATION,
                   T.TOOL_RESULT, T.TASK_COMPACT_SUMMARY]): 5,
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
    from tests.compaction_testkit import summarize_for_compact
    out = await summarize_for_compact(_state(), _ctx(_FakeMemory({})))
    # spec: compact-fidelity——返回 ParsedDigest；散文输出整文落库 + degraded。
    assert out.text == "SUMMARY" and out.degraded is True
