"""maybe_compact_before_dispatch: 派发前压缩的门控逻辑（独立 predispatch 阈值）。

门控通过后委派 _compact_scope（task 层 + agent 层，与 PrepareStep 同构）。本文件聚焦门控：
ratio=0 / 未达阈值 / 无 context_limit 时不压；达阈值且可折时走压缩。两层折叠的端到端验证见
test_compaction.py::test_predispatch_folds_task_and_agent_layers。
"""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.loop.steps.compact import maybe_compact_before_dispatch
from ctx_weft.protocols import MemoryEventType as T, MemoryScope  # noqa: F401


class _FakeMemory:
    def __init__(self, task_count):
        self._task_count = task_count
        self.applied = []  # (layer, summary)

    async def count_recent(self, scope, types, ctx):
        return self._task_count

    async def recall_recent(self, scope, types, limit, ctx):
        return []

    async def recall_recent_by_agent(self, scope, types, limit, ctx):
        return []

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


def _state(*, ratio, context_limit=1000, context_tokens=0, keep_last=2):
    loop_config = SimpleNamespace(
        predispatch_compact_token_ratio=ratio,
        compact_keep_last=keep_last,
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
    assert mem.applied == []


async def test_skips_when_below_threshold():
    mem = _FakeMemory(task_count=50)
    # 500 / 1000 = 0.5 < ratio 0.6
    out = await maybe_compact_before_dispatch(_state(ratio=0.6), _ctx(mem), prompt_tokens=500)
    assert out == []
    assert mem.applied == []


async def test_skips_when_nothing_foldable():
    mem = _FakeMemory(task_count=2)  # <= keep_last=2 → 不空跑 LLM
    out = await maybe_compact_before_dispatch(_state(ratio=0.6), _ctx(mem), prompt_tokens=800)
    assert out == []
    assert mem.applied == []


async def test_compacts_task_layer_once_when_over_threshold_and_foldable():
    mem = _FakeMemory(task_count=10)  # > keep_last=2
    out = await maybe_compact_before_dispatch(_state(ratio=0.6), _ctx(mem), prompt_tokens=800)
    # 只折 task 层一次，摘要来自 LLM
    assert [layer for layer, _ in mem.applied] == ["task"]
    assert mem.applied[0][1] == "SUMMARY"
    # 发 started + compacted，均标 pre_dispatch
    assert [e.type for e in out] == ["MemoryCompactStarted", "MemoryCompacted"]
    assert [e.payload.get("trigger") for e in out] == ["pre_dispatch", "pre_dispatch"]
    assert out[1].payload["layer"] == "task"
    assert out[1].payload["used_llm"] is True


async def test_falls_back_to_loop_guard_tokens_when_prompt_tokens_zero():
    mem = _FakeMemory(task_count=10)
    out = await maybe_compact_before_dispatch(
        _state(ratio=0.6, context_tokens=800), _ctx(mem), prompt_tokens=0)
    assert [layer for layer, _ in mem.applied] == ["task"]
    assert len(out) == 2


async def test_skips_when_no_context_limit():
    mem = _FakeMemory(task_count=10)
    out = await maybe_compact_before_dispatch(
        _state(ratio=0.6, context_limit=0), _ctx(mem), prompt_tokens=800)
    assert out == []
    assert mem.applied == []
