"""Task and agent lifecycle vocabularies and terminal/park predicates."""
from __future__ import annotations

from typing import Literal

__all__ = [
    "PARKED_TASK_STATUSES",
    "TERMINAL_AGENT_STATUSES",
    "TERMINAL_TASK_STATUSES",
    "AgentStatus",
    "TaskStatus",
]


TaskStatus = Literal[
    "PENDING",
    "ACTIVE",
    "REVIEWING",         # execution handed off; background terminal review pending
    "SUSPENDED",         # 等子任务完成——**只剩这一个语义**
    "AWAITING_HUMAN",    # 被 HITL 挂起，需要人来解决
    "INTERRUPTED",       # 被外部打断（LLM outage / run 崩溃），等 /resume
    "FINISHED",
    "FAILED",
    "CANCELED",
]

#: 已经坐实的 task 终态。`TaskManager` 写过其中之一（如熔断 trip 的 root 判死）之后，
#: run 的结局不得再把它盖掉。
TERMINAL_TASK_STATUSES: frozenset[str] = frozenset({"FINISHED", "FAILED", "CANCELED"})

#: 非终态的「停下来了」：run 已经退出、任务还没做完。三者的区别在于**解开它需要谁**
#: ——等子任务（自愈）/ 等人答一句 / 等运维 `/resume`。判据是 `task.status`，不是任何
#: 字面量。
PARKED_TASK_STATUSES: frozenset[str] = frozenset(
    {"SUSPENDED", "AWAITING_HUMAN", "INTERRUPTED"}
)


# ── Agent ─────────────────────────────────────────────────────────────────────

AgentStatus = Literal["idle", "running", "waiting_human", "interrupted", "terminated"]

#: agent 的**唯一**终态，且**只**由外部显式 cancel 触发。task 终态让 agent 回 `idle`
#: 而不是终态——agent 是跨多轮的容器，可以接新消息开新 task（spec 3.1）。
TERMINAL_AGENT_STATUSES: frozenset[str] = frozenset({"terminated"})
