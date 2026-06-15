"""HitlPark：热→冷降级 / 显式挂起的专用信号（spec/07 §7）。

继承 BaseException（非 Exception）→ 穿过 CapabilityGateway 的 except Exception，不被当成
工具错误结果；一路上抛到 loop，由 _run_loop 显式捕获、落 task SUSPENDED（非 FAILED），
复用委派挂起返回路径。与真正的 interrupt（CancelledError）可区分。
"""

from __future__ import annotations


class HitlPark(BaseException):
    """携带挂起所需的最小信息。"""

    def __init__(self, request_id: str = "", tool_call_id: str = "") -> None:
        super().__init__(f"HITL park: request={request_id} tool_call={tool_call_id}")
        self.request_id = request_id
        self.tool_call_id = tool_call_id
