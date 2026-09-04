"""plan_threshold_trip：熔断清场的分类判据（纯函数，不碰总线/队列）。

顺序不在这里测——那是 test_failure_threshold_trip.py 的事（trip 序列 1-8）。
本文件只钉「谁进哪一桶」。
"""

from __future__ import annotations

import dataclasses

import pytest

from ctx_weft.core.domain.models import NormalTaskSettings, Task
from ctx_weft.core.orchestrator.failure_threshold import TripPlan, plan_threshold_trip
from ctx_weft.core.utils import now_utc


def _t(tid: str, *, parent: str | None = "root", status: str = "ACTIVE",
       started: bool = True) -> Task:
    return Task(
        id=tid, session_id="ses_1", status=status, parent_task_id=parent,
        settings=NormalTaskSettings(),
        started_at=now_utc() if started else None,
    )


def _tasks(*ts: Task) -> dict[str, Task]:
    return {t.id: t for t in ts}


def test_root_non_terminal_is_failed():
    plan = plan_threshold_trip(
        _tasks(_t("root", parent=None, status="SUSPENDED")),
        pending_ids=[], running_ids=set())
    assert plan.fail_roots == ("root",)


def test_root_already_terminal_is_skipped():
    """已终态的 root（自己就是第 N 败，FinalizeStep 已闭合）不改状态、不发事件。"""
    for st in ("FINISHED", "FAILED", "CANCELED"):
        plan = plan_threshold_trip(
            _tasks(_t("root", parent=None, status=st)),
            pending_ids=[], running_ids=set())
        assert plan.fail_roots == (), st


def test_queued_non_root_canceled_root_entry_dropped():
    """清队：非 root 条目取消；root 条目直接丢弃（它的去向是 root 判死）。"""
    plan = plan_threshold_trip(
        _tasks(_t("root", parent=None), _t("c1", status="PENDING")),
        pending_ids=["root", "c1"], running_ids=set())
    assert plan.cancel_queued == ("c1",)


def test_started_queued_task_goes_to_cancel_now():
    plan = plan_threshold_trip(
        _tasks(_t("c1", status="PENDING", started=True)),
        pending_ids=["c1"], running_ids=set())
    assert "c1" in plan.cancel_now_ids


def test_unstarted_task_never_needs_closure():
    """未启动过的任务从未铸框 / 写过 memory，跳过闭合——零 memory 写。"""
    plan = plan_threshold_trip(
        _tasks(_t("c1", status="PENDING", started=False)),
        pending_ids=["c1"], running_ids=set())
    assert plan.cancel_queued == ("c1",) and plan.cancel_now_ids == ()


def test_suspended_non_root_canceled_and_closed():
    plan = plan_threshold_trip(
        _tasks(_t("c1", status="SUSPENDED", started=True)),
        pending_ids=[], running_ids=set())
    assert plan.cancel_suspended == ("c1",)
    assert "c1" in plan.cancel_now_ids


def test_inflight_non_root_gets_signal_not_event():
    """在途任务只发协作取消信号；TASK_CANCELED 由 run 结束后的 apply_run_outcome 发。"""
    plan = plan_threshold_trip(
        _tasks(_t("c1", status="ACTIVE", started=True)),
        pending_ids=[], running_ids={"c1"})
    assert plan.signal_inflight == ("c1",)
    assert "c1" not in plan.cancel_suspended and "c1" not in plan.cancel_now_ids
    assert plan.ack_task_ids == ("c1",)          # 已启动带框 → eager ack


def test_inflight_without_dispatch_frame_not_acked():
    """判据是 started_at + parent_task_id（框由 ensure_dispatch_frame_at_start 铸），
    不看瞬态的 origin_tool_call_id。"""
    plan = plan_threshold_trip(
        _tasks(_t("c1", status="ACTIVE", started=False)),
        pending_ids=[], running_ids={"c1"})
    assert plan.signal_inflight == ("c1",) and plan.ack_task_ids == ()


def test_inflight_root_gets_signal_after_being_failed():
    plan = plan_threshold_trip(
        _tasks(_t("root", parent=None, status="ACTIVE")),
        pending_ids=[], running_ids={"root"})
    assert plan.fail_roots == ("root",) and plan.signal_roots == ("root",)


def test_cancel_now_order_is_queued_then_suspended():
    """finalizer 按列表顺序写 memory，顺序变了会改变落盘次序。"""
    plan = plan_threshold_trip(
        _tasks(_t("q1", status="PENDING"), _t("s1", status="SUSPENDED")),
        pending_ids=["q1"], running_ids=set())
    assert plan.cancel_now_ids == ("q1", "s1")


def test_missing_task_in_pending_is_skipped():
    plan = plan_threshold_trip({}, pending_ids=["ghost"], running_ids=set())
    assert plan.cancel_queued == ()


def test_plan_is_frozen():
    plan = plan_threshold_trip({}, pending_ids=[], running_ids=set())
    assert isinstance(plan, TripPlan)
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.fail_roots = ()  # type: ignore[misc]
