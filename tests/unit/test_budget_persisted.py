"""budget persisted 接线（spec: execution-limits；wp8-1.2）。

跨 retry 累计（run 结束 snapshot 写回 → 重排后 restore 含前次）+ 重启恢复
（投影含 budget_consumed → 新 TM restore → 续用剩余）+ 旧投影无字段 → 空 dict 回落。
"""
from __future__ import annotations

import pytest

from ctx_weft.core.control.execution_budget import (
    ExecutionBudget,
    ExecutionLimits,
)
from ctx_weft.core.control.reducers import reduce_events
from ctx_weft.protocols.events import Event, EventType
from datetime import UTC, datetime

_T0 = datetime(2026, 9, 11, tzinfo=UTC)


def test_budget_snapshot_written_to_task_projection():
    """budget_consumed 随 TASK_STARTED 投影落库（reducers 透传）。"""
    ev = Event(
        id="evt_1", run_id="r", sequence=1, session_id="s1",
        type=EventType.TASK_CREATED, timestamp=_T0, task_id="t1",
        payload={"task": {"id": "t1", "status": "PENDING",
                          "budget_consumed": {"active_sec": 5.0, "turns": 2}}},
    )
    view = reduce_events([ev], "s1")
    tv = view.tasks["t1"]
    assert tv.budget_consumed == {"active_sec": 5.0, "turns": 2}


def test_old_projection_without_budget_falls_back():
    """旧投影无 budget_consumed → None → restore 走空 dict（行为同 wp7）。"""
    ev = Event(
        id="evt_1", run_id="r", sequence=1, session_id="s1",
        type=EventType.TASK_CREATED, timestamp=_T0, task_id="t1",
        payload={"task": {"id": "t1", "status": "PENDING"}},
    )
    view = reduce_events([ev], "s1")
    assert view.tasks["t1"].budget_consumed is None
    b = ExecutionBudget.restore(ExecutionLimits(), None)
    assert b.persisted_active_sec == 0.0 and b.persisted_turns == 0


def test_task_from_projection_carries_budget():
    """converters：投影 → Task dataclass 透传 budget_consumed。"""
    from ctx_weft.core.control.converters import task_from_projection
    ev = Event(
        id="evt_1", run_id="r", sequence=1, session_id="s1",
        type=EventType.TASK_CREATED, timestamp=_T0, task_id="t1",
        payload={"task": {"id": "t1", "status": "ACTIVE",
                          "budget_consumed": {"active_sec": 3.2, "turns": 1}}},
    )
    view = reduce_events([ev], "s1")
    task = task_from_projection(view.tasks["t1"])
    assert task.budget_consumed == {"active_sec": 3.2, "turns": 1}


def test_restore_continues_remaining_budget():
    """算术背书（wp7 已钉）：restore → persisted + 本 run 续算。"""
    lim = ExecutionLimits(task_active_timeout_sec=10.0)
    b = ExecutionBudget.restore(lim, {"active_sec": 8.0, "turns": 3})
    b.run_started()
    # 模拟 1s 活动
    import time
    b.monotonic = lambda: (b._run_started or 0) + 1.0  # type: ignore
    assert b.total_active_sec == pytest.approx(9.0)  # 8+1 续用剩余
    assert b.total_turns == 3
