"""跨层 fold L0→L1→L2（spec 2026-06-28 §4）。

通过 finalize_task_memory 造真实 L0 单元（task 层 body + agent 层 finish 对），
再调 fold_root_experience 验证三级降级：
- L1（超 keep_full）：删该单元 task 层 body（supersede、不再 recall），finish 对仍在。
- L2（超 keep_pair）：连 finish 对也 supersede，折成一条 AGENT_COMPACT_SUMMARY。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ctx_weft.core.loop.steps.compact import _count_root_residues, fold_root_experience
from ctx_weft.core.loop.steps.finalize import finalize_task_memory
from ctx_weft.core.state.models import NormalTaskSettings, Task
from ctx_weft.protocols import (
    MemoryEvent,
    MemoryEventType,
    MemoryScope,
    ProviderContext,
)
from ctx_weft.protocols.template import LoopConfig
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

pytestmark = pytest.mark.asyncio

T = MemoryEventType
_BASE = datetime(2026, 1, 1, tzinfo=UTC)
AGENT = "ag1"


def _pctx() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default")


def _task_scope(task_id: str) -> MemoryScope:
    return MemoryScope(session_id="s1", task_id=task_id, agent_id=AGENT)


def _agent_scope() -> MemoryScope:
    """fold 跑在 agent scope（task_id 为当前 task，但 agent 层召回只看 agent_id）。"""
    return MemoryScope(session_id="s1", task_id="cur", agent_id=AGENT)


class _FakeTM:
    def children_of(self, task_id: str) -> set[str]:
        return set()


def _ev(type_, scope, content, t, role=None, **meta) -> MemoryEvent:
    return MemoryEvent(type=type_, scope=scope, content=content,
                       timestamp=_BASE + timedelta(seconds=t), role=role, metadata=meta)


def _finalize_state(task: Task, scope: MemoryScope, cfg: LoopConfig):
    agent = SimpleNamespace(id=scope.agent_id, loop_config=cfg)
    session = SimpleNamespace(id="s1", tenant_id="default")
    return SimpleNamespace(run_id="run1", sequence_counter=0, session=session,
                           scope=scope, task=task, agent=agent)


def _finalize_ctx(mem):
    return SimpleNamespace(memory=mem, provider_ctx=_pctx(), task_manager=_FakeTM())


def _fold_state(cfg: LoopConfig):
    agent = SimpleNamespace(id=AGENT, loop_config=cfg)
    session = SimpleNamespace(id="s1", tenant_id="default")
    return SimpleNamespace(run_id="run1", sequence_counter=0, session=session,
                           scope=_agent_scope(), task=SimpleNamespace(id="cur"), agent=agent)


def _fold_ctx(mem):
    return SimpleNamespace(memory=mem, provider_ctx=_pctx())


def _root_task(task_id: str) -> Task:
    return Task(id=task_id, session_id="s1", status="FINISHED", tenant_id="default",
                assigned_agent_id=AGENT, creator_agent_id=AGENT, parent_task_id=None,
                title=f"Root {task_id}", user_prompt="hi", settings=NormalTaskSettings())


async def _make_l0_unit(mem, task_id: str, t0: int) -> None:
    """造一个真实 L0 顶层单元：task 层 raw body（短任务，留全 raw）+ agent 层 finish 对。"""
    scope = _task_scope(task_id)
    # short body：1 轮 assistant（< short_task_turn_cap=2, small → 留全 raw）
    await mem.ingest(_ev(T.USER_PROMPT, scope, f"prompt {task_id}", t0, role="user"), _pctx())
    await mem.ingest(_ev(T.LLM_RESPONSE, scope, f"reply {task_id}", t0 + 1, role="assistant"), _pctx())
    task = _root_task(task_id)
    state = _finalize_state(task, scope, LoopConfig())
    await finalize_task_memory(mem, state, task, f"out {task_id}", "success", _finalize_ctx(mem))


async def _alive_body(mem, task_id: str):
    """该 task 的 task 层 body（USER_PROMPT/LLM_RESPONSE）中未 superseded 的。"""
    return await mem.recall_recent(_task_scope(task_id),
                                   [T.USER_PROMPT, T.LLM_RESPONSE], 2000, _pctx())


async def _alive_finish_pair(mem, origin: str):
    """该 origin 在 agent scope 的 finish 对（AGENT_CONVERSATION_TURN）中未 superseded 的。"""
    recs = await mem.recall_recent(_agent_scope(), [T.AGENT_CONVERSATION_TURN], 2000, _pctx())
    return [r for r in recs if r.metadata.get("origin_task_id") == origin]


async def _alive_summaries(mem):
    return await mem.recall_recent(_agent_scope(), [T.AGENT_COMPACT_SUMMARY], 2000, _pctx())


# ──────────────────────────────────────────────────────────────────────────────


async def test_l0_units_have_body_and_finish_pair() -> None:
    """sanity：finalize 后每个 L0 单元都有 task 层 body + agent 层 finish 对。"""
    mem = InMemoryMemoryProvider()
    await _make_l0_unit(mem, "R0", 0)
    assert await _alive_body(mem, "R0"), "L0 unit must have task-layer body"
    assert await _alive_finish_pair(mem, "R0"), "L0 unit must have agent-layer finish pair"


async def test_l1_drops_body_keeps_finish_pair() -> None:
    """L0→L1：超 keep_full 的最老单元 task 层 body 被 supersede、finish 对仍在。"""
    mem = InMemoryMemoryProvider()
    # keep_full=2，开 4 个结束 root → 最老 2 个降 L1
    for i in range(4):
        await _make_l0_unit(mem, f"R{i}", i * 10)
    cfg = LoopConfig(compact_keep_last=2, compact_keep_pair=30)
    await fold_root_experience(_fold_state(cfg), _fold_ctx(mem),
                               cfg.compact_keep_last, "folded")

    # 最老 2 个（R0,R1）：body 删、finish 对留
    for old in ("R0", "R1"):
        assert await _alive_body(mem, old) == [], f"{old}: L1 task-layer body must be superseded"
        assert await _alive_finish_pair(mem, old), f"{old}: L1 finish pair must survive"
    # 最新 2 个（R2,R3）：body + finish 对都在
    for kept in ("R2", "R3"):
        assert await _alive_body(mem, kept), f"{kept}: kept (L0) body must survive"
        assert await _alive_finish_pair(mem, kept), f"{kept}: kept finish pair must survive"
    # keep_pair=30 → 无单元降 L2 → 无新摘要
    assert await _alive_summaries(mem) == [], "no L2 fold yet → no AGENT_COMPACT_SUMMARY"


async def test_l2_drops_finish_pair_writes_summary() -> None:
    """L1→L2：超 keep_pair 的最老单元连 finish 对也 supersede + 一条 AGENT_COMPACT_SUMMARY。"""
    mem = InMemoryMemoryProvider()
    # keep_full=1, keep_pair=2，开 4 个 → R0 降 L2，R1 降 L2... 实为 top[:-keep_pair] 降 L2
    for i in range(4):
        await _make_l0_unit(mem, f"R{i}", i * 10)
    cfg = LoopConfig(compact_keep_last=1, compact_keep_pair=2)
    await fold_root_experience(_fold_state(cfg), _fold_ctx(mem),
                               cfg.compact_keep_last, "folded")

    # 超 keep_pair=2 的最老（R0,R1）：finish 对也被 supersede
    for l2 in ("R0", "R1"):
        assert await _alive_body(mem, l2) == [], f"{l2}: L2 body superseded"
        assert await _alive_finish_pair(mem, l2) == [], f"{l2}: L2 finish pair must be superseded"
    # keep_pair 窗内但超 keep_full（R2）：body 删、finish 对留（L1）
    assert await _alive_body(mem, "R2") == [], "R2: beyond keep_full → body superseded"
    assert await _alive_finish_pair(mem, "R2"), "R2: within keep_pair → finish pair survives"
    # 最新（R3）：完整 L0
    assert await _alive_body(mem, "R3"), "R3: L0 body survives"
    assert await _alive_finish_pair(mem, "R3"), "R3: L0 finish pair survives"
    # 一条新 AGENT_COMPACT_SUMMARY
    summ = await _alive_summaries(mem)
    assert len(summ) == 1, "exactly one AGENT_COMPACT_SUMMARY for L2 fold"
    assert summ[0].role == "user"


async def test_count_root_residues_counts_l0_units() -> None:
    """_count_root_residues 数「有 body 的结束顶层单元」（L0 单元）。"""
    mem = InMemoryMemoryProvider()
    for i in range(4):
        await _make_l0_unit(mem, f"R{i}", i * 10)
    cfg = LoopConfig(compact_keep_last=2, compact_keep_pair=30)
    state, ctx = _fold_state(cfg), _fold_ctx(mem)

    assert await _count_root_residues(state, ctx) == 4, "4 L0 units before fold"
    await fold_root_experience(state, ctx, cfg.compact_keep_last, "folded")
    # L1 删了 R0/R1 的 body → 只剩 R2/R3 有 body
    assert await _count_root_residues(state, ctx) == 2, "after L1 fold, 2 units still have body"


async def test_l2_summary_anchored_before_kept() -> None:
    """L2 摘要锚到保留集最早 ts − 1µs（不变量 A）。"""
    mem = InMemoryMemoryProvider()
    for i in range(4):
        await _make_l0_unit(mem, f"R{i}", i * 100)
    cfg = LoopConfig(compact_keep_last=1, compact_keep_pair=2)
    await fold_root_experience(_fold_state(cfg), _fold_ctx(mem),
                               cfg.compact_keep_last, "folded")
    summ = await _alive_summaries(mem)
    assert len(summ) == 1
    # 保留集（R2 finish 对 + R3 全部）最早 ts；摘要须早于之
    kept_finish = await _alive_finish_pair(mem, "R2")
    kept_min = min(r.timestamp for r in kept_finish)
    assert summ[0].timestamp < kept_min, "L2 summary must anchor before kept set"
