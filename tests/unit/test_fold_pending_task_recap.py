"""段 recap 的待重跑账：折叠语义 + 它进快照往返后还在。

这个账从前是独立函数 `fold_pending_task_recap` + 恢复路径上一次按类型收窄的查询。查询随
会话长度线性增长（实测 1000 个 task 的会话：取回 1999 条折出 1 个，272ms / 5.1MB，每次
`/resume` 付一遍），所以它搬进了投影（`RunStateView.pending_recap`），与 `pending_hitl`
同一档待遇——**折叠实现只此一处**（`_apply`），不与旧函数并存。
"""

from ctx_weft.core.control.reducers import (
    deserialize_view,
    reduce_events,
    serialize_view,
)
from ctx_weft.core.events import Event
from ctx_weft.core.events.types import EventType
from ctx_weft.core.utils import generate_id, now_utc


def _ev(type_, payload):
    return Event(
        id=generate_id("evt"), run_id=None, sequence=0, session_id="ses1",
        type=type_, timestamp=now_utc(), tenant_id="default",
        task_id=payload.get("task_id"), payload=payload,
    )


def _pending(events):
    return reduce_events(events, run_id="ses1").pending_recap


def test_started_without_done_is_pending():
    assert _pending([
        _ev(EventType.TASK_RECAP_STARTED,
            {"task_id": "t1", "boundary": "finish", "agent_id": "a1"}),
    ]) == {"t1": {"boundary": "finish", "agent_id": "a1"}}


def test_started_then_done_is_empty():
    assert _pending([
        _ev(EventType.TASK_RECAP_STARTED,
            {"task_id": "t1", "boundary": "finish", "agent_id": "a1"}),
        _ev(EventType.TASK_RECAP_DONE, {"task_id": "t1"}),
    ]) == {}


def test_last_write_wins_per_task():
    # 同 task 二次 started（如 recover 又崩一次）：以最后一次 boundary 为准
    assert _pending([
        _ev(EventType.TASK_RECAP_STARTED,
            {"task_id": "t1", "boundary": "interrupt", "agent_id": "a1"}),
        _ev(EventType.TASK_RECAP_DONE, {"task_id": "t1"}),
        _ev(EventType.TASK_RECAP_STARTED,
            {"task_id": "t1", "boundary": "finish", "agent_id": "a1"}),
    ]) == {"t1": {"boundary": "finish", "agent_id": "a1"}}


def test_key_comes_from_the_payload_not_the_event_header():
    """键取 payload 的 `task_id`，不是 `ev.task_id`——段边界上事件头可能是父任务。

    取错了会把待重跑的账记到父任务名下：恢复时按父任务重跑，那个子段的 memory 写永远
    补不上，而且不报错。这里刻意让两者不同，只有读对了才过。
    """
    ev = Event(
        id=generate_id("evt"), run_id=None, sequence=0, session_id="ses1",
        type=EventType.TASK_RECAP_STARTED, timestamp=now_utc(), tenant_id="default",
        task_id="t_parent",                                   # 事件头：父任务
        payload={"task_id": "t_child", "boundary": "finish", "agent_id": "a1"},
    )

    assert _pending([ev]) == {"t_child": {"boundary": "finish", "agent_id": "a1"}}


# ── 进快照往返 ────────────────────────────────────────────────────────────────


def test_pending_recap_survives_a_snapshot_round_trip():
    """它进投影的全部意义就在这条：写进 blob、读回来还在。

    掉了的话，恢复会认为「没有待重跑的 recap」，那段 memory 写就永远补不上——而且不报错。
    """
    view = reduce_events([
        _ev(EventType.TASK_RECAP_STARTED,
            {"task_id": "t_stuck", "boundary": "finish", "agent_id": "a1"}),
    ], run_id="ses1")

    back = deserialize_view(serialize_view(view))

    assert back.pending_recap == {"t_stuck": {"boundary": "finish", "agent_id": "a1"}}


def test_old_blob_without_the_key_deserializes_to_empty():
    """存量 blob 没有这个键 → 空 dict，不是 KeyError。

    本分支的 `RunSnapshot` 没有 `projection_version`，没有东西能把老 blob 判废，所以新投影
    字段只能靠这里的默认值兜——这条钉的就是「兜得住、不炸」。代价是部署后每个会话的第一次
    恢复可能把「有崩溃打断的 recap」读成「没有」；下一轮对话结束写新快照就自愈（见
    `deserialize_view` 的 docstring）。
    """
    assert deserialize_view({"session_id": "ses1"}).pending_recap == {}


def test_delta_apply_onto_an_old_view_still_accrues_the_account():
    """老快照当增量基底时，**增量里**的 recap 事件仍会正常记账。

    即「缺键」只丢快照位点之前的账，不会把这个机制整个关掉——自愈靠的就是这一点。
    """
    from ctx_weft.core.control.reducers import apply_events

    base = deserialize_view({"session_id": "ses1"})          # 老 blob，无该键
    view = apply_events([
        _ev(EventType.TASK_RECAP_STARTED,
            {"task_id": "t_new", "boundary": "finish", "agent_id": "a1"}),
    ], base)

    assert view.pending_recap == {"t_new": {"boundary": "finish", "agent_id": "a1"}}
