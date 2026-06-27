"""USER_PROMPT anchor protection: max_turns/_compact_scope must not fold USER_PROMPT events.

Task layer contains [UP1, llm, tool, UP2(HITL), llm].
After _maybe_compact_task (max_turns exit) AND after _compact_scope:
- Both USER_PROMPT events survive un-superseded
- LLM/TOOL events are folded (superseded)
"""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.loop.steps.compact import CompactStep, _compact_scope
from ctx_weft.core.loop.steps.observe import ObserveStep, Verdict
from ctx_weft.protocols import MemoryEventType, MemoryScope


class _FakeMemProtect:
    """Records protect_types passed to apply_compact."""

    def __init__(self, count=10):
        self._count = count
        self.applied = []  # list of dicts with call kwargs

    async def count_recent(self, scope, types, ctx):
        return self._count

    async def recall_recent(self, scope, types, limit, ctx):
        return []

    async def recall_recent_by_agent(self, scope, types, limit, ctx):
        return []

    async def apply_compact(self, scope, summary, keep_last, ctx, layer, protect_types=()):
        self.applied.append({
            "layer": layer.value,
            "summary": summary,
            "keep_last": keep_last,
            "protect_types": protect_types,
        })
        return SimpleNamespace(events_before=10, events_after=keep_last, summary_event_id="s1")


class _FakeAssembler:
    async def assemble(self, request):
        return SimpleNamespace(system="SYS", messages=[], tools=[])


class _FakeLLM:
    async def complete(self, request, stream=True):
        yield SimpleNamespace(kind="token", text="SUMMARY", usage=None, tool_call=None)


def _state(exit_reason="max_turns"):
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
        task=SimpleNamespace(id="t1", process_report=""),
        scope=MemoryScope(session_id="s1", task_id="t1", agent_id="a1"),
        transcript=[],
        extra={"template": None, "bound_capabilities": []},
        act_exit_reason=exit_reason,
        context_limit=10000,
        token_ratio=0.9,
    )


def _ctx(mem):
    return SimpleNamespace(
        memory=mem,
        assembler=_FakeAssembler(),
        llm=_FakeLLM(),
        provider_ctx=SimpleNamespace(),
        task_manager=None,
    )


async def test_maybe_compact_task_passes_protect_types():
    """_maybe_compact_task (max_turns) must pass protect_types=(USER_PROMPT,) to apply_compact."""
    mem = _FakeMemProtect(count=10)
    events = []
    verdict = Verdict(task_outcome="retry", summary="REPORT", reported=True)
    await ObserveStep()._maybe_compact_task(_state("max_turns"), _ctx(mem), verdict, events)

    assert len(mem.applied) == 1
    call = mem.applied[0]
    assert call["layer"] == "task"
    assert MemoryEventType.USER_PROMPT in call["protect_types"], (
        f"USER_PROMPT not in protect_types: {call['protect_types']}"
    )


async def test_compact_scope_passes_protect_types():
    """_compact_scope (token-ratio / context_limit path) must pass protect_types=(USER_PROMPT,)."""
    mem = _FakeMemProtect(count=10)
    state = _state("normal")
    ctx = _ctx(mem)
    await _compact_scope(state, ctx, trigger="test")

    task_calls = [c for c in mem.applied if c["layer"] == "task"]
    assert task_calls, "No task-layer apply_compact call found"
    call = task_calls[0]
    assert MemoryEventType.USER_PROMPT in call["protect_types"], (
        f"USER_PROMPT not in protect_types: {call['protect_types']}"
    )
