"""RunPhase: per-run loop-progress flags, defaulted on LoopContext."""

from ctx_weft.core.loop.driver import LoopContext, RunPhase


def test_run_phase_defaults():
    rp = RunPhase()
    assert rp.produced is False
    assert rp.in_tool_loop is False


def test_loop_context_has_run_phase_default():
    ctx = LoopContext(
        assembler=None, llm=None, memory=None, event_bus=None, provider_ctx=None,
    )
    assert isinstance(ctx.run_phase, RunPhase)
    assert ctx.run_phase.produced is False
