"""测试用的最小 HITL 装配：`HitlRegistry` + `HitlService`（+ 可选 `HitlWaiter`）。

`LoopContext` 现在收的是新契约的两个对象（管账的 `hitl`、管栈的 `waiter`），不再是
一个 `HitlManager`。装配是纯机械的五行，抽在这里免得每个 fixture 各抄一遍、又各自
漏掉 `ReplyIntake` 必须显式给 normalizer 这条（旧实现的「没注入就退化成恒等变换」
正是被本次重设计删掉的那类隐式默认）。
"""

from __future__ import annotations

from ctx_weft.core.hitl.registry import HitlRegistry
from ctx_weft.core.hitl.reply_intake import ReplyIntake
from ctx_weft.core.hitl.service import HitlService


class PassthroughNormalizer:
    """显式的恒等 normalizer：`(memory 侧内容, event 侧载荷)` 二元组，两侧同值。"""

    async def __call__(self, content, session_id):
        return content, content


def make_hitl(event_bus, *, max_resolved: int = 1000) -> tuple[HitlService, HitlRegistry]:
    registry = HitlRegistry(max_resolved=max_resolved)
    service = HitlService(
        registry=registry, event_bus=event_bus,
        reply_intake=ReplyIntake(PassthroughNormalizer()),
    )
    return service, registry
