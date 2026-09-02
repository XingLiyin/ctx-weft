"""由未决 HITL 的 **delivery** 推导「面板提示」（panel hint）。**只有这一份判据。**

**返回值不是 `SessionStatus`。** `SessionStatus` 自 2026-09-02 的会话状态所有权重构起
已不含 `PAUSED` / `PAUSED_HITL`——两者合并成了单一的 `WAITING`（见
`core.state.models.SessionStatus` 与 `docs/upgrade/2026-09-02-session-status-ownership.md`）。
本模块回答的是另一个问题：**等的是一块要人拍板的面板，还是只是一句话？** 那是
`delivery` 的性质，由 `HitlOpened` 承载，不进会话状态。两个历史字面量在这里被当作
**面板提示**保留，因为它们已经是 host 只读入口的对外契约：

  - `"PAUSED"`      —— 软待命：会话在等用户说话，**没有面板要答**；
  - `"PAUSED_HITL"` —— 有一个面板决定悬着。

判据是 `delivery`，**不是 form**：`UserTurnDelivery` → `"PAUSED"`；其余
（`ToolResultDelivery` / `NoResumeDelivery`）→ `"PAUSED_HITL"`。旧实现按
`form == "wait"` 字面量判定，host 自定义 form 因此拿不到正确行为——误标会让前端等一个
不存在的面板。

唯一消费方：`CtxWeftRuntime._derive_paused_status` → `session_status_after_recover`
（一个先于本次重构、也长于本次重构的 host 只读入口；它问的是 delivery，不是会话状态）。
"""

from __future__ import annotations

from collections.abc import Iterable

from ctx_weft.protocols.hitl import Delivery, UserTurnDelivery


def paused_status_for(deliveries: Iterable[Delivery]) -> str:
    """未决 HITL 的 delivery 集合 → **面板提示**；空集合 → `""`（没人在等）。

    返回的两个值是面板提示，不是 `SessionStatus`——见模块 docstring。
    """
    items = list(deliveries)
    if not items:
        return ""
    return ("PAUSED" if all(isinstance(d, UserTurnDelivery) for d in items)
            else "PAUSED_HITL")
