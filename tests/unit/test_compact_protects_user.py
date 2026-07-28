"""USER_PROMPT anchor protection: apply_compact must not fold USER_PROMPT events when
protect_types=(USER_PROMPT,) is passed; and the production callers (_fold_retry_segment,
escalating_compact) must pass that kwarg.

Two levels of tests:
1. Behavioral: InMemoryMemoryProvider.apply_compact directly — USER_PROMPT events survive,
   LLM/TOOL events fold, TASK_COMPACT_SUMMARY is inserted.
2. Caller wiring: _fold_retry_segment and escalating_compact pass protect_types=(USER_PROMPT,)
   to apply_compact (verified via a spy wrapper on the real provider).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ctx_weft.core.loop.steps.compact import COLLAPSE_DELIM, escalating_compact
from ctx_weft.core.loop.steps.observe import ObserveStep, Verdict
from ctx_weft.protocols import (
    CompactResult,
    MemoryEvent,
    MemoryEventType,
    MemoryLayer,
    MemoryScope,
    ProviderContext,
)
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider
from ctx_weft.providers.llm.tokenizer import HeuristicTokenizer


# ── fixtures ──────────────────────────────────────────────────────────────────

def _pctx() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default", task_id="t1", agent_id="a1")


def _scope() -> MemoryScope:
    return MemoryScope(session_id="s1", task_id="t1", agent_id="a1")


def _ts(offset_us: int) -> datetime:
    """Distinct timestamps with microsecond offsets."""
    base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    return base + timedelta(microseconds=offset_us)


async def _seed_events(mem: InMemoryMemoryProvider) -> None:
    """Ingest 5 task-layer events in chronological order with distinct timestamps."""
    pctx = _pctx()
    scope = _scope()
    events = [
        MemoryEvent(type=MemoryEventType.USER_PROMPT,  scope=scope, content="原始",  timestamp=_ts(1), role="user"),
        MemoryEvent(type=MemoryEventType.LLM_RESPONSE, scope=scope, content="想法1", timestamp=_ts(2), role="assistant"),
        MemoryEvent(type=MemoryEventType.TOOL_RESULT,  scope=scope, content="结果1", timestamp=_ts(3), role="tool"),
        MemoryEvent(type=MemoryEventType.USER_PROMPT,  scope=scope, content="HITL",  timestamp=_ts(4), role="user"),
        MemoryEvent(type=MemoryEventType.LLM_RESPONSE, scope=scope, content="想法2", timestamp=_ts(5), role="assistant"),
    ]
    for ev in events:
        await mem.ingest(ev, pctx)


async def _active_contents(
    mem: InMemoryMemoryProvider,
    types: list[MemoryEventType],
) -> list[str]:
    """Return content of active (un-superseded) events of the given types."""
    records = await mem.recall_recent(_scope(), types, limit=100, ctx=_pctx())
    return [r.content for r in records]


# ─────────────────────────────────────────────────────────────────────────────
# Level 1（v2 P4a 迁移注记）：provider apply_compact 的 keep_last=2 行为测试已删——
# 该形态无生产消费者；user 回合保护/段作用域/锚点不变量由 tests/unit/test_segment_fold.py 锁定。
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Level 2: Caller-wiring tests — production callers pass protect_types=(USER_PROMPT,)
# ─────────────────────────────────────────────────────────────────────────────

def _state(exit_reason: str = "max_turns") -> SimpleNamespace:
    agent = SimpleNamespace(
        id="a1",
        loop_config=SimpleNamespace(
            compact_keep_last=2, compact_token_ratio=0.1, compact_target_ratio=0.0),
        loop_guard=SimpleNamespace(context_limit=10000, context_tokens=10000),
        runtime={"llm_model": "mock"},
    )
    return SimpleNamespace(
        run_id="r1",
        sequence_counter=0,
        agent=agent,
        session=SimpleNamespace(id="s1", tenant_id="default"),
        task=SimpleNamespace(id="t1", process_report=""),
        scope=_scope(),
        transcript=[],
        extra={"template": None, "bound_capabilities": []},
        act_exit_reason=exit_reason,
        context_limit=10000,
        token_ratio=0.9,
    )


class _SpyProvider(InMemoryMemoryProvider):
    """Real InMemoryMemoryProvider that records all apply_compact calls."""

    def __init__(self) -> None:
        super().__init__()
        self.compact_calls: list[dict] = []

    # v2 P4a：apply_compact override 已删（生产路径不再调用；compact_calls 恒空，
    # escalating 测试的「task 层不走 apply_compact」断言退化为恒真但保留文档意义）。


def _ctx(mem: InMemoryMemoryProvider) -> SimpleNamespace:
    class _FakeAssembler:
        async def assemble(self, request):
            return SimpleNamespace(system="SYS", messages=[], tools=[])

    class _FakeLLM:
        tokenizer = HeuristicTokenizer()

        async def complete(self, request, stream=True):
            yield SimpleNamespace(kind="token", text="段摘要", usage=None, tool_call=None)

    return SimpleNamespace(
        memory=mem,
        assembler=_FakeAssembler(),
        llm=_FakeLLM(),
        provider_ctx=_pctx(),
        task_manager=None,
    )


async def test_fold_retry_segment_protects_user_prompts():
    """retry 段折经 segment_fold（v2 P3c 策展上移）：user 回合必须幸存、段摘要必须产出。

    旧断言检查 apply_compact 的 protect_types 传参（实现耦合）；protect 政策现内置于
    框架侧 segment_fold，改为纯行为断言。
    """
    mem = _SpyProvider()
    await _seed_events(mem)

    events: list = []
    verdict = Verdict(task_outcome="retry", act_recap="段摘要", reported=True)

    await ObserveStep()._fold_retry_segment(_state("max_turns"), _ctx(mem), verdict, events)

    # Verify behavioral outcome: both USER_PROMPs survive
    up_contents = await _active_contents(mem, [MemoryEventType.USER_PROMPT])
    assert "原始" in up_contents, f"'原始' USER_PROMPT was folded: {up_contents}"
    assert "HITL" in up_contents, f"'HITL' USER_PROMPT was folded: {up_contents}"

    # Verify TASK_COMPACT_SUMMARY exists
    summary_contents = await _active_contents(mem, [MemoryEventType.TASK_COMPACT_SUMMARY])
    assert summary_contents, "No TASK_COMPACT_SUMMARY was created"


async def test_escalating_compact_passes_protect_types_and_user_prompts_survive():
    """escalating_compact's L3 uses collapse_task_layer (not apply_compact) for the task layer.

    The 'original' USER_PROMPT content ("原始") is preserved in the collapsed UP's first
    section (before COLLAPSE_DELIM), and "HITL" UP (within the keep window) survives as
    a separate record — so no UP content is lost, just represented differently.
    """
    mem = _SpyProvider()
    await _seed_events(mem)

    with patch(
        "ctx_weft.core.loop.steps.compact.summarize_for_compact",
        new_callable=AsyncMock,
        return_value="段摘要",
    ):
        # 无 agent 层派发对 → L1/L2 天然跳过，直落 L3 坍缩当前 task。
        await escalating_compact(_state("normal"), _ctx(mem), token_estimate=10000, trigger="test")

    # escalating_compact 的 L3 用 collapse_task_layer，不用 apply_compact，折 task 层
    task_calls = [c for c in mem.compact_calls if c["layer"] is MemoryLayer.TASK]
    assert not task_calls, (
        "apply_compact should NOT be called for task layer (collapse_task_layer is used instead)"
    )

    # Behavioral outcome: task layer collapsed into a USER_PROMPT with collapsed=True
    up_contents = await _active_contents(mem, [MemoryEventType.USER_PROMPT])
    # "HITL" was in the keep window (last 2 records) → survives as a separate USER_PROMPT
    assert "HITL" in up_contents, f"'HITL' USER_PROMPT was unexpectedly folded: {up_contents}"
    # "原始" is embedded in the collapsed UP content (original section before COLLAPSE_DELIM)
    all_up_text = " ".join(up_contents)
    assert "原始" in all_up_text, (
        f"'原始' content is lost — should be embedded in collapsed UP: {up_contents}"
    )
    # A collapsed USER_PROMPT (with COLLAPSE_DELIM) was ingested
    assert any(COLLAPSE_DELIM in c for c in up_contents), (
        f"No collapsed USER_PROMPT found in active UP records: {up_contents}"
    )

    # No TASK_COMPACT_SUMMARY — collapse_task_layer writes collapsed USER_PROMPT instead
    summary_contents = await _active_contents(mem, [MemoryEventType.TASK_COMPACT_SUMMARY])
    assert not summary_contents, f"Unexpected TASK_COMPACT_SUMMARY written: {summary_contents}"
