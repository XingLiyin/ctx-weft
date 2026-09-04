"""failure_counter 真折叠：TASK_FAILED +1，TASK_FINISHED 清零，FAILURE_THRESHOLD_HIT 不再 +1。

核心语义：failure_counter 是连败计数。TASK_FAILED（普通失败）累计；
TASK_FINISHED（成功）清零；TASK_FAILED_BY_THRESHOLD（熔断聚合失败）不算新败。
"""

from __future__ import annotations

from datetime import datetime, timezone

from ctx_weft.core.control.reducers import (
    reduce_events, serialize_view, deserialize_view, apply_events
)
from ctx_weft.core.control.types import RunStateView
from ctx_weft.protocols.events import Event, EventType


def _ts() -> datetime:
    return datetime(2026, 6, 5, tzinfo=timezone.utc)


def _ev(seq: int, type_: str, task_id: str | None = None, **payload) -> Event:
    """构造一个 session=s1 的事件。"""
    return Event(
        id=f"evt_{seq:04d}",
        run_id="run_1",
        sequence=seq,
        session_id="s1",
        type=type_,
        timestamp=_ts(),
        task_id=task_id,
        payload=payload,
    )


def _session_created() -> Event:
    return _ev(1, EventType.SESSION_CREATED,
               user_prompt="test", root_agent_id="agt_root")


def _task_created(seq: int, task_id: str = "tsk_1") -> Event:
    return _ev(seq, EventType.TASK_CREATED, task_id=task_id, task={
        "id": task_id, "status": "PENDING", "title": "T1",
        "assigned_agent_id": "agt_root", "creator_agent_id": "agt_root",
    })


def _task_started(seq: int, task_id: str = "tsk_1") -> Event:
    return _ev(seq, EventType.TASK_STARTED, task_id=task_id,
               assigned_agent_id="agt_root")


def _task_finished(seq: int, task_id: str = "tsk_1") -> Event:
    return _ev(seq, EventType.TASK_FINISHED, task_id=task_id)


def _task_failed(seq: int, task_id: str = "tsk_1", error_code: str | None = None) -> Event:
    """Task failed event. If error_code provided, it's added to payload."""
    payload = {}
    if error_code is not None:
        payload["error_code"] = error_code
    return _ev(seq, EventType.TASK_FAILED, task_id=task_id, **payload)


def _failure_threshold_hit(seq: int) -> Event:
    """Legacy FAILURE_THRESHOLD_HIT event."""
    return _ev(seq, EventType.FAILURE_THRESHOLD_HIT)


def test_task_failed_increments_counter() -> None:
    """单个 TASK_FAILED → counter == 1。"""
    events = [
        _session_created(),
        _task_created(2),
        _task_started(3),
        _task_failed(4),
    ]
    view = reduce_events(events, run_id="run_1")
    assert view.sessions["s1"].failure_counter == 1


def test_multiple_task_failures_accumulate() -> None:
    """TASK_FAILED ×2 → counter == 2。"""
    events = [
        _session_created(),
        _task_created(2, task_id="tsk_1"),
        _task_started(3, task_id="tsk_1"),
        _task_failed(4, task_id="tsk_1"),  # counter = 1
        _task_created(5, task_id="tsk_2"),
        _task_started(6, task_id="tsk_2"),
        _task_failed(7, task_id="tsk_2"),  # counter = 2
    ]
    view = reduce_events(events, run_id="run_1")
    assert view.sessions["s1"].failure_counter == 2


def test_task_finished_resets_counter() -> None:
    """TASK_FAILED, TASK_FINISHED → counter 清零。"""
    events = [
        _session_created(),
        _task_created(2),
        _task_started(3),
        _task_failed(4),  # counter = 1
        _task_finished(5),  # counter = 0
    ]
    view = reduce_events(events, run_id="run_1")
    assert view.sessions["s1"].failure_counter == 0


def test_task_failed_reset_failed_again() -> None:
    """TASK_FAILED, TASK_FINISHED, TASK_FAILED → counter == 1（清零后重计）。"""
    events = [
        _session_created(),
        _task_created(2, task_id="tsk_1"),
        _task_started(3, task_id="tsk_1"),
        _task_failed(4, task_id="tsk_1"),  # counter = 1
        _task_finished(5, task_id="tsk_1"),  # counter = 0
        _task_created(6, task_id="tsk_2"),
        _task_started(7, task_id="tsk_2"),
        _task_failed(8, task_id="tsk_2"),  # counter = 1
    ]
    view = reduce_events(events, run_id="run_1")
    assert view.sessions["s1"].failure_counter == 1


def test_task_failed_by_threshold_not_counted() -> None:
    """熔断给 root 判死的那条 TASK_FAILED 不计——它是聚合结果，不是新败。

    判据已从 payload 的 `error_code` 改为**前置的 FAILURE_THRESHOLD_HIT 事件**，
    故此处补上那条事件。这不是为了迁就实现而放宽断言：改造前的写法构造了一条
    **生产者造不出来的**事件流——`TASK_FAILED_BY_THRESHOLD` 的唯一发射点是
    `TaskManager._trip_failure_threshold` 第 6 步，而同一函数第 2 步无条件先发
    `FAILURE_THRESHOLD_HIT`，两者恒成对且顺序固定。`error_code` 仍照发（对外契约
    不变），只是 reducer 不再拿它当判据。
    """
    events = [
        _session_created(),
        _task_created(2),
        _task_started(3),
        _task_failed(4),  # 普通失败，counter = 1
        _failure_threshold_hit(5),  # trip 第 2 步
        _task_failed(6, error_code="TASK_FAILED_BY_THRESHOLD"),  # trip 第 6 步，不计
    ]
    view = reduce_events(events, run_id="run_1")
    assert view.sessions["s1"].failure_counter == 1


def test_failure_threshold_hit_not_counted() -> None:
    """FAILURE_THRESHOLD_HIT 单独出现 → counter unchanged (0)。

    此事件在旧实现里驱动 +1，新实现里不再驱动计数（只标记熔断发生）。
    """
    events = [
        _session_created(),
        _failure_threshold_hit(2),
    ]
    view = reduce_events(events, run_id="run_1")
    # counter 不应改变，保持默认值 0
    assert view.sessions["s1"].failure_counter == 0


def test_snapshot_roundtrip_preserves_failure_counter() -> None:
    """快照往返：折叠后经快照重建 counter 保留。"""
    # 构造一个有两次失败的事件序列
    events = [
        _session_created(),
        _task_created(2, task_id="tsk_1"),
        _task_started(3, task_id="tsk_1"),
        _task_failed(4, task_id="tsk_1"),  # counter = 1
        _task_created(5, task_id="tsk_2"),
        _task_started(6, task_id="tsk_2"),
        _task_failed(7, task_id="tsk_2"),  # counter = 2
    ]

    # 全量回放得到 counter == 2
    full_view = reduce_events(events, run_id="run_1")
    assert full_view.sessions["s1"].failure_counter == 2

    # 序列化为快照，再反序列化
    snapshot_data = serialize_view(full_view)
    restored_view = deserialize_view(snapshot_data)
    assert restored_view.sessions["s1"].failure_counter == 2

    # 快照恢复后继续应用增量事件，counter 应继续工作
    delta_events = [
        _task_finished(8, task_id="tsk_2"),  # counter = 0
        _task_created(9, task_id="tsk_3"),
        _task_started(10, task_id="tsk_3"),
        _task_failed(11, task_id="tsk_3"),  # counter = 1
    ]
    final_view = apply_events(delta_events, restored_view)
    assert final_view.sessions["s1"].failure_counter == 1


# ── 熔断闩位：判据是事件，不是 payload 字符串 ────────────────────────────────


def _session_resumed(seq: int) -> Event:
    return _ev(seq, EventType.SESSION_RESUMED, root_agent_id="agt_root")


def test_threshold_hit_latches_and_stops_counting() -> None:
    """FAILURE_THRESHOLD_HIT 之后的 TASK_FAILED 不计——不看 error_code。

    真实 trip 序列：第 2 步发 HIT，第 6 步 root 判死发 TASK_FAILED。
    reducer 据前者置闩，后者因此不计。
    """
    events = [
        _session_created(),
        _task_created(2, task_id="tsk_1"),
        _task_started(3, task_id="tsk_1"),
        _task_failed(4, task_id="tsk_1"),        # 普通失败，counter = 1
        _failure_threshold_hit(5),               # 熔断闩位
        _task_failed(6, task_id="tsk_root"),     # root 判死，不计
    ]
    view = reduce_events(events, run_id="run_1")
    assert view.sessions["s1"].failure_counter == 1
    assert view.sessions["s1"].threshold_tripped is True


def test_latch_holds_for_late_inflight_failures() -> None:
    """trip 之后在途任务的迟到失败同样不计——会话已终结，不存在“又一次新败”。"""
    events = [
        _session_created(),
        _task_created(2, task_id="tsk_1"),
        _task_started(3, task_id="tsk_1"),
        _task_failed(4, task_id="tsk_1"),        # counter = 1
        _failure_threshold_hit(5),
        _task_failed(6, task_id="tsk_root"),     # root 判死
        _task_failed(7, task_id="tsk_2"),        # 在途任务迟到失败
    ]
    view = reduce_events(events, run_id="run_1")
    assert view.sessions["s1"].failure_counter == 1


def test_session_resumed_clears_the_latch() -> None:
    """续跑开新一轮：闩位清掉，新一轮的失败照常计数。

    与内存侧同形——resume_session 造的是全新 Session（failure_counter=0）
    和全新 TaskManager（_threshold_tripped=False）。
    """
    events = [
        _session_created(),
        _task_created(2, task_id="tsk_1"),
        _task_started(3, task_id="tsk_1"),
        _task_failed(4, task_id="tsk_1"),
        _failure_threshold_hit(5),
        _task_failed(6, task_id="tsk_root"),
        _session_resumed(7),
        _task_created(8, task_id="tsk_3"),
        _task_started(9, task_id="tsk_3"),
        _task_failed(10, task_id="tsk_3"),       # 新一轮的失败，照常计
    ]
    view = reduce_events(events, run_id="run_1")
    assert view.sessions["s1"].threshold_tripped is False
    assert view.sessions["s1"].failure_counter == 2


def test_snapshot_roundtrip_preserves_the_latch() -> None:
    """闩位必须随快照往返——否则快照点之后的迟到失败会被重新计入。"""
    events = [
        _session_created(),
        _task_created(2, task_id="tsk_1"),
        _task_started(3, task_id="tsk_1"),
        _task_failed(4, task_id="tsk_1"),
        _failure_threshold_hit(5),
    ]
    view = reduce_events(events, run_id="run_1")
    assert view.sessions["s1"].threshold_tripped is True

    restored = deserialize_view(serialize_view(view))
    assert restored.sessions["s1"].threshold_tripped is True

    final = apply_events([_task_failed(6, task_id="tsk_root")], restored)
    assert final.sessions["s1"].failure_counter == 1
