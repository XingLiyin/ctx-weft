"""run 结局 + 重试预算 → task 处置。**唯一**一份「task 下一步是什么」的判据。

为什么单独成模块、且是纯函数：这份判断今天散在四个地方——
`FinalizeStep` 判重试耗尽、`ObserveStep._apply_assessment` 把 verdict 写成状态、
`TaskManager._handle_task_failure` 判重试预算、`_run_loop` 的 except 链写挂起态。
把它们收进一张表，`TaskManager` 才可能成为 task 状态的唯一改写者
（docs/events-v2.md §2.1 的分层原则推到 task 层）。

纯函数是刻意的：它不该知道事件总线、不该知道队列、不该 await 任何东西。
loop 报「发生了什么」，这张表回答「那么 task 变成什么」，TaskManager 负责执行。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["Disposition", "RunOutcome", "RunOutcomeKind", "disposition_for"]


class RunOutcomeKind(StrEnum):
    """一次 run 是怎么结束的。**run 自己的词表**，不是 task 状态。"""

    COMPLETED = "completed"                        # step 链跑到头，带 observer 判决
    AWAITING_HUMAN = "awaiting_human"              # HITL 冷 park
    SUSPENDED_ON_CHILDREN = "suspended_on_children"  # 等子任务
    INTERRUPTED = "interrupted"                    # LLM outage / run 崩溃
    CANCELED = "canceled"                          # 被取消


@dataclass(frozen=True)
class RunOutcome:
    """loop 交给 TaskManager 的全部信息：**发生了什么**，不含「task 该变成什么」。"""

    kind: RunOutcomeKind
    verdict: str = ""            # COMPLETED 时：success / fail / retry
    summary: str = ""
    outputs: object = None
    error: str = ""              # 死因 / 受阻原因（自由文本，只作溯源）
    error_code: str = ""
    reason: str = ""             # INTERRUPTED：llm_outage / run_crash；CANCELED：取消原因
    retriable: bool = False      # INTERRUPTED 专用：这次打断允不允许原地重试
    hitl_id: str = ""            # AWAITING_HUMAN
    spawn_titles: tuple[str, ...] = ()   # SUSPENDED_ON_CHILDREN


@dataclass(frozen=True)
class Disposition:
    """处置结果：task 落到哪个状态 + TaskManager 该发哪条事件。"""

    status: str
    event_type: str
    payload: dict


def disposition_for(
    outcome: RunOutcome, *, retry_count: int, max_retries: int,
) -> Disposition:
    """结局 + 预算 → 处置。**不改变任何今天的转移结果**，只是把判断收到一处。

    与今天三处实际口径逐字对照（见 task-1-report.md）：
    - `TaskManager._handle_task_failure`：非 retriable 的运行层异常直接挂起（跳过重试
      判断）；retriable 且 `task.retry_count < task.max_retries` 才原地重试，重试前
      先 `task.retry_count += 1` 再把新值写进 TASK_REQUEUED payload——即
      `retry_count=retry_count+1`。预算耗尽同样落到 `_suspend_task_interrupted`，
      payload 里的 `retry_count` 是未增的原值。
    - `runtime.py` 的 `will_retry`：`run_error is not None and task.retry_count <
      task.max_retries and getattr(run_error, "retriable", True)`——即"retriable
      且预算未尽"这一个合取条件，与上面 `_handle_task_failure` 的判断同构（只是
      在不同调用点各查了一次）。outage 类异常固定 `retriable=False`，故恒不重试。
    - `FinalizeStep`（finalize.py:695 附近）：`retry_exhausted = outcome == "retry"
      and task.retry_count >= task.max_retries`；耗尽时降级为 "fail" 且
      `error_code="TASK_FAILED_RETRY_EXHAUSTED"`，否则 verdict=="fail" 时
      `error_code="TASK_FAILED_BY_OBSERVER"`；两支的 `error_message` 都取
      `task.error`（对应这里的 `outcome.error`）。未耗尽的 retry 分支同样先
      `task.retry_count += 1` 再把新值写进 payload。
    """
    if outcome.kind is RunOutcomeKind.AWAITING_HUMAN:
        return Disposition("AWAITING_HUMAN", "TaskAwaitingHuman",
                           {"hitl_id": outcome.hitl_id})

    if outcome.kind is RunOutcomeKind.SUSPENDED_ON_CHILDREN:
        return Disposition("SUSPENDED", "TaskSuspended", {
            "summary": outcome.summary,
            "spawn_titles": list(outcome.spawn_titles),
        })

    if outcome.kind is RunOutcomeKind.CANCELED:
        return Disposition("CANCELED", "TaskCanceled", {"reason": outcome.reason})

    if outcome.kind is RunOutcomeKind.INTERRUPTED:
        # outage 恒 retriable=False：它等 /resume，从不原地重试（今天的行为）。
        if outcome.retriable and retry_count < max_retries:
            return Disposition("PENDING", "TaskRequeued", {
                "reason": outcome.reason, "retry_count": retry_count + 1,
            })
        return Disposition("INTERRUPTED", "TaskInterrupted", {
            "reason": outcome.reason,
            "error_code": outcome.error_code,
            "error_message": outcome.error,
            "retry_count": retry_count,
        })

    # COMPLETED：observer 的判决 + 重试预算
    if outcome.verdict == "success":
        return Disposition("FINISHED", "TaskFinished", {
            "outcome": "success", "summary": outcome.summary, "outputs": outcome.outputs,
        })
    if outcome.verdict == "retry" and retry_count < max_retries:
        return Disposition("PENDING", "TaskRequeued", {
            "outcome": "retry", "summary": outcome.summary, "retry_count": retry_count + 1,
        })
    # fail，或 retry 但预算耗尽 —— 后者降级成 fail（今天在 FinalizeStep:695）
    exhausted = outcome.verdict == "retry"
    return Disposition("FAILED", "TaskFailed", {
        "error_code": ("TASK_FAILED_RETRY_EXHAUSTED" if exhausted
                       else "TASK_FAILED_BY_OBSERVER"),
        "error_message": outcome.error,
        "retry_count": retry_count,
    })
