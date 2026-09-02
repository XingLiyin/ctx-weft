"""HitlWaiter：热等待的持有者。**协程栈的事只发生在这一层。**

`core/hitl` 管账、不认识协程；本模块管栈、不管账。旧实现把两者混在一个类里，
于是必须在函数体内延迟 import `core.loop.park` 来躲循环依赖——那个延迟 import
是边界画错的自白（spec §0/§3）。

驱逐**不是失败**：它是热→冷降级。`wait()` 返回 `None`，请求保持未决，答案晚到
照常走冷路径。把「被驱逐」翻译成 `HitlPark` 是 `CapabilityGateway` 的事，不是本
模块的事——本模块连 park 都不认识。
"""

from __future__ import annotations

import asyncio
import logging

from ctx_weft.core.hitl.registry import HitlRegistry
from ctx_weft.protocols.hitl import HitlDecision

logger = logging.getLogger(__name__)


class FutureWaitSlot:
    """`WaitSlot` 的 asyncio 实现：一个 future 的薄包装。

    刻意只有 `deliver` 一个对外操作（`abandon` 供本模块驱逐时用）——registry 因此
    只接触一个并发原语，不接触任何 loop 类型，依赖箭头保持朝下（spec §3.2）。
    """

    def __init__(self) -> None:
        self._future: asyncio.Future[HitlDecision] = (
            asyncio.get_running_loop().create_future()
        )

    def deliver(self, decision: HitlDecision) -> bool:
        """True = 已被热投递消费（`claimed`）；False = 等待方已放弃/已消费。"""
        if self._future.done():
            return False
        self._future.set_result(decision)
        return True

    def abandon(self) -> None:
        """驱逐：此后 `deliver` 一律返回 False，应答改走冷路径。"""
        if not self._future.done():
            self._future.cancel()

    async def result(self) -> HitlDecision:
        return await self._future


class HitlWaiter:
    """把一个 hitl_id 变成一次可等待的会合。"""

    def __init__(
        self, registry: HitlRegistry, timeout_sec: int | None = None
    ) -> None:
        #: None = 永不超时（默认）。这是**纯内存/存活旋钮**——热窗口多久后驱逐，
        #: 与「人类该多久回复」无关。
        self._timeout_sec = timeout_sec
        self._registry = registry

    async def wait(self, hitl_id: str) -> HitlDecision | None:
        """阻塞至应答；**被驱逐返回 `None`，不抛**。未知 id 抛 `KeyError`。

        不抛是刻意的：抛一个 core 内部的 `BaseException` 就是旧实现让 provider 层
        被迫 catch 它的那条路。翻译成 park 的权力留在 gateway。

        **入口即已终局 → 当成驱逐，不把决定递回去（spec 复审修复）**：`gateway` 调用
        顺序是 `ctx.hitl.open(...)` 先于 `ctx.waiter.wait(...)`，而 `open()` 内部
        `await self._emit(...)` 会在 `emit()` 里同步 drain 订阅者——另一个协程可能
        在这个窗口里把请求答了。此时请求从未挂过等待槽，`HitlService._commit` 看到
        `slot is None` ⟹ `claimed=False` ⟹ `reply_to_hitl` 已经（或即将）触发冷续跑。
        若这里把 `req.decision` 原样递回去，还在等的这个协程会热续跑——同一个 task
        被两条路同时驱动，正是本任务要堵的洞。入口已终局的另一种情形是 `cancel()`，
        此时保持「未建槽即返回」同样正确：不该假装收到了一个答案。
        两种情形都按「驱逐」处理——返回 `None`，让 gateway 走已经踩熟的 park 路径；
        真正「答复先到、槽已挂上」的热路径不受影响，走下面的 `slot.result()`。
        """
        req = self._registry.get(hitl_id)
        if req is None:
            raise KeyError(f"No HITL request found: {hitl_id}")
        if req.resolved:
            logger.info(
                "HITL resolved before wait() attached a slot (open()/wait() race "
                "window, or cancel) → treating as evicted so the cold path (which "
                "already owns this resolution) is the only driver (hitl=%s)",
                hitl_id,
            )
            return None
        slot = FutureWaitSlot()
        self._registry.attach_slot(hitl_id, slot)
        try:
            async with asyncio.timeout(self._timeout_sec):
                return await slot.result()
        except TimeoutError:
            # 驱逐与「应答刚好到达」的竞态由 registry 的同步 resolve 裁决：它已原子地
            # 取走槽，则本处 detach 是 no-op、future 已有结果、上面的 await 早已返回。
            current = self._registry.get(hitl_id)
            if current is not None and current.resolved:
                return current.decision  # 应答先到：走热已解决
            self._registry.detach_slot(hitl_id)
            slot.abandon()
            logger.info("HITL hot window evicted → cold (hitl=%s)", hitl_id)
            return None
