"""maybe_compact_before_dispatch: 派发前压缩的门控逻辑（独立 predispatch 阈值）。

门控通过后委派 escalating_compact（L1 agent 折 → L2 降级 → L3 task 坍，按预算升级，与
CompactStep 同构）。本文件聚焦门控：ratio=0 / 未达阈值 / 无 context_limit 时不压；达阈值且
可折时走压缩。两层折叠的端到端验证见 test_compaction.py::test_predispatch_folds_task_and_agent_layers。
"""

from __future__ import annotations

from tests.model_testkit import model_snapshot

from datetime import datetime, timedelta, timezone
import pytest
from types import SimpleNamespace

from ctx_weft.core.loop.steps.act import _predispatch_compact_options
from ctx_weft.core.loop.steps.compact import CompactStep
from ctx_weft.core.loop.driver import StepExecution
from ctx_weft.core.loop.capability_gateway import DISPATCH_TOOLS
from ctx_weft.protocols import LLMUsage, ToolCall
from ctx_weft.protocols import MemoryAddress
from ctx_weft.protocols import MemoryEventType as T  # noqa: F401
from ctx_weft.providers.llm.tokenizer import HeuristicTokenizer
from tests.assembler_testkit import compiled_test_inputs, scripted_inputs

async def _compact_before_dispatch(state, ctx, *, prompt_tokens):
    calls = [ToolCall(id="dispatch", name=next(iter(DISPATCH_TOOLS)), arguments={})]
    options = _predispatch_compact_options(state, calls, LLMUsage(prompt_tokens=prompt_tokens))
    if options is None:
        return []
    ctx.execution = StepExecution(options=options)
    outcome = await CompactStep().execute(state, ctx)
    assert outcome.next_step == "prepare"
    assert outcome.next_step_options == {"after_compact": True}
    return outcome.events


_BASE_DT = datetime(2026, 1, 1, tzinfo=timezone.utc)


from tests.unit.test_compact_step_inline import _FakeMemory as _OrganizeMemory
from ctx_weft.protocols import ProviderContext

class _FakeMemory(_OrganizeMemory):
    def __init__(self, task_count):
        super().__init__({frozenset([T.USER_PROMPT]): task_count})
        self.applied = []


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
        scope=MemoryAddress(session_id="s1", task_id="t1", agent_id="agt1"),
        transcript=[],
        sequence_counter=0,
        extra={"template": None, "bound_capabilities": []},
        # resolve_llm_identity 的真值来源（task-4 复审第二轮：summarize_for_compact
        # 不再读 agent.runtime.get("llm_model")，那个键正常任务执行永远不会被写入）。
        resolved_model=model_snapshot(model="mock", account="", context_limit=context_limit),
    )


def _ctx(memory):
    return SimpleNamespace(memory=memory, assembler=_FakeAssembler(), llm=_FakeLLM(),
                           provider_ctx=ProviderContext(session_id="s1", task_id="t1", agent_id="agt1"), task_manager=None)


async def test_disabled_when_ratio_zero():
    mem = _FakeMemory(task_count=50)  # plenty foldable, but feature off
    out = await _compact_before_dispatch(_state(ratio=0.0), _ctx(mem), prompt_tokens=999)
    assert not any(e.type == "MemoryCompacted" for e in out)
    assert mem.ingested == []
    assert mem.superseded == []


async def test_skips_when_below_threshold():
    mem = _FakeMemory(task_count=50)
    # 500 / 1000 = 0.5 < ratio 0.6
    out = await _compact_before_dispatch(_state(ratio=0.6), _ctx(mem), prompt_tokens=500)
    assert not any(e.type == "MemoryCompacted" for e in out)
    assert mem.ingested == []
    assert mem.superseded == []


async def test_skips_when_nothing_foldable():
    """escalating_compact 到达 L3（无 root residue 可折,L1/L2 均跳过）仍会试摘要 + 坍缩,
    但 task_count=2 <= keep_last=2 时 collapse_task_layer 内部判定无可折,不摸任何记录
    （STARTED 事件仍可能发出——预算门本身已过,只是最终无 MemoryCompacted，收尾仍发 Finished）。"""
    mem = _FakeMemory(task_count=2)  # <= keep_last=2 → collapse 内部 noop
    out = await _compact_before_dispatch(_state(ratio=0.6), _ctx(mem), prompt_tokens=800)
    assert [e.type for e in out] == ["MemoryCompactStarted", "MemoryCompactFinished"]  # 无 MemoryCompacted
    assert mem.ingested == []
    assert mem.superseded == []


async def test_compacts_task_layer_once_when_over_threshold_and_foldable():
    mem = _FakeMemory(task_count=10)  # > keep_last=2
    out = await _compact_before_dispatch(_state(ratio=0.6), _ctx(mem), prompt_tokens=800)
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
    out = await _compact_before_dispatch(
        _state(ratio=0.6, context_tokens=800), _ctx(mem), prompt_tokens=0)
    # fallback to context_tokens=800 triggers compact → collapsed USER_PROMPT ingested
    assert len(mem.ingested) == 1
    assert mem.ingested[0].metadata.get("collapsed") is True
    assert len(out) == 3  # started + compacted + finished（收尾聚合）


async def test_invalid_window_rejected_before_dispatch():
    with pytest.raises(ValueError, match="context_limit"):
        _state(ratio=0.6, context_limit=0)


async def test_predispatch_uses_escalating_compact(monkeypatch):
    """端到端接线断言：门控通过后 maybe_compact_before_dispatch 委派 escalating_compact，
    并原样透传 token_estimate + trigger="pre_dispatch"（Task 6 已接线，本用例防回归）。"""
    from tests import compaction_testkit as cm

    seen = {}

    async def _fake_esc(state, ctx, *, token_estimate, target_tokens, **kwargs):
        from ctx_weft.core.loop.steps.compact import _CompactResult as CompactionResult
        seen["est"] = token_estimate
        return CompactionResult(token_estimate, token_estimate, target_tokens)

    monkeypatch.setattr("ctx_weft.core.loop.steps.compact._run_compact", _fake_esc)
    mem = _FakeMemory(task_count=10)
    out = await _compact_before_dispatch(
        _state(ratio=0.5), _ctx(mem), prompt_tokens=800)
    assert out[-1].payload["trigger"] == "pre_dispatch"
    assert seen["est"] == 800
    assert not any(e.type == "MemoryCompacted" for e in out)
