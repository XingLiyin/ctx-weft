"""测试侧的「全量装填」等价物——生产里那个 `CtxWeftRuntime.recover()` 已删。

`recover()` 于 2026-09-09 移除，两个理由：

  ① 它的循环体逐字就是 `rebuild_session`（解 tenant → 装填 HITL → 登记成员 → 装填 ALM），
     同一件事写在两处；
  ② 它扫的那个「active session」集合是坏的——判据（`providers/events/_lifecycle`）只会
     add、不会 discard，两条 discard 依据 `SessionFinished` / `SessionStatusChanged` 早已
     随会话状态机退役而停发。于是「active 集」= 这台机器历史上跑过的**全部**会话，启动
     开销与历史会话数线性增长且永不收敛。

生产的恢复因此改成用户驱动：用到哪条会话，就 `rebuild_session` 哪条。

**但装填机制本身一行没变**，而下面这些用例测的正是那套机制。给它们一个测试侧的等价物，
比把每条用例改写成「点名装填哪几个 session」更贴近各自的题意——那样会把「验装填」的用例
悄悄变成「验我记得种了哪几个 session」。

⚠ 新代码不要模仿它。要装填一条会话请直接 `await rt.rebuild_session(sid)`。
"""

from __future__ import annotations

from typing import Any


async def rebuild_all_active(rt: Any) -> int:
    """扫 `list_active_session_ids()` 逐条 `rebuild_session`，返回装填的 agent 总数。"""
    total = 0
    for sid in await rt.event_store.list_active_session_ids():
        total += await rt.rebuild_session(sid)
    return total
