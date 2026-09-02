"""SessionStatus 值域与状态机对齐（Task 9）。"""

from __future__ import annotations

import typing

from ctx_weft.core.orchestrator.session_state import (
    TERMINAL_SESSION_STATUSES,
    WAITING,
)
from ctx_weft.core.state.models import SessionStatus


def test_every_status_value_is_reachable_from_the_state_machine():
    """状态机表里没有的状态就不该在值域里——同 §6 不变式 1 对事件的要求。"""
    reachable = {"RUNNING", "INTERRUPTED", WAITING} | set(TERMINAL_SESSION_STATUSES)
    assert set(typing.get_args(SessionStatus)) == reachable


def test_the_paused_pair_collapsed_into_one_waiting_value():
    """PAUSED / PAUSED_HITL 的差别是「前端要不要出面板」——那是 delivery 的性质，
    前端渲染面板时已经拿到，会话状态不该复制它（docs/events-v2.md §2.1.3）。"""
    d = set(typing.get_args(SessionStatus))
    assert "PAUSED" not in d
    assert "PAUSED_HITL" not in d
    assert WAITING in d


def test_queued_and_timeout_are_gone():
    """两个值 core 从未赋过——没有赋值者的状态是死值，不该留在契约里。"""
    d = set(typing.get_args(SessionStatus))
    assert "QUEUED" not in d and "TIMEOUT" not in d
