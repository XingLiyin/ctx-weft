"""core/hitl：HITL 的自足子系统。

**不 import `core.loop`、不 import `core.runtime`**，包括函数体内的延迟 import——
这是本设计的核心不变式：编排层不认识协程栈，park 只属于 loop（spec §3）。
"""

from ctx_weft.core.hitl.registry import HitlRegistry, PendingHitl, WaitSlot
from ctx_weft.core.hitl.reply_intake import ContentNormalizer, ReplyIntake
from ctx_weft.core.hitl.service import HitlService, delivery_to_payload

__all__ = [
    "ContentNormalizer",
    "HitlRegistry",
    "HitlService",
    "PendingHitl",
    "ReplyIntake",
    "WaitSlot",
    "delivery_to_payload",
]
