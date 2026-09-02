"""会话状态机：状态 × 输入 → 转移。纯函数，无 IO（Task 1）。

判据来自 docs/events-v2.md §2.1.3 的转移表。输入全部来自 TaskManager 的信号
或外部命令——这里不出现任何 HITL 概念，那是 SM 看不见的层。
"""

from __future__ import annotations

import pytest

from ctx_weft.core.orchestrator.session_state import (
    TERMINAL_SESSION_STATUSES,
    SessionInput,
    next_transition,
)


def test_queue_blocked_goes_waiting():
    t = next_transition("RUNNING", SessionInput.QUEUE_BLOCKED)
    assert t is not None
    assert t.status == "WAITING"
    assert t.event_type == "SessionWaiting"


def test_blocked_twice_emits_nothing():
    """TM 每次聚合都可能重发信号；状态没变就不该刷前端。"""
    assert next_transition("WAITING", SessionInput.QUEUE_BLOCKED) is None


def test_interrupted_beats_waiting():
    """等人的时候又断了：异常压过正常，会话该显示「出事了」。"""
    t = next_transition("WAITING", SessionInput.QUEUE_INTERRUPTED, reason="llm_outage")
    assert t.status == "INTERRUPTED"


def test_waiting_after_interrupted_is_allowed():
    """断的那个被解决了，只剩等人的 → 回落到 WAITING。"""
    t = next_transition("INTERRUPTED", SessionInput.QUEUE_BLOCKED)
    assert t.status == "WAITING"


def test_task_started_returns_a_paused_session_to_running():
    t = next_transition("WAITING", SessionInput.TASK_STARTED)
    assert t.status == "RUNNING"
    assert t.event_type == "SessionRunning"
    assert t.payload == {"reason": "human_replied"}


def test_task_started_returns_an_interrupted_session_to_running():
    t = next_transition("INTERRUPTED", SessionInput.TASK_STARTED)
    assert t.status == "RUNNING" and t.payload == {"reason": "resumed"}


def test_task_started_while_already_running_emits_nothing():
    assert next_transition("RUNNING", SessionInput.TASK_STARTED) is None


def test_queue_interrupted_goes_interrupted():
    t = next_transition("RUNNING", SessionInput.QUEUE_INTERRUPTED, reason="llm_outage")
    assert t.status == "INTERRUPTED"
    assert t.event_type == "SessionInterrupted"
    assert t.payload == {"reason": "llm_outage"}


def test_queue_drained_finishes_with_the_given_final_status():
    t = next_transition("RUNNING", SessionInput.QUEUE_DRAINED, final_status="SUCCEEDED")
    assert t.status == "SUCCEEDED"
    assert t.event_type == "SessionFinished"
    assert t.payload == {"final_status": "SUCCEEDED"}


def test_cancel_finishes_from_paused_too():
    t = next_transition("WAITING", SessionInput.CANCEL)
    assert t.status == "CANCELED"
    assert t.event_type == "SessionFinished"
    assert t.payload == {"final_status": "CANCELED"}


def test_drained_while_blocked_on_human_does_not_finish():
    """TM 不该在还有人等回话时报 drained；万一报了，状态机也不许把会话终结掉
    ——绝不把 parked 任务孤立（spec/07 §9.1）。"""
    assert next_transition("WAITING", SessionInput.QUEUE_DRAINED,
                           final_status="SUCCEEDED") is None


def test_drained_while_interrupted_does_not_finish():
    assert next_transition("INTERRUPTED", SessionInput.QUEUE_DRAINED,
                           final_status="SUCCEEDED") is None


@pytest.mark.parametrize("terminal", sorted(TERMINAL_SESSION_STATUSES))
@pytest.mark.parametrize("inp", list(SessionInput))
def test_terminal_states_never_transition(terminal, inp):
    """「已终态不被覆盖」的唯一落点。今天这条守卫在 5 个地方各写一遍。"""
    assert next_transition(terminal, inp, final_status="SUCCEEDED") is None
