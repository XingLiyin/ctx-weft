"""V1 内置 Step 集合。

本包只放 Step 与它们各自的私有实现（`_capabilities` 被 prepare/reconcile 使用）。
Guidance 的收集与渲染归 assembler；段折算法与 finish 记录交接
位于 `loop/task_memory_fold.py`，短段免折判断由 ObserveStep 负责。
前后台执行统一由 `loop/driver.py` 调度；观察、判决与摘要收尾都由 ObserveStep 负责。
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
