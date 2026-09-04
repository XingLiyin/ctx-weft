from ctx_weft.core.models.agent import LoopGuard
from ctx_weft.core.models.session import Session
from ctx_weft.core.control.types import SessionView


def test_defaults_are_8192():
    assert LoopGuard().reserved_output_tokens == 8192
    s = Session(id="s", user_prompt="hi", status="RUNNING")
    assert s.reserved_output_tokens == 8192
    assert SessionView(id="s").reserved_output_tokens == 8192


def test_loopguard_carries_reserve():
    g = LoopGuard(context_limit=100_000, reserved_output_tokens=4096)
    assert g.reserved_output_tokens == 4096
