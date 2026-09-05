"""领域事实不写会话状态（Task 7 · docs/events-v2.md §2.1.1）。"""

from __future__ import annotations

import pytest

from ctx_weft.core.control.reducers import reduce_events
from ctx_weft.core.utils.clock import now_utc
from ctx_weft.core.utils.ids import generate_id
from ctx_weft.protocols.events import Event, EventType


def _ev(t: EventType, payload: dict, seq: int, *, task_id: str | None = None) -> Event:
    return Event(id=generate_id("evt"), run_id="run_1", sequence=seq, session_id="sess_1",
                 type=t, timestamp=now_utc(), task_id=task_id, payload=payload)


def _created() -> Event:
    return _ev(EventType.SESSION_CREATED, {"root_agent_id": "ag_1"}, 0)


def test_hitl_opened_no_longer_pauses_the_session():
    view = reduce_events([_created(), _ev(EventType.HITL_OPENED, {
        "hitl_id": "hit_1",
        "delivery": {"kind": "tool_result", "tool_call_id": "c1"}}, 1)], "run_1")
    assert view.session_status == "RUNNING"


def test_hitl_resolved_no_longer_returns_the_session_to_running():
    view = reduce_events([
        _created(),
        # 先把会话推到 WAITING（用 L 档 setter——分支内部的 SESSION_WAITING 已于
        # 2026-09-05 删除；这里只是脚手架，断言的是 HITL_RESOLVED 不动它）。
        _ev(EventType.SESSION_STATUS_CHANGED, {"new_status": "WAITING"}, 1),
        _ev(EventType.HITL_RESOLVED, {"hitl_id": "hit_1", "outcome": "accepted"}, 2),
    ], "run_1")
    assert view.session_status == "WAITING"


def test_run_finished_no_longer_writes_the_session_status():
    view = reduce_events([_created(),
                          _ev(EventType.RUN_FINISHED, {"final_status": "FINISHED"}, 1)],
                         "run_1")
    assert view.session_status == "RUNNING"


def test_run_started_no_longer_writes_the_session_status():
    view = reduce_events([
        _created(),
        _ev(EventType.SESSION_STATUS_CHANGED, {"new_status": "WAITING"}, 1),
        _ev(EventType.RUN_STARTED, {"run_id": "run_1", "initial_step": "prepare"}, 2),
    ], "run_1")
    assert view.session_status == "WAITING"


def test_legacy_session_paused_hitl_still_folds_for_old_logs():
    # 旧模型按 form 分 PAUSED / PAUSED_HITL 两档；新值域只有 WAITING（Task 9 收敛掉
    # PAUSED/PAUSED_HITL）。L 档的职责是把旧事实翻译进当前词表，故这里恒折进 WAITING，
    # 不再区分 form。见 task-7-report.md「C1 复议」。
    view = reduce_events([_created(),
                          _ev(EventType.SESSION_PAUSED_HITL, {"form": "approval"}, 1)],
                         "run_1")
    assert view.session_status == "WAITING"


def test_legacy_session_status_changed_still_folds_for_old_logs():
    # INTERRUPTED 仍在当前值域内 → 原样通过。
    view = reduce_events([_created(),
                          _ev(EventType.SESSION_STATUS_CHANGED,
                              {"new_status": "INTERRUPTED"}, 1)], "run_1")
    assert view.session_status == "INTERRUPTED"


@pytest.mark.parametrize("legacy", ["PAUSED", "PAUSED_HITL"])
def test_legacy_session_status_changed_translates_old_vocabulary(legacy: str):
    # 存量日志里 PAUSED / PAUSED_HITL 的**主要产地**就是这条通用 setter
    # （重构前 recover() 发的 _emit_session_status(... or "PAUSED_HITL")）。
    # 它们不在新值域里，L 档必须翻译进当前词表——与 SESSION_PAUSED_HITL 分支同口径。
    view = reduce_events([_created(),
                          _ev(EventType.SESSION_STATUS_CHANGED,
                              {"new_status": legacy}, 1)], "run_1")
    assert view.session_status == "WAITING"
    assert view.sessions["sess_1"].status == "WAITING"


@pytest.mark.parametrize("status", ["RUNNING", "INTERRUPTED", "SUCCEEDED", "FAILED", "CANCELED"])
def test_legacy_session_status_changed_passes_through_current_vocabulary(status: str):
    view = reduce_events([_created(),
                          _ev(EventType.SESSION_STATUS_CHANGED,
                              {"new_status": status}, 1)], "run_1")
    assert view.session_status == status
    assert view.sessions["sess_1"].status == status
