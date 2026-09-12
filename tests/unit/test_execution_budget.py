"""ExecutionBudget 计量算术（spec: execution-limits；wp7-1.2，design D5）。

fake monotonic（列表弹出式）驱动纯算术：park 不计 / 跨 retry 累计 / 重启恢复 /
漏记 ≤ 周期 / check 首超限项 / turns 预占与逻辑计次。
"""
from __future__ import annotations

import pytest

from ctx_weft.core.control.execution_budget import (
    ExecutionBudget,
    ExecutionLimitExceeded,
    ExecutionLimits,
)
from ctx_weft.core.models.discriminators import TaskErrorCode


class _Clock:
    """列表弹出式假 monotonic：每次调用取下一个时刻。"""

    def __init__(self, *ticks: float) -> None:
        self._ticks = list(ticks)
        self._last = 0.0

    def __call__(self) -> float:
        self._last = self._ticks.pop(0) if self._ticks else self._last
        return self._last


def test_parked_correct_arithmetic():
    """挂点正确的等待场景：0→1 活动，1→50 等待（park），50→53 活动。总活动 = 1+3 = 4。"""
    lim = ExecutionLimits(task_active_timeout_sec=10.0)
    clk = _Clock(0.0, 1.0, 50.0, 51.0, 53.0)
    b = ExecutionBudget(lim, monotonic=clk)
    b.run_started()                    # 弹 0
    b.park()                           # 弹 1（HITL 开始）
    b.resume()                         # 弹 50（parked_accum = 49）
    assert b.current_run_active_sec == pytest.approx(2.0)  # 弹 51：51-0-49=2（0→1 + 50→51）
    assert b.total_active_sec == pytest.approx(4.0)         # 弹 53：53-0-49=4（再 +2）


def test_retry_accumulates_across_runs():
    """跨 retry 累计：snapshot → 新 budget restore → 总量含前次。"""
    lim = ExecutionLimits(task_active_timeout_sec=10.0)
    b = ExecutionBudget(lim, monotonic=_Clock(0.0, 6.0))
    b.run_started()
    snap = b.snapshot()                       # 6s 已消费
    assert snap["active_sec"] == pytest.approx(6.0)
    b2 = ExecutionBudget.restore(lim, snap, monotonic=_Clock(100.0, 103.0))
    b2.run_started()                          # 新 run：3s 活动
    assert b2.total_active_sec == pytest.approx(9.0)  # 6+3 跨 retry 累计


def test_restart_restores_consumed():
    """重启恢复：persisted consumed 续算剩余预算（不从零重计）。"""
    lim = ExecutionLimits(task_active_timeout_sec=10.0, max_actor_turns_per_task=5)
    b = ExecutionBudget.restore(
        lim, {"active_sec": 8.0, "turns": 3}, monotonic=_Clock(0.0, 3.0))
    b.run_started()
    assert b.total_turns == 3
    assert b.monotonic() == 3.0         # t=3：total = 8+3 = 11 > 10 → 命中
    with pytest.raises(ExecutionLimitExceeded) as ei:
        b.check()
    assert ei.value.code == TaskErrorCode.TASK_DEADLINE_EXCEEDED


def test_check_returns_first_exceeded():
    lim = ExecutionLimits(max_actor_turns_per_task=2)
    b = ExecutionBudget(lim, persisted_turns=3)   # 3 > 2（预占后超限即拦）
    with pytest.raises(ExecutionLimitExceeded) as ei:
        b.check()
    assert ei.value.code == TaskErrorCode.ACTOR_TURN_LIMIT
    assert ei.value.retriable is False


def test_unlimited_never_raises():
    b = ExecutionBudget(ExecutionLimits.none(), persisted_active_sec=1e9, persisted_turns=10**9)
    b.check()
    b.check_step(step_active_sec=1e9)


def test_turn_reservation_and_logical_counting():
    """派发前预占一轮；consume 兑现（不双计）；自愈不经过 consume（逻辑计次）。"""
    lim = ExecutionLimits(max_actor_turns_per_task=2)
    b = ExecutionBudget(lim)
    b.reserve_turn()                    # 预占（崩溃恢复不超支）
    assert b.total_turns == 1
    b.consume_turn()                    # 兑现预占——不再 +1
    assert b.total_turns == 1
    b.consume_turn()                    # 第二次逻辑请求
    assert b.total_turns == 2
    b.reserve_turn()                    # 第三次派发预占 → 3 > 2 → 拦截
    with pytest.raises(ExecutionLimitExceeded):
        b.check()


def test_step_deadline():
    lim = ExecutionLimits(step_active_timeout_sec=5.0)
    b = ExecutionBudget(lim)
    with pytest.raises(ExecutionLimitExceeded) as ei:
        b.check_step(step_active_sec=5.1)
    assert ei.value.code == TaskErrorCode.STEP_DEADLINE_EXCEEDED


def test_snapshot_roundtrip():
    lim = ExecutionLimits()
    b = ExecutionBudget(lim, monotonic=_Clock(0.0, 2.5))
    b.run_started()
    b.consume_turn()
    snap = b.snapshot()
    assert snap == {"active_sec": 2.5, "turns": 1}
    b2 = ExecutionBudget.restore(lim, snap)
    assert b2.total_turns == 1
