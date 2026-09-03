"""agent 五态机的纯函数层。

与 `session_state.py` 同构：只做「当前状态 + 输入 -> 下一状态 + 该发哪条事件」，
不碰事件总线、不持有实例状态。副作用全在 `AgentRegistry`（lifecycle manager）。

设计要点（spec 3.1）：
- `idle` 既是初始态，也是每轮交互处理完后回到的态；它**不等于**「没有活着的 task」。
- task 终态（finished/failed/canceled/finalized）让 agent 回 `idle` 而**不是**终态——
  agent 是跨多轮的容器，可以接新消息开新 task。
- 真正的终态 `terminated` **只**由外部显式 cancel 触发。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

AgentStatus = Literal["idle", "running", "waiting_human", "interrupted", "terminated"]

TERMINAL_AGENT_STATUSES: frozenset[str] = frozenset({"terminated"})


class AgentInput(StrEnum):
    """状态机输入。由 ALM 从 TASK_* 事件类型翻译而来——判据是类型，不是 payload 文本。"""

    TASK_STARTED = "task_started"
    AWAITING_HUMAN = "awaiting_human"
    HUMAN_RESOLVED = "human_resolved"
    INTERRUPTED = "interrupted"
    RESUMED = "resumed"
    SETTLED = "settled"
    CANCEL = "cancel"


@dataclass(frozen=True)
class AgentTransition:
    status: str
    event_type: str
    payload: dict = field(default_factory=dict)


def next_agent_transition(
    current: str,
    inp: AgentInput,
    *,
    task_id: str | None = None,
    hitl_id: str = "",
    reason: str = "",
    cascaded_from: str | None = None,
) -> AgentTransition | None:
    """返回 None 表示不转移（同态输入，或已在终态）。"""
    if current in TERMINAL_AGENT_STATUSES:
        return None

    if inp is AgentInput.CANCEL:
        return AgentTransition(
            "terminated",
            "AgentTerminated",
            {"from_status": current, "reason": reason, "cascaded_from": cascaded_from},
        )

    if inp is AgentInput.TASK_STARTED:
        if current == "running":
            return None
        return AgentTransition(
            "running",
            "AgentRunning",
            {"from_status": current, "task_id": task_id, "trigger": "task_started"},
        )

    if inp is AgentInput.AWAITING_HUMAN:
        if current == "waiting_human":
            return None
        return AgentTransition(
            "waiting_human",
            "AgentWaitingHuman",
            {"from_status": current, "hitl_id": hitl_id, "task_id": task_id},
        )

    if inp is AgentInput.HUMAN_RESOLVED:
        if current == "running":
            return None
        return AgentTransition(
            "running",
            "AgentRunning",
            {"from_status": current, "task_id": task_id, "trigger": "human_replied"},
        )

    if inp is AgentInput.INTERRUPTED:
        if current == "interrupted":
            return None
        return AgentTransition(
            "interrupted",
            "AgentInterrupted",
            {"from_status": current, "reason": reason, "task_id": task_id},
        )

    if inp is AgentInput.RESUMED:
        if current == "running":
            return None
        return AgentTransition(
            "running",
            "AgentRunning",
            {"from_status": current, "task_id": task_id, "trigger": "resumed"},
        )

    if inp is AgentInput.SETTLED:
        if current == "idle":
            return None
        return AgentTransition(
            "idle",
            "AgentIdle",
            {"from_status": current, "reason": reason},
        )

    return None
