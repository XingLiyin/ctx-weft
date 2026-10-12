"""HitlPark：热→冷降级 / 显式挂起的专用信号（spec/07 §7）。

继承 BaseException（非 Exception）→ 穿过 CapabilityGateway 的 except Exception，不被当成
工具错误结果；一路上抛到 loop，由 _run_loop 显式捕获、落 task SUSPENDED（非 FAILED），
复用委派挂起返回路径。与真正的 interrupt（CancelledError）可区分。
"""

from __future__ import annotations


class HitlPark(BaseException):
    """携带挂起所需的最小信息。"""

    def __init__(self, hitl_id: str = "", tool_call_id: str = "") -> None:
        super().__init__(f"HITL park: hitl={hitl_id} tool_call={tool_call_id}")
        self.hitl_id = hitl_id
        self.tool_call_id = tool_call_id


class RoundDiscarded(BaseException):
    """这一轮在 LLM 开口之前被用户中止 —— 当作没发生过（spec 2026-09-09）。

    与 `HitlPark` 同样继承 `BaseException`（穿过 `except Exception`）、同样一路上抛到
    `_run_loop`，但结局相反：`HitlPark` 是「停在这儿等人」，本信号是「这一轮从头到尾
    抹掉」。

    抛出之后**不得再发任何事件**：抛出点（`act`）已经在未提交窗口里发过一条
    `TASK_CANCELED` 把 agent 送回 `idle`，随后 `_run_task` 会把窗口整个丢弃——丢弃之后
    发的任何事件都会直接落盘，那正是这套设计要避免的残留。所以 `_run_loop` 捕获它时
    既不发 `RUN_CANCELED` 也不发 `RUN_FINISHED`，`_run_task` 也不走 `apply_run_outcome`。
    """

    def __init__(self, task_id: str = "") -> None:
        super().__init__(f"round discarded before first chunk: task={task_id}")
        self.task_id = task_id
