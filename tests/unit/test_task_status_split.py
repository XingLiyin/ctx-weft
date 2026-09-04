"""task 层把「为什么停」变成类型，而不是 reason 字符串（Task 2）。

状态值域的过载是事件过载的根：一个 SUSPENDED 盖住「等子任务」/「等人」/「被打断」，
消费方只能去匹配 TaskSuspended.reason。拆值域 + 拆类型必须一起做。
"""

from __future__ import annotations

import typing

from ctx_weft.core.control.reducers import TASK_STATUS_BY_EVENT, reduce_events
from ctx_weft.core.models.status import TaskStatus
from ctx_weft.core.util import generate_id, now_utc
from ctx_weft.protocols.events import Event, EventType


def _ev(t: EventType, payload: dict, seq: int, *, task_id: str = "task_1") -> Event:
    return Event(id=generate_id("evt"), run_id="run_1", sequence=seq, session_id="sess_1",
                 type=t, timestamp=now_utc(), task_id=task_id or None, payload=payload)


def _seed() -> list[Event]:
    return [
        _ev(EventType.SESSION_CREATED, {"root_agent_id": "ag_1"}, 0, task_id=""),
        _ev(EventType.TASK_CREATED, {"task": {"id": "task_1", "status": "PENDING"}}, 1),
    ]


def test_task_status_domain_distinguishes_the_three_reasons_to_stop():
    d = set(typing.get_args(TaskStatus))
    assert {"SUSPENDED", "AWAITING_HUMAN", "INTERRUPTED"} <= d


def test_task_awaiting_human_projects_to_awaiting_human():
    view = reduce_events(_seed() + [
        _ev(EventType.TASK_AWAITING_HUMAN, {"hitl_id": "hit_1"}, 2),
    ], "run_1")
    assert view.tasks["task_1"].status == "AWAITING_HUMAN"


def test_task_interrupted_projects_the_task_to_interrupted():
    """「被打断」的 task 状态由 task 域的 TaskInterrupted 写。

    run 域的 RunInterrupted 只说「这次执行死了」，不写 task 状态——那次 run 死了
    不等于 task 停在 INTERRUPTED（还能重试的走 TaskRequeued → PENDING）。
    """
    view = reduce_events(_seed() + [
        _ev(EventType.TASK_INTERRUPTED, {"reason": "llm_outage"}, 2),
    ], "run_1")
    assert view.tasks["task_1"].status == "INTERRUPTED"


def test_both_new_types_are_in_the_status_map():
    """漏进这张表 = 事件发了但投影不动，冷重建看不见。"""
    assert TASK_STATUS_BY_EVENT[EventType.TASK_AWAITING_HUMAN] == "AWAITING_HUMAN"
    assert TASK_STATUS_BY_EVENT[EventType.TASK_INTERRUPTED] == "INTERRUPTED"


def test_legacy_task_suspended_still_projects_to_suspended():
    """L 档：旧事件带着 reason=hitl_park / run_crash，reducer 要继续认。"""
    view = reduce_events(_seed() + [
        _ev(EventType.TASK_SUSPENDED, {"reason": "hitl_park"}, 2),
    ], "run_1")
    assert view.tasks["task_1"].status == "SUSPENDED"


def test_new_types_do_not_reuse_any_legacy_string():
    assert EventType.TASK_AWAITING_HUMAN == "TaskAwaitingHuman"
    assert EventType.RUN_INTERRUPTED == "RunInterrupted"
    assert EventType.TASK_INTERRUPTED == "TaskInterrupted"
