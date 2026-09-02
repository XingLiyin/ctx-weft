"""由未决 HITL 的 **delivery** 推导会话暂停态。**只有这一份判据。**

判据是 `delivery`，**不是 form**：`UserTurnDelivery` = 会话在等用户说话（软待命，没有
面板要答）→ `PAUSED`；其余（`ToolResultDelivery` / `NoResumeDelivery`）= 有一个面板
决定悬着 → `PAUSED_HITL`。旧实现按 `form == "wait"` 字面量判定，host 自定义 form 因此
拿不到正确行为——误标会让前端等一个不存在的面板。

两个消费方共用本模块，不各写一遍（复审 I4）：
  - `CtxWeftRuntime._derive_paused_status` —— 由内存 registry 的 pending 集合推导；
  - `core.control.reducers._apply` 的 `HITL_OPENED` 分支 —— 由单条事件的 delivery 推导。
"""

from __future__ import annotations

from collections.abc import Iterable

from ctx_weft.protocols.hitl import Delivery, UserTurnDelivery

#: 会话「已暂停」的两个取值。`HITL_RESOLVED` 只在会话仍处其中之一时才把它掰回 RUNNING
#: ——避免覆盖一个已经到达的终态。
PAUSED_STATUSES = ("PAUSED", "PAUSED_HITL")


def paused_status_for(deliveries: Iterable[Delivery]) -> str:
    """未决 HITL 的 delivery 集合 → 暂停态；空集合 → `""`（不该暂停）。"""
    items = list(deliveries)
    if not items:
        return ""
    return ("PAUSED" if all(isinstance(d, UserTurnDelivery) for d in items)
            else "PAUSED_HITL")
