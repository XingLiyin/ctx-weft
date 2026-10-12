"""后台 recap 子系统：段边界上那次 fire-and-forget 的观察。

    boundaries.py  两张边界名单（要不要判、是不是 close）
    verdict.py     判定档专属：提交判决 + 判 success 之后的带外收尾
    recap.py       编排本体（两档共用）与它的串行锁
    runner.py      生命周期与并发记账，公开入口 launch_recap

本包**不含** finish 对的两槽交接（在 `loop/finish_pair.py`）与段折算法（在 `loop/fold.py`）
——那两样各有自己的对手方，放这儿会把依赖绕成环。
"""

from ctx_weft.core.loop.background.boundaries import (
    CLOSE_BOUNDARIES,
    JUDGING_BOUNDARIES,
    judges,
)
from ctx_weft.core.loop.background.runner import (
    await_pending_recap,
    await_pending_recap_for_agent,
    launch_recap,
    pending_recap_run_id,
)

__all__ = [
    "CLOSE_BOUNDARIES",
    "JUDGING_BOUNDARIES",
    "await_pending_recap",
    "await_pending_recap_for_agent",
    "judges",
    "launch_recap",
    "pending_recap_run_id",
]
