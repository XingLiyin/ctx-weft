"""ObserveStep compacts the task layer on max_turns exit (spec 2026-06-22)."""

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


class _FakeAssembler:
    async def assemble(self, request):
        return SimpleNamespace(system="SYS", messages=[], tools=[])


class _FakeLLM:
    async def complete(self, request, stream=True):
        yield SimpleNamespace(kind="token", text="FRESH", usage=None, tool_call=None)


class _EmptyLLM:
    async def complete(self, request, stream=True):
        return
        yield  # makes this an empty async generator


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
    return SimpleNamespace(
        memory=mem, assembler=_FakeAssembler(), llm=_FakeLLM(), provider_ctx=SimpleNamespace()
    )


def _ctx_empty_llm(mem):
    return SimpleNamespace(
        memory=mem, assembler=_FakeAssembler(), llm=_EmptyLLM(), provider_ctx=SimpleNamespace()
    )


async def test_max_turns_reuses_reported_summary():
    # 本轮真上报：复用 verdict.act_recap（可信 report），不调 summarize_for_compact
    mem = _FakeMem(count=10)
    events = []
    verdict = Verdict(task_outcome="retry", act_recap="REPORT", reported=True)
    await ObserveStep()._maybe_compact_task(_state("max_turns", "REPORT"), _ctx(mem), verdict, events)
    assert mem.applied == [("task", "REPORT", 2)]
    assert [e.type for e in events] == [EventType.MEMORY_COMPACT_STARTED, EventType.MEMORY_COMPACTED]


async def test_max_turns_uses_dedicated_summary_when_not_reported():
    # 本轮未上报（规则降级）：用 summarize_for_compact，不退薄 verdict.summary
    mem = _FakeMem(count=10)
    events = []
    verdict = Verdict(task_outcome="retry", act_recap="thin rule text", reported=False)
    await ObserveStep()._maybe_compact_task(_state("max_turns", ""), _ctx(mem), verdict, events)
    assert mem.applied == [("task", "FRESH", 2)]
    assert [e.type for e in events] == [EventType.MEMORY_COMPACT_STARTED, EventType.MEMORY_COMPACTED]


async def test_max_turns_ignores_stale_persisted_process_report():
    # 关键回归：未上报时，即使持久 process_report 非空（陈旧），也不复用它 —— 走 summarize_for_compact
    mem = _FakeMem(count=10)
    events = []
    verdict = Verdict(task_outcome="retry", act_recap="thin rule text", reported=False)
    await ObserveStep()._maybe_compact_task(_state("max_turns", "STALE-PRIOR-ROUND"), _ctx(mem), verdict, events)
    assert mem.applied == [("task", "FRESH", 2)]


async def test_no_compact_when_task_layer_at_or_below_keep_last():
    mem = _FakeMem(count=2)  # <= compact_keep_last
    events = []
    verdict = Verdict(task_outcome="retry", act_recap="REPORT", reported=True)
    await ObserveStep()._maybe_compact_task(_state("max_turns", "REPORT"), _ctx(mem), verdict, events)
    assert mem.applied == []
    assert events == []


async def test_no_compact_on_non_max_turns_exit():
    mem = _FakeMem(count=10)
    events = []
    verdict = Verdict(task_outcome="retry", act_recap="REPORT", reported=True)
    await ObserveStep()._maybe_compact_task(_state("normal", "REPORT"), _ctx(mem), verdict, events)
    assert mem.applied == []
    assert events == []


async def test_max_turns_ultimate_fallback_placeholder_when_not_reported_and_empty_llm():
    # 未上报 + LLM 无 token → summarize_for_compact 返回 "" → 落到 "[Context compacted]"
    mem = _FakeMem(count=10)
    events = []
    verdict = Verdict(task_outcome="retry", act_recap="thin rule text", reported=False)
    await ObserveStep()._maybe_compact_task(_state("max_turns", ""), _ctx_empty_llm(mem), verdict, events)
    assert mem.applied == [("task", "[Context compacted]", 2)]
