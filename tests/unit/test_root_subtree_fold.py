"""fold_root_experience：连带折叠被折胶囊的 AGENT_CONVERSATION_TURN（§2.2）。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ctx_weft.core.loop.steps.compact import fold_root_experience
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


def _state(scope):
    agent = SimpleNamespace(id=scope.agent_id, loop_config=LoopConfig())
    session = SimpleNamespace(id="s1", tenant_id="default")
    return SimpleNamespace(run_id="run1", sequence_counter=0, session=session,
                           scope=scope, task=SimpleNamespace(id="cur"), agent=agent)


def _ctx(mem):
    return SimpleNamespace(memory=mem, provider_ctx=_pctx())


async def _seed_capsule(mem, scope, task_id: str, t0: int):
    """一份 root 胶囊：user + assistant(summary) + dispatch + result，同 task_id。"""
    tcid = f"tc_{task_id}"
    await mem.ingest(_ev(T.AGENT_CONVERSATION_TURN, scope, f"prompt {task_id}", t0, role="user", origin_task_id=task_id), _pctx())
    await mem.ingest(_ev(T.AGENT_CONVERSATION_TURN, scope, f"summary {task_id}", t0, role="assistant", origin_task_id=task_id), _pctx())
    await mem.ingest(_ev(T.TASK_DISPATCH, scope, "", t0, role="assistant", tool_call_id=tcid), _pctx())
    await mem.ingest(_ev(T.TASK_DISPATCH_RESULT, scope, f"result {task_id}", t0, role="tool",
                         tool_call_id=tcid, child_task_id=task_id, parent_task_id=None), _pctx())


async def _seed_capsule_offset(mem, scope, task_id: str, turn_t: int, result_t: int):
    """多回合胶囊：AGENT_CONVERSATION_TURN 在 turn_t（早），TASK_DISPATCH_RESULT 在 result_t（晚）。
    用于验证 anchor_ts 必须扫描全部元素（不能只看 result）。"""
    tcid = f"tc_{task_id}"
    await mem.ingest(_ev(T.AGENT_CONVERSATION_TURN, scope, f"prompt {task_id}", turn_t, role="user",
                         origin_task_id=task_id), _pctx())
    await mem.ingest(_ev(T.AGENT_CONVERSATION_TURN, scope, f"summary {task_id}", turn_t + 1, role="assistant",
                         origin_task_id=task_id), _pctx())
    await mem.ingest(_ev(T.TASK_DISPATCH, scope, "", result_t - 1, role="assistant", tool_call_id=tcid), _pctx())
    await mem.ingest(_ev(T.TASK_DISPATCH_RESULT, scope, f"result {task_id}", result_t, role="tool",
                         tool_call_id=tcid, child_task_id=task_id, parent_task_id=None), _pctx())


async def _alive(mem, scope, type_, role=None):
    recs = await mem.recall_recent(scope, [type_], 2000, _pctx())
    return [r for r in recs if role is None or r.role == role]


async def test_folded_capsule_conversation_turns_superseded():
    """被折胶囊（最旧）的 user+assistant conversation turn 不落单。"""
    mem = InMemoryMemoryProvider()
    scope = _sc()
    # keep_last=1 → 3 个胶囊中折掉最旧 2 个
    for i, t in enumerate([0, 10, 20]):
        await _seed_capsule(mem, scope, f"task{i}", t)
    n = await fold_root_experience(_state(scope), _ctx(mem), keep_last=1, summary_text="folded")
    assert n > 0
    turns = await _alive(mem, scope, T.AGENT_CONVERSATION_TURN)
    alive_tasks = {r.metadata.get("origin_task_id") for r in turns}
    assert alive_tasks == {"task2"}            # 只剩最新胶囊的 turns，旧的不落单


async def test_kept_capsule_intact():
    """保留胶囊（最新 keep_last 个）的四件套完整存活。"""
    mem = InMemoryMemoryProvider()
    scope = _sc()
    for i, t in enumerate([0, 10, 20]):
        await _seed_capsule(mem, scope, f"task{i}", t)
    await fold_root_experience(_state(scope), _ctx(mem), keep_last=2, summary_text="folded")
    turns = await _alive(mem, scope, T.AGENT_CONVERSATION_TURN)
    alive_tasks = {r.metadata.get("origin_task_id") for r in turns}
    assert alive_tasks == {"task1", "task2"}
    results = await _alive(mem, scope, T.TASK_DISPATCH_RESULT)
    assert {r.metadata.get("child_task_id") for r in results} == {"task1", "task2"}


async def test_compact_summary_sorts_before_kept_capsule():
    """新 AGENT_COMPACT_SUMMARY 的 timestamp 早于最旧保留胶囊（anchor − 1µs）。"""
    mem = InMemoryMemoryProvider()
    scope = _sc()
    for i, t in enumerate([0, 10, 20]):
        await _seed_capsule(mem, scope, f"task{i}", t)
    await fold_root_experience(_state(scope), _ctx(mem), keep_last=1, summary_text="folded")
    summ = await _alive(mem, scope, T.AGENT_COMPACT_SUMMARY)
    assert len(summ) == 1
    kept_results = await _alive(mem, scope, T.TASK_DISPATCH_RESULT)
    assert summ[0].timestamp < min(r.timestamp for r in kept_results)


async def test_nothing_to_fold_returns_zero():
    """root 残留 ≤ keep_last → 不折，conversation turns 全留。"""
    mem = InMemoryMemoryProvider()
    scope = _sc()
    await _seed_capsule(mem, scope, "task0", 0)
    n = await fold_root_experience(_state(scope), _ctx(mem), keep_last=6, summary_text="x")
    assert n == 0
    turns = await _alive(mem, scope, T.AGENT_CONVERSATION_TURN)
    assert {r.metadata.get("origin_task_id") for r in turns} == {"task0"}


async def test_anchor_ts_covers_all_kept_capsule_elements():
    """anchor_ts 必须扫描保留胶囊的全部元素（含 AGENT_CONVERSATION_TURN），
    不能只看 TASK_DISPATCH_RESULT。

    构造：2 个胶囊，keep_last=1（折掉第 1 个，保留第 2 个）。
    第 2 个（被保留）胶囊的 AGENT_CONVERSATION_TURN 时间戳（turn_t=5）
    早于其 TASK_DISPATCH_RESULT（result_t=20）。
    正确 anchor = _BASE + 5s - 1µs；旧实现 anchor = _BASE + 20s - 1µs（错误）。
    断言：AGENT_COMPACT_SUMMARY.timestamp < _BASE + timedelta(seconds=5)。
    """
    mem = InMemoryMemoryProvider()
    scope = _sc()
    # 第 1 个胶囊（会被折掉）
    await _seed_capsule_offset(mem, scope, "task0", turn_t=0, result_t=3)
    # 第 2 个胶囊（会被保留）：turn 在 t=5，result 在 t=20
    await _seed_capsule_offset(mem, scope, "task1", turn_t=5, result_t=20)
    await fold_root_experience(_state(scope), _ctx(mem), keep_last=1, summary_text="compacted")
    summ = await _alive(mem, scope, T.AGENT_COMPACT_SUMMARY)
    assert len(summ) == 1
    # anchor_ts must be based on min of ALL kept elements (turn at t=5), not just result (t=20)
    kept_turn_earliest = _BASE + timedelta(seconds=5)
    assert summ[0].timestamp < kept_turn_earliest, (
        f"AGENT_COMPACT_SUMMARY.timestamp={summ[0].timestamp} should be < {kept_turn_earliest} "
        f"(min of kept capsule's AGENT_CONVERSATION_TURN), but anchor was computed only from "
        f"TASK_DISPATCH_RESULT (t=20)"
    )
