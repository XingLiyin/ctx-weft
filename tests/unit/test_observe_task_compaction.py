"""ObserveStep folds the current attempt's task-layer raw when verdict.task_outcome=="retry"
(spec 2026-07-01 reshape of the old max_turns-only _maybe_compact_task).

New contract: _fold_retry_segment is unconditional (no keep_last gate) whenever
task_outcome=="retry" — always keep_last=0, always reuse verdict.act_recap (no dedicated
summarize_for_compact LLM call). Non-retry outcomes are a no-op.
"""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.events import EventType
from ctx_weft.core.loop.steps.observe import ObserveStep, Verdict
from ctx_weft.protocols import MemoryAddress


class _FakeMem:
    """v2：retry 段折经 segment_fold → load_view + fold；applied 记 (layer, summary, keep_last)。"""

    def __init__(self, count):
        self._count = count
        self.applied = []  # (layer, summary, keep_last)

    async def load_view(self, address, scope, ctx, kinds=None):
        from datetime import datetime, timezone
        from ctx_weft.protocols import MemoryKind, MemoryScope, MemoryRecord
        if scope is not MemoryScope.TASK or self._count <= 0:
            return []
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        return [
            MemoryRecord(id="up", type=None, content="u", timestamp=t0, role="user",
                         kind=MemoryKind.CONVERSATION_TURN, layer=MemoryScope.TASK),
            MemoryRecord(id="a1", type=None, content="x",
                         timestamp=t0.replace(minute=1), role="assistant",
                         kind=MemoryKind.CONVERSATION_TURN, layer=MemoryScope.TASK),
        ]

    async def fold(self, supersede_ids, replacements, ctx):
        for ev in replacements:
            self.applied.append((ev.layer.value, ev.content, ev.metadata.get("keep_last", 0)))
        return [f"s{i+1}" for i in range(len(replacements))]


def _state(exit_reason, process_report):
    agent = SimpleNamespace(
        id="a1",
        loop_config=SimpleNamespace(compact_keep_last=2),
        runtime={"llm_model": "mock"},
    )
    return SimpleNamespace(
        run_id="r1",
        sequence_counter=0,
        agent=agent,
        session=SimpleNamespace(id="s1", tenant_id="default"),
        task=SimpleNamespace(id="t1", process_report=process_report),
        scope=MemoryAddress(session_id="s1", task_id="t1", agent_id="a1"),
        transcript=[],
        extra={"template": None, "bound_capabilities": []},
        act_exit_reason=exit_reason,
    )


def _ctx(mem):
    return SimpleNamespace(memory=mem, provider_ctx=SimpleNamespace())


async def test_retry_reuses_act_recap_unconditionally():
    # 本轮真上报：复用 verdict.act_recap（可信 report）；keep_last 固定 0，不看 compact_keep_last
    mem = _FakeMem(count=10)
    events = []
    verdict = Verdict(task_outcome="retry", act_recap="REPORT", reported=True)
    await ObserveStep()._fold_retry_segment(_state("max_turns", "REPORT"), _ctx(mem), verdict, events)
    assert mem.applied == [("task", "REPORT", 0)]
    assert [e.type for e in events] == [EventType.MEMORY_COMPACTED]


async def test_retry_reuses_act_recap_even_when_not_reported():
    # 规则降级（未上报）：仍复用 verdict.act_recap，不再调 summarize_for_compact
    mem = _FakeMem(count=10)
    events = []
    verdict = Verdict(task_outcome="retry", act_recap="thin rule text", reported=False)
    await ObserveStep()._fold_retry_segment(_state("max_turns", ""), _ctx(mem), verdict, events)
    assert mem.applied == [("task", "thin rule text", 0)]


async def test_retry_folds_regardless_of_layer_size():
    # 新契约无 keep_last 门控：即使可折条数很少也无条件折
    mem = _FakeMem(count=1)
    events = []
    verdict = Verdict(task_outcome="retry", act_recap="REPORT", reported=True)
    await ObserveStep()._fold_retry_segment(_state("max_turns", "REPORT"), _ctx(mem), verdict, events)
    assert mem.applied == [("task", "REPORT", 0)]


async def test_retry_folds_on_context_limit_and_observer_retry_too():
    # 三来源：max_turns/context_limit/observer-retry 均由 task_outcome=="retry" 统一驱动
    mem = _FakeMem(count=10)
    events = []
    verdict = Verdict(task_outcome="retry", act_recap="ctx-limit recap", reported=True)
    await ObserveStep()._fold_retry_segment(_state("context_limit", "REPORT"), _ctx(mem), verdict, events)
    assert mem.applied == [("task", "ctx-limit recap", 0)]


async def test_no_fold_on_non_retry_outcome():
    mem = _FakeMem(count=10)
    events = []
    verdict = Verdict(task_outcome="success", act_recap="REPORT", reported=True)
    await ObserveStep()._fold_retry_segment(_state("normal", "REPORT"), _ctx(mem), verdict, events)
    assert mem.applied == []
    assert events == []


async def test_retry_skips_fold_when_act_recap_empty():
    # 空 recap 不再写 "[Context compacted]" 占位折叠：保 raw、不折、不发事件
    mem = _FakeMem(count=10)
    events = []
    verdict = Verdict(task_outcome="retry", act_recap="", reported=False)
    await ObserveStep()._fold_retry_segment(_state("max_turns", ""), _ctx(mem), verdict, events)
    assert mem.applied == []
    assert events == []
