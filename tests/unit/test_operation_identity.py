"""operation_id 身份三性质（spec: tool-operations；wp5-3.3）。

① 跨重启稳定：同一 (tenant, session, agent, record_id, ordinal) 派生同 id；
② 同参两次合法调用不去重（O-T09）：不同 record_id/ordinal → 不同 id；
③ call_1 复用不串扰：wire id 相同但逻辑身份独立。
"""

from __future__ import annotations

from ctx_weft.protocols.operations import (
    operation_id_for,
    operation_memory_result_id,
)


def test_same_logical_call_same_id_across_restart():
    a = operation_id_for("t", "s", "a", "rec_01", 0)
    b = operation_id_for("t", "s", "a", "rec_01", 0)   # 冷恢复重入：同五元组 → 同 id
    assert a == b
    assert operation_memory_result_id(a) == operation_memory_result_id(b)


def test_two_identical_legal_calls_dont_merge():
    """O-T09：两条 assistant 都用 call_1 且同参数——不同 record（两轮回合）→ 两个 id。"""
    first = operation_id_for("t", "s", "a", "rec_round1", 0)
    second = operation_id_for("t", "s", "a", "rec_round2", 0)
    assert first != second


def test_same_turn_different_ordinal_dont_merge():
    """同回合的两个工具调用（ordinal 0/1）→ 不同 id。"""
    assert operation_id_for("t", "s", "a", "rec", 0) != operation_id_for("t", "s", "a", "rec", 1)


def test_cross_session_isolation():
    """同 record/ordinal 在不同会话 → 不同 id（身份含 session 维度）。"""
    assert operation_id_for("t", "s1", "a", "rec", 0) != operation_id_for("t", "s2", "a", "rec", 0)
