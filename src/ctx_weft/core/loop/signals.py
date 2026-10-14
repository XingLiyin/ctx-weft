"""Loop 的跨调用栈控制流信号：挂起当前执行或丢弃未提交的一轮。

继承 BaseException 而非 Exception，避免被工具错误处理捕获。
本模块只定义信号及其载荷；runtime 与 TaskManager 负责执行结局和状态处理。
"""

from __future__ import annotations


class HitlPark(BaseException):
    """携带人工等待请求标识，让当前调用栈退出并交回 runtime。

    runtime 将其转换为 AWAITING_HUMAN 结局，TaskManager 据此处理任务状态。
    热等待转冷、主动让位和软打断均可使用此信号。
    """

    def __init__(self, hitl_id: str = "", tool_call_id: str = "") -> None:
        super().__init__(f"HITL park: hitl={hitl_id} tool_call={tool_call_id}")
        self.hitl_id = hitl_id
        self.tool_call_id = tool_call_id


class RoundDiscarded(BaseException):
    """用户在提交前中止本轮执行，要求将这一轮整体丢弃。

    runtime 原样重抛，TaskManager 丢弃暂存记录和事件窗口，不走 apply_run_outcome。
    调用栈退出期间窗口仍保持打开，runtime 的 RUN_FINISHED 也会被缓冲并一起丢弃。
    必须最后才关闭并丢弃窗口，避免后续收尾事件直接落盘。
    """

    def __init__(self, task_id: str = "") -> None:
        super().__init__(f"round discarded before first chunk: task={task_id}")
        self.task_id = task_id
