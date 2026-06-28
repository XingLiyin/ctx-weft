"""fold_root_experience：新 Task-6 格式（AGENT_CONVERSATION_TURN 胶囊）的检测与折叠（§3.11）。

旧格式（TASK_DISPATCH_RESULT(parent=None) 作 root 标记）已在 spec §3.11 废弃；
新实现按 origin_task_id 分组 AGENT_CONVERSATION_TURN，parent_task_id 判据决定顶层单元。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ctx_weft.core.loop.steps.compact import _count_root_residues, fold_root_experience
from ctx_weft.protocols import MemoryEvent, MemoryEventType, MemoryScope, ProviderContext
from ctx_weft.protocols.template import LoopConfig
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

pytestmark = pytest.mark.asyncio
T = MemoryEventType
_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _pctx() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default")


def _sc(agent="ag1") -> MemoryScope:
    return MemoryScope(session_id="s1", task_id=None, agent_id=agent)


def _ev(type_, scope, content, t, role=None, **meta) -> MemoryEvent:
    return MemoryEvent(type=type_, scope=scope, content=content,
                       timestamp=_BASE + timedelta(seconds=t), role=role, metadata=meta)


def _state(scope, keep_pair: int = 0):
    """Task-4 跨层 fold：这些既有用例验证 L2 语义（finish 对 supersede + 摘要）。新模型下
    finish 对仅在「超 keep_pair」时降 L2，故令 keep_pair == 调用方传的 keep_last，使
    「超 keep_last → 直接 L2」（等价旧单层行为）。`_seed_root_capsule` 只在 agent 层造 finish
    对（无独立 task 层 body），故 L1 删 body 对它们是 no-op。"""
    agent = SimpleNamespace(id=scope.agent_id,
                            loop_config=LoopConfig(compact_keep_pair=keep_pair))
    session = SimpleNamespace(id="s1", tenant_id="default")
    return SimpleNamespace(run_id="run1", sequence_counter=0, session=session,
                           scope=scope, task=SimpleNamespace(id="cur"), agent=agent)


def _ctx(mem):
    return SimpleNamespace(memory=mem, provider_ctx=_pctx())


async def _seed_root_capsule(mem, scope, task_id: str, t0: int, parent_task_id=None):
    """Task-4 task-resident：一份结束单元 = task 层 body（task_id scope 的 USER_PROMPT）
    + agent 层 finish 对（4x AGENT_CONVERSATION_TURN，parent_task_id=None for root）。

    body → user prompt → assistant summary → assistant finish_task tool_call → tool result
    （L1 删 task 层 body；L2 连 finish 对一并删）。
    """
    tcid = f"tc_{task_id}"
    # task 层 body（按 origin task_id 的 task scope，同 agent_id 供 recall_recent_by_agent 召回）
    body_scope = MemoryScope(session_id="s1", task_id=task_id, agent_id=scope.agent_id)
    await mem.ingest(_ev(T.USER_PROMPT, body_scope, f"body prompt {task_id}", t0,
                         role="user"), _pctx())
    await mem.ingest(_ev(T.AGENT_CONVERSATION_TURN, scope, f"user prompt {task_id}", t0,
                         role="user", origin_task_id=task_id, parent_task_id=parent_task_id), _pctx())
    await mem.ingest(_ev(T.AGENT_CONVERSATION_TURN, scope, f"assistant summary {task_id}", t0 + 1,
                         role="assistant", origin_task_id=task_id, parent_task_id=parent_task_id), _pctx())
    await mem.ingest(_ev(T.AGENT_CONVERSATION_TURN, scope, "", t0 + 2,
                         role="assistant", origin_task_id=task_id, parent_task_id=parent_task_id,
                         tool_calls=[{"id": tcid, "name": "control:finish_task", "input": {}}]), _pctx())
    await mem.ingest(_ev(T.AGENT_CONVERSATION_TURN, scope, f"Process Report: done {task_id}", t0 + 3,
                         role="tool", origin_task_id=task_id, parent_task_id=parent_task_id,
                         tool_call_id=tcid), _pctx())


async def _seed_root_capsule_with_dispatch(mem, scope, task_id: str, t0: int, parent_task_id=None):
    """新格式胶囊 + cross-agent dispatch pair（parent_task_id 指向 delegating task）。"""
    await _seed_root_capsule(mem, scope, task_id, t0, parent_task_id=parent_task_id)


async def _seed_capsule_offset(mem, scope, task_id: str, turn_t: int, result_t: int):
    """多回合胶囊：前两轮在 turn_t（早），finish 对在 result_t（晚）。
    用于验证 anchor_ts 必须扫描全部元素（不能只看最新回合）。"""
    tcid = f"tc_{task_id}"
    await mem.ingest(_ev(T.AGENT_CONVERSATION_TURN, scope, f"user prompt {task_id}", turn_t,
                         role="user", origin_task_id=task_id, parent_task_id=None), _pctx())
    await mem.ingest(_ev(T.AGENT_CONVERSATION_TURN, scope, f"assistant summary {task_id}", turn_t + 1,
                         role="assistant", origin_task_id=task_id, parent_task_id=None), _pctx())
    await mem.ingest(_ev(T.AGENT_CONVERSATION_TURN, scope, "", result_t - 1,
                         role="assistant", origin_task_id=task_id, parent_task_id=None,
                         tool_calls=[{"id": tcid, "name": "control:finish_task", "input": {}}]), _pctx())
    await mem.ingest(_ev(T.AGENT_CONVERSATION_TURN, scope, f"Process Report: done {task_id}", result_t,
                         role="tool", origin_task_id=task_id, parent_task_id=None,
                         tool_call_id=tcid), _pctx())


async def _alive(mem, scope, type_, role=None):
    recs = await mem.recall_recent(scope, [type_], 2000, _pctx())
    return [r for r in recs if role is None or r.role == role]


# ──────────────────────────────────────────────────────────────────────────────
# 既有测试（已迁移到 Task-6 真实格式）
# ──────────────────────────────────────────────────────────────────────────────

async def test_folded_capsule_conversation_turns_superseded():
    """被折胶囊（最旧）的所有 AGENT_CONVERSATION_TURN 被 superseded，不落单。"""
    mem = InMemoryMemoryProvider()
    scope = _sc()
    # keep_last=1 → 3 个胶囊中折掉最旧 2 个
    for i, t in enumerate([0, 10, 20]):
        await _seed_root_capsule(mem, scope, f"task{i}", t * 10)
    n = await fold_root_experience(_state(scope, keep_pair=1), _ctx(mem), keep_last=1, summary_text="folded")
    assert n > 0
    turns = await _alive(mem, scope, T.AGENT_CONVERSATION_TURN)
    alive_tasks = {r.metadata.get("origin_task_id") for r in turns}
    assert alive_tasks == {"task2"}  # 只剩最新胶囊的 turns


async def test_kept_capsule_intact():
    """保留胶囊（最新 keep_last 个）的所有 AGENT_CONVERSATION_TURN 完整存活。"""
    mem = InMemoryMemoryProvider()
    scope = _sc()
    for i, t in enumerate([0, 10, 20]):
        await _seed_root_capsule(mem, scope, f"task{i}", t * 10)
    await fold_root_experience(_state(scope, keep_pair=2), _ctx(mem), keep_last=2, summary_text="folded")
    turns = await _alive(mem, scope, T.AGENT_CONVERSATION_TURN)
    alive_tasks = {r.metadata.get("origin_task_id") for r in turns}
    assert alive_tasks == {"task1", "task2"}


async def test_compact_summary_sorts_before_kept_capsule():
    """新 AGENT_COMPACT_SUMMARY 的 timestamp 早于最旧保留胶囊所有元素（anchor − 1µs）。"""
    mem = InMemoryMemoryProvider()
    scope = _sc()
    for i, t in enumerate([0, 10, 20]):
        await _seed_root_capsule(mem, scope, f"task{i}", t * 10)
    await fold_root_experience(_state(scope, keep_pair=1), _ctx(mem), keep_last=1, summary_text="folded")
    summ = await _alive(mem, scope, T.AGENT_COMPACT_SUMMARY)
    assert len(summ) == 1
    # 保留胶囊 task2 的最早回合在 t=200s
    kept_min_ts = _BASE + timedelta(seconds=200)
    assert summ[0].timestamp < kept_min_ts


async def test_nothing_to_fold_returns_zero():
    """root 残留 ≤ keep_last → 不折，conversation turns 全留。"""
    mem = InMemoryMemoryProvider()
    scope = _sc()
    await _seed_root_capsule(mem, scope, "task0", 0)
    n = await fold_root_experience(_state(scope), _ctx(mem), keep_last=6, summary_text="x")
    assert n == 0
    turns = await _alive(mem, scope, T.AGENT_CONVERSATION_TURN)
    assert {r.metadata.get("origin_task_id") for r in turns} == {"task0"}


async def test_anchor_ts_covers_all_kept_capsule_elements():
    """anchor_ts 必须扫描保留胶囊的全部回合（最早 user 回合），不能只看最晚回合。

    构造：2 个胶囊，keep_last=1（折掉第 1 个，保留第 2 个）。
    第 2 个（被保留）胶囊的首回合 user 在 turn_t=5，finish 回合在 result_t=20。
    正确 anchor < _BASE + 5s；错误 anchor = _BASE + 20s - 1µs。
    """
    mem = InMemoryMemoryProvider()
    scope = _sc()
    # 第 1 个胶囊（会被折掉）
    await _seed_capsule_offset(mem, scope, "task0", turn_t=0, result_t=3)
    # 第 2 个胶囊（会被保留）：首 user 回合在 t=5，finish 对在 t=20/21
    await _seed_capsule_offset(mem, scope, "task1", turn_t=5, result_t=20)
    await fold_root_experience(_state(scope, keep_pair=1), _ctx(mem), keep_last=1, summary_text="compacted")
    summ = await _alive(mem, scope, T.AGENT_COMPACT_SUMMARY)
    assert len(summ) == 1
    kept_turn_earliest = _BASE + timedelta(seconds=5)
    assert summ[0].timestamp < kept_turn_earliest, (
        f"AGENT_COMPACT_SUMMARY.timestamp={summ[0].timestamp} should be < {kept_turn_earliest} "
        f"(min of kept capsule's first AGENT_CONVERSATION_TURN)"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 新增测试（Task 12：§3.11 新判据）
# ──────────────────────────────────────────────────────────────────────────────

async def test_count_root_residues_detection():
    """_count_root_residues 数「仍有 task 层 body 的结束顶层单元」（L0 单元，Task-4 §4）。

    keep_last+2 个结束单元 → count == keep_last+2。fold（keep_full=keep_pair=keep_last=2）后，
    最旧 2 个降 L2（body + finish 对均 supersede），count == keep_last，AGENT_COMPACT_SUMMARY
    存在（role=user）。
    """
    keep_last = 2
    total = keep_last + 2
    mem = InMemoryMemoryProvider()
    scope = _sc()
    for i in range(total):
        await _seed_root_capsule(mem, scope, f"R{i}", i * 20)
    state = _state(scope, keep_pair=keep_last)
    ctx = _ctx(mem)

    count_before = await _count_root_residues(state, ctx)
    assert count_before == total, f"expected {total}, got {count_before}"

    await fold_root_experience(state, ctx, keep_last=keep_last, summary_text="compacted")

    count_after = await _count_root_residues(state, ctx)
    assert count_after == keep_last, f"after fold expected {keep_last}, got {count_after}"

    summ = await _alive(mem, scope, T.AGENT_COMPACT_SUMMARY)
    assert len(summ) == 1
    assert summ[0].role == "user"


async def test_same_agent_nested_child_folded_with_parent():
    """折顶层 R 时，同 agent 内嵌子胶囊 C1（origin=C1, parent=R）也被 supersede。"""
    mem = InMemoryMemoryProvider()
    scope = _sc()
    # R0：顶层 root，t=0
    await _seed_root_capsule(mem, scope, "R0", 0, parent_task_id=None)
    # C1：R0 的同 agent 子胶囊，t=10
    await _seed_root_capsule(mem, scope, "C1", 10, parent_task_id="R0")
    # R1：另一个顶层 root，t=40（保留）
    await _seed_root_capsule(mem, scope, "R1", 40, parent_task_id=None)

    # keep_last=1 → 折 R0（R0 是最旧顶层），R0 的后代 C1 也应被折
    n = await fold_root_experience(_state(scope, keep_pair=1), _ctx(mem), keep_last=1, summary_text="folded")
    assert n > 0

    turns = await _alive(mem, scope, T.AGENT_CONVERSATION_TURN)
    alive_origins = {r.metadata.get("origin_task_id") for r in turns}
    # R0 和 C1 均应被 supersede
    assert "R0" not in alive_origins, "R0 should be folded"
    assert "C1" not in alive_origins, "C1 (child of R0) should be folded with R0"
    assert "R1" in alive_origins, "R1 (kept) must survive"


async def test_cross_agent_dispatch_pair_folded_with_parent():
    """折顶层 R 时，cross-agent 派发对（TASK_DISPATCH + TASK_DISPATCH_RESULT，parent=R）也被 supersede。"""
    mem = InMemoryMemoryProvider()
    scope = _sc()
    # R0：顶层 root，t=0
    await _seed_root_capsule(mem, scope, "R0", 0, parent_task_id=None)
    # cross-agent 派发对：代表 R0 派发的子任务 C2
    tc = "tc_cross_C2"
    await mem.ingest(_ev(T.TASK_DISPATCH, scope, "", 10, role="assistant", tool_call_id=tc), _pctx())
    await mem.ingest(_ev(T.TASK_DISPATCH_RESULT, scope, "child result", 11, role="tool",
                         tool_call_id=tc, child_task_id="C2", parent_task_id="R0"), _pctx())
    # R1：另一个顶层 root，t=40（保留）
    await _seed_root_capsule(mem, scope, "R1", 40, parent_task_id=None)

    n = await fold_root_experience(_state(scope, keep_pair=1), _ctx(mem), keep_last=1, summary_text="folded")
    assert n > 0

    # TASK_DISPATCH_RESULT(C2) 应被 supersede
    results = await _alive(mem, scope, T.TASK_DISPATCH_RESULT)
    alive_children = {r.metadata.get("child_task_id") for r in results}
    assert "C2" not in alive_children, "cross-agent TASK_DISPATCH_RESULT for C2 should be superseded"

    # TASK_DISPATCH（配对）也应被 supersede
    dispatches = await _alive(mem, scope, T.TASK_DISPATCH)
    alive_tcids = {r.metadata.get("tool_call_id") for r in dispatches}
    assert tc not in alive_tcids, "cross-agent TASK_DISPATCH for C2 should be superseded"


async def test_cross_agent_child_counts_as_top_level_in_own_scope():
    """在 child agent 的 scope 内，胶囊 origin=C2, parent=R（R 不在该 scope origin 集）→ 算 1 顶层单元。

    OR 判据：parent_task_id ∉ scope origin 集 → 顶层单元。
    """
    mem = InMemoryMemoryProvider()
    # child agent 有自己的 scope（agent_id 不同或 task_id 不同均可）
    child_scope = MemoryScope(session_id="s1", task_id=None, agent_id="child_agent")

    # 仅植入 C2 的胶囊（parent=R，但 R 不在 child_scope 的任何胶囊 origin 中）
    await _seed_root_capsule(mem, child_scope, "C2", 0, parent_task_id="R")

    child_state = SimpleNamespace(
        run_id="run1", sequence_counter=0,
        session=SimpleNamespace(id="s1", tenant_id="default"),
        scope=child_scope,
        task=SimpleNamespace(id="cur"),
        agent=SimpleNamespace(id="child_agent", loop_config=LoopConfig()),
    )
    ctx = _ctx(mem)

    count = await _count_root_residues(child_state, ctx)
    assert count == 1, (
        f"C2 (parent=R, R not in scope) should count as 1 top-level unit, got {count}"
    )


async def test_anchor_ordering_before_all_kept_capsule_elements():
    """AGENT_COMPACT_SUMMARY.timestamp < min timestamp of ALL kept capsule elements（§3.11 anchor）。"""
    mem = InMemoryMemoryProvider()
    scope = _sc()
    # 3 capsules: fold R0, keep R1+R2
    await _seed_root_capsule(mem, scope, "R0", 0)
    await _seed_root_capsule(mem, scope, "R1", 100)  # earliest kept: t=100
    await _seed_root_capsule(mem, scope, "R2", 200)

    await fold_root_experience(_state(scope, keep_pair=2), _ctx(mem), keep_last=2, summary_text="anchor test")

    summ = await _alive(mem, scope, T.AGENT_COMPACT_SUMMARY)
    assert len(summ) == 1

    kept_turns = await _alive(mem, scope, T.AGENT_CONVERSATION_TURN)
    min_kept_ts = min(r.timestamp for r in kept_turns)
    assert summ[0].timestamp < min_kept_ts, (
        f"AGENT_COMPACT_SUMMARY.timestamp={summ[0].timestamp} must be < "
        f"min kept element ts={min_kept_ts}"
    )
