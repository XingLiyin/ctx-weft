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
from ctx_weft.protocols import MemoryScope


class _FakeMem:
    def __init__(self, count):
        self._count = count
        self.applied = []  # (layer, summary, keep_last)

    async def count_recent(self, scope, types, ctx):
        return self._count

    async def apply_compact(self, scope, summary, keep_last, ctx, layer, protect_types=()):
        self.applied.append((layer.value, summary, keep_last))
        return SimpleNamespace(events_before=10, events_after=keep_last, summary_event_id="s1")


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
        scope=MemoryScope(session_id="s1", task_id="t1", agent_id="a1"),
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
