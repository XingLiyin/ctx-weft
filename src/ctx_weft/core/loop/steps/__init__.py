"""V1 内置 Step 集合。

本包只放 Step 与它们各自的私有实现（`act_guidance` 只被 PrepareStep 用，`_capabilities`
只被 prepare/reconcile 用）。2026-09-29 清出去三样不是 Step 的东西：段折算法与它的免折门
去了 `loop/fold.py`，后台 recap 子系统去了 `loop/background/`，finish 对的两槽交接去了
`loop/finish_pair.py`——它们各有自己的对手方，混在 steps/ 里只会把依赖绕成环。
"""

from ctx_weft.core.loop.steps.act import ActStep
from ctx_weft.core.loop.steps.compact import CompactStep
from ctx_weft.core.loop.steps.finalize import FinalizeStep
from ctx_weft.core.loop.steps.recognize_intent import RecognizeIntentStep
from ctx_weft.core.loop.steps.observe import ObserveStep
from ctx_weft.core.loop.steps.prepare import PrepareStep
from ctx_weft.core.loop.steps.reconcile import ReconcileStep
from ctx_weft.core.loop.steps.suspend import SuspendStep

__all__ = [
    "ActStep", "CompactStep", "FinalizeStep", "RecognizeIntentStep",
    "ObserveStep", "PrepareStep", "ReconcileStep", "SuspendStep",
]
