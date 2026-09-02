"""会话状态机：**唯一**一份「什么状态下、什么输入、转到哪」的判据。

会话状态回答的是「这个会话健康吗、还能不能自己往前走」，不是「卡在哪」——
后者是 task 的事。故只有 6 个值：RUNNING（有活在跑）/ WAITING（停着但正常）/
INTERRUPTED（停着且异常）/ 三个终态（SUCCEEDED / FAILED / CANCELED）。

为什么单独成模块、且是纯函数：今天这份判据散在 5 个地方各写一遍——
`task_manager.py:818` / `:799` / `:804`、`reducers.py:562`、host 投影里的
「不得覆盖已落终态」守卫。它们口径不同、位置分散，正是通用 setter
`SessionStatusChanged` 存在的土壤（docs/events-v2.md §2.1.1）。

**这里看不见 HITL。** 会话不知道有没有人在等回话，只知道 TaskManager 报了
「我没有能跑的了，因为有人在等」。分层见 docs/events-v2.md §2.1.1。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "TERMINAL_SESSION_STATUSES",
    "WAITING",
    "SessionInput",
    "Transition",
    "next_transition",
]

#: 会话终态。到达之后任何输入都不再引发转移。
TERMINAL_SESSION_STATUSES: frozenset[str] = frozenset({"SUCCEEDED", "FAILED", "CANCELED"})

#: 「停着但正常」的那个状态。**只有一个**——「等的是审批面板还是一句话」是
#: `HitlOpened.delivery` 的性质，前端渲染面板时已经拿到，会话状态不复制它。
WAITING: str = "WAITING"


class SessionInput(StrEnum):
    """SM 的全部输入。前四个来自 TaskManager 的事件，`CANCEL` 是外部命令。

    **每一个都是一个独立的输入，不靠 payload 里的字段区分**——收到哪个就转到哪，
    这是本次重构的核心约束（见计划的 Global Constraints）。
    """

    QUEUE_BLOCKED = "queue_blocked"           # TaskQueueBlocked：都停着，正常
    QUEUE_INTERRUPTED = "queue_interrupted"   # TaskQueueInterrupted：都停着，异常
    QUEUE_DRAINED = "queue_drained"           # TaskQueueDrained：全部终态
    TASK_STARTED = "task_started"             # TaskStarted：有活在跑 = 会话在跑
    CANCEL = "cancel"                         # 外部硬取消命令


@dataclass(frozen=True)
class Transition:
    """一次转移的完整结果：新状态 + SM 该发哪条事件 + 该事件的 payload。"""

    status: str
    event_type: str
    payload: dict


def next_transition(
    current: str,
    inp: SessionInput,
    *,
    reason: str = "",
    final_status: str = "",
) -> Transition | None:
    """当前状态 + 一个输入 → 转移；`None` = 不转移、不发事件。

    返回 `None` 而不是「转到自己」是刻意的：SM 据此决定**发不发事件**。TM 每次
    聚合都可能重发同一条信号，状态没变就不该刷前端。
    """
    if current in TERMINAL_SESSION_STATUSES:
        return None                                  # 已终态：任何输入都不转移

    if inp is SessionInput.CANCEL:
        return Transition("CANCELED", "SessionFinished", {"final_status": "CANCELED"})

    if inp is SessionInput.QUEUE_BLOCKED:
        if current == WAITING:
            return None
        return Transition(WAITING, "SessionWaiting", {})

    if inp is SessionInput.QUEUE_INTERRUPTED:
        if current == "INTERRUPTED":
            return None
        return Transition("INTERRUPTED", "SessionInterrupted", {"reason": reason})

    if inp is SessionInput.TASK_STARTED:
        if current == "RUNNING":
            return None
        # 从哪种停顿里回来，决定 reason——这是溯源，不是判据：转移到 RUNNING
        # 这件事本身不依赖它。
        return Transition("RUNNING", "SessionRunning",
                          {"reason": "human_replied" if current == WAITING else "resumed"})

    if inp is SessionInput.QUEUE_DRAINED:
        # 还有人在等回话、或会话处于中断态时不终结——绝不把 parked / 挂起的任务孤立
        # （spec/07 §9.1）。TM 本就不该在这两种情况下报 drained，这里是第二道闸。
        if current in (WAITING, "INTERRUPTED"):
            return None
        return Transition(final_status, "SessionFinished", {"final_status": final_status})

    return None
