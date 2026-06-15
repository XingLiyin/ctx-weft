"""V1 内置 Step 集合。"""

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
