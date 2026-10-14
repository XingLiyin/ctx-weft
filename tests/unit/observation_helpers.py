"""Exercise submission and the step-owned foreground assessment together."""
from types import SimpleNamespace

from ctx_weft.core.capabilities.control_tools import ControlResult, report_observation
from ctx_weft.core.loop.steps.observe import ObserveStep


def submit_and_apply(*, ctx, **arguments):
    step = ObserveStep()
    submitted = report_observation(ctx=ctx, **arguments)
    result = step._interpret_submission(SimpleNamespace(task=ctx.task), submitted)
    step._apply_assessment(ctx.task, result.verdict)
    return ControlResult(content=submitted.content, metadata=result.metadata())
