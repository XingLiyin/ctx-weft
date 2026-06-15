"""两类 compact 走同一 CompactStep，由数据驱动的 _foldable_layers 决定折叠哪些层（spec/06 §7）。

无 layer setting：CompactStep._foldable_layers 列出可折叠层；折叠 task 层写
TASK_COMPACT_SUMMARY、折叠 agent 层写 AGENT_COMPACT_SUMMARY。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from loomex_core.core.events.bus import InProcessEventBus
from loomex_core.core.loop.driver import LoopContext, LoopState
from loomex_core.core.loop.steps.compact import CompactStep
from loomex_core.core.loop.steps.finalize import FinalizeStep
from loomex_core.core.state.models import Task
from loomex_core.protocols import MemoryEvent, MemoryEventType, MemoryScope, ProviderContext
from loomex_core.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

pytestmark = pytest.mark.asyncio
T = MemoryEventType
_BASE = datetime(2026, 1, 1, tzinfo=UTC)


class _FakeAssembler:
    async def assemble(self, request):
        return SimpleNamespace(messages=[], system="", tools=[], token_count=0)


class _FakeLLM:
    async def complete(self, req, stream=True):
        yield SimpleNamespace(kind="token", text="SUMMARY")


def _scope() -> MemoryScope:
    return MemoryScope(session_id="s1", task_id="T_target", agent_id="ag1")


def _pctx() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default")


def _ev(type_, content, t, role=None) -> MemoryEvent:
    return MemoryEvent(type=type_, scope=_scope(), content=content,
                       timestamp=_BASE + timedelta(seconds=t), role=role)


async def _run_compact(mem) -> None:
    task = SimpleNamespace(id="C1", status="ACTIVE")
    agent = SimpleNamespace(
        id="ag1", runtime={"llm_model": "mock"},
        loop_config=SimpleNamespace(compact_keep_last=1),
    )
    session = SimpleNamespace(id="s1", tenant_id="default", goal="")
    state = LoopState(run_id="r1", session=session, task=task, agent=agent,
                      scope=_scope(), extra={"template": None, "bound_capabilities": []},
                      transcript=[])
    ctx = LoopContext(assembler=_FakeAssembler(), llm=_FakeLLM(), memory=mem,
                      event_bus=InProcessEventBus(), provider_ctx=_pctx())
    await CompactStep().execute(state, ctx)


async def test_task_compact_writes_task_summary() -> None:
    mem = InMemoryMemoryProvider()
    await mem.ingest(_ev(T.USER_PROMPT, "u", 0, "user"), _pctx())
    await mem.ingest(_ev(T.LLM_RESPONSE, "a1", 1, "assistant"), _pctx())
    await mem.ingest(_ev(T.LLM_RESPONSE, "a2", 2, "assistant"), _pctx())

    await _run_compact(mem)

    recs = await mem.recall_recent(
        _scope(), [T.USER_PROMPT, T.LLM_RESPONSE, T.TASK_COMPACT_SUMMARY], 10, _pctx()
    )
    assert any(r.type == T.TASK_COMPACT_SUMMARY and r.content == "SUMMARY" for r in recs)
    assert "a2" in [r.content for r in recs]  # keep_last=1


async def test_agent_compact_writes_agent_summary() -> None:
    mem = InMemoryMemoryProvider()
    await mem.ingest(_ev(T.TASK_DISPATCH_RESULT, "r1", 0, "tool"), _pctx())
    await mem.ingest(_ev(T.TASK_DISPATCH_RESULT, "r2", 1, "tool"), _pctx())

    await _run_compact(mem)

    recs = await mem.recall_recent(
        _scope(), [T.TASK_DISPATCH_RESULT, T.AGENT_COMPACT_SUMMARY], 10, _pctx()
    )
    assert any(r.type == T.AGENT_COMPACT_SUMMARY and r.content == "SUMMARY" for r in recs)


async def test_finalize_retry_carries_progress_no_user_message() -> None:
    """retry：observe 分析 → process_report(Current Progress)；不注入 user message。"""
    task = Task(id="T1", session_id="s1", status="PENDING", title="X")
    task.observer_outcome = "retry"
    mem = InMemoryMemoryProvider()
    state = LoopState(
        run_id="r1",
        session=SimpleNamespace(id="s1", tenant_id="default"),
        task=task,
        agent=SimpleNamespace(id="ag1", loop_config=SimpleNamespace(compact_keep_last=6)),
        scope=MemoryScope(session_id="s1", task_id="T1", agent_id="ag1"),
        verdict=SimpleNamespace(task_outcome="retry", summary="missing X; do Y next"),
    )
    ctx = LoopContext(
        assembler=None, llm=None, memory=mem,
        event_bus=InProcessEventBus(), provider_ctx=_pctx(), task_manager=None,
    )
    await FinalizeStep().execute(state, ctx)

    assert task.process_report == "missing X; do Y next"  # → Current Progress
    assert task.retry_count == 1
    recs = await mem.recall_recent(
        MemoryScope(session_id="s1", task_id="T1", agent_id="ag1"), [T.USER_PROMPT], 10, _pctx()
    )
    assert recs == []  # 不注入 user message




# ── CompactStep._foldable_layers：到达限制时同时压两层 + 空层守卫 ──────────────────────────────

def _reason_scope() -> MemoryScope:
    return MemoryScope(session_id="s1", task_id="RT", agent_id="rag")


def _reason_state() -> tuple[LoopState, Task]:
    task = Task(id="RT", session_id="s1", status="ACTIVE", title="X")
    state = LoopState(
        run_id="r1",
        session=SimpleNamespace(id="s1", tenant_id="default"),
        task=task,
        agent=SimpleNamespace(id="rag", loop_config=SimpleNamespace(compact_keep_last=1)),
        scope=_reason_scope(),
        extra={},
    )
    return state, task


async def _ingest_n(mem, type_, n: int, role: str) -> None:
    for i in range(n):
        await mem.ingest(
            MemoryEvent(type=type_, scope=_reason_scope(), content=f"c{i}",
                        timestamp=_BASE + timedelta(seconds=i), role=role),
            ProviderContext(session_id="s1", tenant_id="default"),
        )


async def test_compactable_layers_skips_empty_layer() -> None:
    """task 层有 3 条(>keep_last=1)→入选；agent 层空→被守卫跳过。"""
    mem = InMemoryMemoryProvider()
    state, _ = _reason_state()
    ctx = LoopContext(assembler=None, llm=None, memory=mem,
                      event_bus=InProcessEventBus(), provider_ctx=_pctx())
    await _ingest_n(mem, T.LLM_RESPONSE, 3, role="assistant")
    layers = await CompactStep()._foldable_layers(state, ctx, keep_last=1)
    assert layers == ["task"]


async def test_compactable_layers_both_when_both_have_content() -> None:
    mem = InMemoryMemoryProvider()
    state, _ = _reason_state()
    ctx = LoopContext(assembler=None, llm=None, memory=mem,
                      event_bus=InProcessEventBus(), provider_ctx=_pctx())
    await _ingest_n(mem, T.LLM_RESPONSE, 3, role="assistant")           # task 层
    await _ingest_n(mem, T.TASK_DISPATCH_RESULT, 3, role="tool")        # agent 层
    layers = await CompactStep()._foldable_layers(state, ctx, keep_last=1)
    assert layers == ["agent", "task"]


async def test_compactable_layers_none_below_threshold() -> None:
    mem = InMemoryMemoryProvider()
    state, _ = _reason_state()
    ctx = LoopContext(assembler=None, llm=None, memory=mem,
                      event_bus=InProcessEventBus(), provider_ctx=_pctx())
    await _ingest_n(mem, T.LLM_RESPONSE, 1, role="assistant")           # == keep_last，不够折
    layers = await CompactStep()._foldable_layers(state, ctx, keep_last=1)
    assert layers == []
