"""maybe_compact_before_dispatch: 派发前压缩的门控逻辑（独立 predispatch 阈值）。

门控通过后委派 escalating_compact（L1 agent 折 → L2 降级 → L3 task 坍，按预算升级，与
CompactStep 同构）。本文件聚焦门控：ratio=0 / 未达阈值 / 无 context_limit 时不压；达阈值且
可折时走压缩。两层折叠的端到端验证见 test_compaction.py::test_predispatch_folds_task_and_agent_layers。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from ctx_weft.core.loop.steps.compact import maybe_compact_before_dispatch
from ctx_weft.protocols import MemoryEventType as T, MemoryScope  # noqa: F401
from ctx_weft.providers.llm.tokenizer import HeuristicTokenizer


_BASE_DT = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _FakeMemory:
    def __init__(self, task_count):
        self._task_count = task_count
        self.applied = []    # kept for backward-compat (no longer populated)
        self.ingested = []   # collapsed USER_PROMPT events written by collapse_task_layer
        self.superseded = []

    async def count_recent(self, scope, types, ctx):
        return self._task_count

    async def recall_recent(self, scope, types, limit, ctx):
        # Return empty for agent-layer queries (no root residues in these unit tests)
        if T.AGENT_CONVERSATION_TURN in types:
            return []
        # Return fake USER_PROMPT records (newest-first) for task-layer type queries
        return list(reversed([
            SimpleNamespace(
                id=str(i), type=T.USER_PROMPT, content=f"msg {i}", role="user",
                metadata={},
                timestamp=_BASE_DT + timedelta(seconds=i),
            )
            for i in range(self._task_count)
        ]))

    async def recall_recent_by_agent(self, scope, types, limit, ctx):
        return []

    async def load_view(self, address, scope, ctx, kinds=None):
        # v2：AGENT 视图（root residue 查询）→ 空；TASK 视图 → 升序 user 回合
        from ctx_weft.protocols import MemoryLayer
        from ctx_weft.protocols.memory_compat import MemoryKind
        if scope is MemoryLayer.AGENT:
            return []
        return [
            SimpleNamespace(
                id=str(i), type=T.USER_PROMPT, content=f"msg {i}", role="user",
                kind=MemoryKind.CONVERSATION_TURN, layer=MemoryLayer.TASK,
                address=None, metadata={},
                timestamp=_BASE_DT + timedelta(seconds=i),
            )
            for i in range(self._task_count)
        ]

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
    context_limit = 1_000_000  # apply_dynamic_max_tokens ceiling fallback (Task 2 网关接线)
    tokenizer = HeuristicTokenizer()

    async def complete(self, request, stream=True):
        yield SimpleNamespace(kind="token", text="SUMMARY", usage=None, tool_call=None)


def _state(*, ratio, context_limit=1000, context_tokens=0, keep_last=2, compact_ratio=0.5):
    loop_config = SimpleNamespace(
        predispatch_compact_token_ratio=ratio,
        compact_keep_last=keep_last,
        collapse_keep_last=keep_last,
        # escalating_compact 自身的升级目标比率（与 predispatch 门控阈值独立）。
        compact_token_ratio=compact_ratio,
        compact_target_ratio=0.0,
    )
    loop_guard = SimpleNamespace(context_limit=context_limit, context_tokens=context_tokens)
    agent = SimpleNamespace(id="agt1", loop_config=loop_config, loop_guard=loop_guard,
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


async def test_disabled_when_ratio_zero():
    mem = _FakeMemory(task_count=50)  # plenty foldable, but feature off
    out = await maybe_compact_before_dispatch(_state(ratio=0.0), _ctx(mem), prompt_tokens=999)
    assert out == []
    assert mem.ingested == []
    assert mem.superseded == []


async def test_skips_when_below_threshold():
    mem = _FakeMemory(task_count=50)
    # 500 / 1000 = 0.5 < ratio 0.6
    out = await maybe_compact_before_dispatch(_state(ratio=0.6), _ctx(mem), prompt_tokens=500)
    assert out == []
    assert mem.ingested == []
    assert mem.superseded == []


async def test_skips_when_nothing_foldable():
    """escalating_compact 到达 L3（无 root residue 可折,L1/L2 均跳过）仍会试摘要 + 坍缩,
    但 task_count=2 <= keep_last=2 时 collapse_task_layer 内部判定无可折,不摸任何记录
    （STARTED 事件仍可能发出——预算门本身已过,只是最终无 MemoryCompacted，收尾仍发 Finished）。"""
    mem = _FakeMemory(task_count=2)  # <= keep_last=2 → collapse 内部 noop
    out = await maybe_compact_before_dispatch(_state(ratio=0.6), _ctx(mem), prompt_tokens=800)
    assert [e.type for e in out] == ["MemoryCompactStarted", "MemoryCompactFinished"]  # 无 MemoryCompacted
    assert mem.ingested == []
    assert mem.superseded == []


async def test_compacts_task_layer_once_when_over_threshold_and_foldable():
    mem = _FakeMemory(task_count=10)  # > keep_last=2
    out = await maybe_compact_before_dispatch(_state(ratio=0.6), _ctx(mem), prompt_tokens=800)
    # task layer collapsed once via collapse_task_layer → one ingested collapsed USER_PROMPT
    assert len(mem.ingested) == 1
    assert mem.ingested[0].metadata.get("collapsed") is True
    assert "SUMMARY" in mem.ingested[0].content
    # 发 started + compacted + finished（收尾聚合），均标 pre_dispatch
    assert [e.type for e in out] == ["MemoryCompactStarted", "MemoryCompacted", "MemoryCompactFinished"]
    assert [e.payload.get("trigger") for e in out] == ["pre_dispatch", "pre_dispatch", "pre_dispatch"]
    assert out[1].payload["layer"] == "task"
    assert out[1].payload["superseded_count"] == 8  # 10 - collapse_keep_last(2)


async def test_falls_back_to_loop_guard_tokens_when_prompt_tokens_zero():
    mem = _FakeMemory(task_count=10)
    out = await maybe_compact_before_dispatch(
        _state(ratio=0.6, context_tokens=800), _ctx(mem), prompt_tokens=0)
    # fallback to context_tokens=800 triggers compact → collapsed USER_PROMPT ingested
    assert len(mem.ingested) == 1
    assert mem.ingested[0].metadata.get("collapsed") is True
    assert len(out) == 3  # started + compacted + finished（收尾聚合）


async def test_skips_when_no_context_limit():
    mem = _FakeMemory(task_count=10)
    out = await maybe_compact_before_dispatch(
        _state(ratio=0.6, context_limit=0), _ctx(mem), prompt_tokens=800)
    assert out == []
    assert mem.ingested == []
    assert mem.superseded == []


async def test_predispatch_uses_escalating_compact(monkeypatch):
    """端到端接线断言：门控通过后 maybe_compact_before_dispatch 委派 escalating_compact，
    并原样透传 token_estimate + trigger="pre_dispatch"（Task 6 已接线，本用例防回归）。"""
    from ctx_weft.core.loop.steps import compact as cm

    seen = {}

    async def _fake_esc(state, ctx, *, token_estimate, trigger):
        seen["trigger"] = trigger
        seen["est"] = token_estimate
        return []

    monkeypatch.setattr(cm, "escalating_compact", _fake_esc)
    mem = _FakeMemory(task_count=10)
    out = await cm.maybe_compact_before_dispatch(
        _state(ratio=0.5), _ctx(mem), prompt_tokens=800)
    assert seen["trigger"] == "pre_dispatch"
    assert seen["est"] == 800
    assert out == []
