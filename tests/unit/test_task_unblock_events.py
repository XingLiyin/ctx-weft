"""「解除阻塞」与「开始执行」是两件事。

三对括号里，AwaitingHuman 那一对此前只有左半边：
  TaskSuspended（等子任务）      ←→ TaskResumed
  TaskAwaitingHuman{hitl_id}     ←→ (缺)
  RunInterrupted                 ←→ (缺，范围外)
"""
from __future__ import annotations

from ctx_weft.core.control.reducers import TASK_STATUS_BY_EVENT, reduce_events
from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols.events import Event, EventType


def _ev(t, task_id, payload=None):
    return Event(
        id=generate_id("evt"), run_id=None, sequence=0, session_id="s1",
        type=t, timestamp=now_utc(), task_id=task_id, payload=payload or {},
    )


def _created(task_id):
    return _ev(EventType.TASK_CREATED, task_id,
               {"task": {"id": task_id, "session_id": "s1", "status": "PENDING"}})


def test_human_resolved_returns_task_to_pending():
    view = reduce_events([
        _created("tsk_1"),
        _ev(EventType.TASK_AWAITING_HUMAN, "tsk_1", {"hitl_id": "hit_1"}),
        _ev(EventType.TASK_HUMAN_RESOLVED, "tsk_1", {"hitl_id": "hit_1"}),
    ], run_id="run_1")
    assert view.tasks["tsk_1"].status == "PENDING"


def test_awaiting_and_resolved_share_the_hitl_id():
    """配对：同一个 hitl_id 把被挡住的区间括起来。"""
    evs = [
        _created("tsk_1"),
        _ev(EventType.TASK_AWAITING_HUMAN, "tsk_1", {"hitl_id": "hit_1"}),
        _ev(EventType.TASK_HUMAN_RESOLVED, "tsk_1", {"hitl_id": "hit_1"}),
    ]
    opened = [e for e in evs if e.type == EventType.TASK_AWAITING_HUMAN]
    closed = [e for e in evs if e.type == EventType.TASK_HUMAN_RESOLVED]
    assert opened[0].payload["hitl_id"] == closed[0].payload["hitl_id"]


def test_task_resumed_maps_to_pending_not_active():
    """ACTIVE 的正主是 TaskStarted —— 它由 TM 在派发时发并回填 assigned_agent_id。
    解挂之后、派发之前，task 在队列里，投影不该说它在跑。"""
    assert TASK_STATUS_BY_EVENT[EventType.TASK_RESUMED] == "PENDING"


def test_active_still_comes_from_task_started():
    view = reduce_events([
        _created("tsk_1"),
        _ev(EventType.TASK_SUSPENDED, "tsk_1", {"summary": "", "spawn_titles": []}),
        _ev(EventType.TASK_RESUMED, "tsk_1"),
    ], run_id="run_1")
    assert view.tasks["tsk_1"].status == "PENDING"
    view2 = reduce_events([
        _created("tsk_1"),
        _ev(EventType.TASK_STARTED, "tsk_1", {"assigned_agent_id": "agt_1"}),
    ], run_id="run_1")
    assert view2.tasks["tsk_1"].status == "ACTIVE"


def test_human_resolved_clears_outputs():
    """与 TaskRequeued 同一效果：解除阻塞回 PENDING 要清旧产出（判据是类型，不是 payload）。"""
    view = reduce_events([
        _created("tsk_1"),
        _ev(EventType.TASK_FINALIZED, "tsk_1", {"outputs": "done"}),
        _ev(EventType.TASK_AWAITING_HUMAN, "tsk_1", {"hitl_id": "hit_1"}),
        _ev(EventType.TASK_HUMAN_RESOLVED, "tsk_1", {"hitl_id": "hit_1"}),
    ], run_id="run_1")
    assert view.tasks["tsk_1"].outputs is None
