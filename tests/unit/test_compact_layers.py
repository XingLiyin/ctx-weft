"""两类 compact 走同一 CompactStep，按层折叠（spec/06 §7）。

折叠 task 层写 TASK_COMPACT_SUMMARY、折叠 agent 层写 AGENT_COMPACT_SUMMARY。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ctx_weft.core.events.bus import InProcessEventBus
from ctx_weft.core.loop.driver import LoopContext, LoopState
from ctx_weft.core.loop.steps.compact import CompactStep
from ctx_weft.core.loop.steps.finalize import FinalizeStep
from ctx_weft.core.state.models import Task
from ctx_weft.protocols import MemoryEvent, MemoryEventType, MemoryScope, ProviderContext
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

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
    """新格式：用 AGENT_CONVERSATION_TURN（parent=None）作 root 胶囊触发 agent 层压缩。"""
    mem = InMemoryMemoryProvider()
    base = datetime(2026, 1, 1, tzinfo=UTC)
    # 植入 2 组 AGENT_CONVERSATION_TURN（keep_last=1 → 折最旧 1 组）
    for grp, t0 in enumerate([0, 10]):
        oid = f"root{grp}"
        for role, dt in [("user", 0), ("assistant", 1)]:
            await mem.ingest(MemoryEvent(
                type=T.AGENT_CONVERSATION_TURN, scope=_scope(),
                content=f"turn {oid} {role}",
                timestamp=base + timedelta(seconds=t0 + dt), role=role,
                metadata={"origin_task_id": oid, "parent_task_id": None},
            ), _pctx())

    await _run_compact(mem)

    recs = await mem.recall_recent(
        _scope(), [T.AGENT_CONVERSATION_TURN, T.AGENT_COMPACT_SUMMARY], 10, _pctx()
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


