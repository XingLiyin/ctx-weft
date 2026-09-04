"""三种实体的生命周期状态词表 + 终态与 park 判据。

**为什么合在一处**：它们是同一套东西——host 按这些字符串分流，改值等于改对外契约。
改造前它们散在四个地方，同一个概念的两个实例甚至一个公开一个私有：

    SessionStatus / TaskStatus / TERMINAL_SESSION_STATUSES   core/state/models.py
    AgentStatus / TERMINAL_AGENT_STATUSES                    orchestrator/agent_state.py
    task 的终态与 park 判据（两个私有 frozenset）             orchestrator/task_manager.py
    终态三元组的第四份逐字复制                                 loop/steps/act_guidance.py

**纯 stdlib 叶子**：本模块不 import 任何 `ctx_weft` 运行期东西。词表在这里，
状态机在别处——`orchestrator/agent_state.py`（agent 五态机）与
`orchestrator/task_disposition.py`（run 结局 → task 处置）都是只吃词表的纯函数层。
"""

from __future__ import annotations

from typing import Literal

__all__ = [
    "PARKED_TASK_STATUSES",
    "TERMINAL_AGENT_STATUSES",
    "TERMINAL_SESSION_STATUSES",
    "TERMINAL_TASK_STATUSES",
    "WAITING",
    "AgentStatus",
    "SessionStatus",
    "TaskStatus",
]


# ── Session ───────────────────────────────────────────────────────────────────

SessionStatus = Literal[
    "RUNNING",          # 有 task 在跑
    "WAITING",          # 停着，但正常——都在等人 / 等外部输入
    "INTERRUPTED",      # 停着，异常——系统故障，等 /resume（非终态）
    "SUCCEEDED",
    "FAILED",
    "CANCELED",
]
# 已删除：`QUEUED` / `TIMEOUT`（core 从未赋值）；`PAUSED` / `PAUSED_HITL`
# （两者的差别是「前端要不要出面板」，那是 `HitlOpened.delivery` 的性质，不是会话状态，
# 已合并成 `WAITING`）。存量日志里的旧值由 `core.control.reducers` 折叠，见
# `docs/upgrade/2026-09-02-session-status-ownership.md`。

#: 会话终态。到达之后任何输入都不再引发转移（`core.control.reducers` 据此拒绝迟到的
#: `SessionRunning` 复活一个已收尾的会话）。
TERMINAL_SESSION_STATUSES: frozenset[str] = frozenset({"SUCCEEDED", "FAILED", "CANCELED"})

#: 「停着但正常」的那个状态。**只有一个**——「等的是审批面板还是一句话」是
#: `HitlOpened.delivery` 的性质，前端渲染面板时已经拿到，会话状态不复制它。
WAITING: str = "WAITING"


# ── Task ──────────────────────────────────────────────────────────────────────

TaskStatus = Literal[
    "PENDING",
    "ACTIVE",
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
