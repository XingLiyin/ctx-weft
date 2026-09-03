from __future__ import annotations

from ctx_weft.core.orchestrator.agent_state import (
    TERMINAL_AGENT_STATUSES,
    AgentInput,
    next_agent_transition,
)


def test_idle_to_running_on_task_started():
    t = next_agent_transition("idle", AgentInput.TASK_STARTED, task_id="t1")
    assert t is not None
    assert t.status == "running"
    assert t.event_type == "AgentRunning"
    assert t.payload["trigger"] == "task_started"
    assert t.payload["from_status"] == "idle"


def test_running_to_waiting_human():
    t = next_agent_transition("running", AgentInput.AWAITING_HUMAN, hitl_id="h1", task_id="t1")
    assert t.status == "waiting_human"
    assert t.event_type == "AgentWaitingHuman"
    assert t.payload["hitl_id"] == "h1"


def test_waiting_human_back_to_running():
    t = next_agent_transition("waiting_human", AgentInput.HUMAN_RESOLVED, task_id="t1")
    assert t.status == "running"
    assert t.payload["trigger"] == "human_replied"


def test_pause_resume_round_trip():
    a = next_agent_transition("running", AgentInput.INTERRUPTED, reason="llm_outage", task_id="t1")
    assert a.status == "interrupted"
    b = next_agent_transition("interrupted", AgentInput.RESUMED, task_id="t1")
    assert b.status == "running"
    assert b.payload["trigger"] == "resumed"


def test_task_terminal_returns_agent_to_idle_not_terminal():
    """task 终态 != agent 终态——agent 回 idle 等下一条消息。"""
    t = next_agent_transition("running", AgentInput.SETTLED, reason="task_finished")
    assert t.status == "idle"
    assert t.event_type == "AgentIdle"
    assert t.status not in TERMINAL_AGENT_STATUSES


def test_only_cancel_reaches_terminated():
    for src in ("idle", "running", "waiting_human", "interrupted"):
        t = next_agent_transition(src, AgentInput.CANCEL, reason="user", cascaded_from=None)
        assert t.status == "terminated"
        assert t.event_type == "AgentTerminated"
    assert "terminated" in TERMINAL_AGENT_STATUSES


def test_terminated_is_absorbing():
    for inp in AgentInput:
        assert next_agent_transition("terminated", inp) is None


def test_no_op_transitions_return_none():
    """同态输入不产生转移，避免刷屏。"""
    assert next_agent_transition("idle", AgentInput.SETTLED) is None
    assert next_agent_transition("running", AgentInput.TASK_STARTED) is None
