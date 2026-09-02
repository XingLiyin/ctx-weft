"""HITL 的两个 provider 侧接缝（段 2 · Task 1）。"""

from __future__ import annotations

from ctx_weft.protocols.capability import (
    AuthorizationDecision,
    CapabilityEvent,
    HumanGatedAuthorizer,
    HumanResumable,
)
from ctx_weft.protocols.hitl import HitlAsk, ToolResultDelivery


def _ask() -> HitlAsk:
    return HitlAsk(form="approval", delivery=ToolResultDelivery(tool_call_id="call_1"),
                   prompt="Allow?")


def test_authorization_decision_can_carry_a_needs_human_ask():
    d = AuthorizationDecision(allowed=False, needs_human=_ask())
    assert d.needs_human is not None and d.needs_human.form == "approval"


def test_needs_human_defaults_to_none_so_existing_decisions_are_unchanged():
    d = AuthorizationDecision(allowed=True)
    assert d.needs_human is None


def test_needs_human_implies_not_allowed():
    """安全不变式：带 ask 的决定绝不能同时是放行——gateway 会先看 allowed。"""
    d = AuthorizationDecision(allowed=False, needs_human=_ask())
    assert d.allowed is False


def test_capability_event_accepts_the_needs_human_kind():
    ev = CapabilityEvent(kind="needs_human", payload={"ask": _ask()})
    assert ev.kind == "needs_human" and ev.payload["ask"].prompt == "Allow?"


def test_optional_interfaces_are_structural_not_inherited():
    """加法式：实现者不必继承基类分叉，只需有对应方法。"""

    class Gated:
        async def on_decision(self, cap, ctx, args, tool_call_id, decision):
            return AuthorizationDecision(allowed=True)

    class Resumable:
        async def resume(self, ask_id, decision, resume_state, ctx):
            yield CapabilityEvent(kind="result", payload={"text": "ok"})

    assert isinstance(Gated(), HumanGatedAuthorizer)
    assert isinstance(Resumable(), HumanResumable)


def test_a_plain_authorizer_is_not_human_gated():
    class Plain:
        async def authorize(self, cap, ctx, args=None, *, tool_call_id=""):
            return AuthorizationDecision(allowed=True)

    assert not isinstance(Plain(), HumanGatedAuthorizer)


def test_provider_without_resume_is_not_human_resumable():
    """Negative case: tool provider with no resume method at all."""

    class PlainToolProvider:
        async def invoke(self, capability_id, arguments, ctx):
            yield CapabilityEvent(kind="result", payload={"text": "ok"})

    assert not isinstance(PlainToolProvider(), HumanResumable)


def test_unrelated_class_with_resume_method_matches_protocol_despite_signature_mismatch():
    """Negative case (collision risk): unrelated class with a `resume` method.

    @runtime_checkable checks only method **presence**, not signature or async-ness.
    A media player, download manager, or other unrelated class incidentally having
    a `resume()` method will match `HumanResumable` even though its semantics are
    completely different.

    This test documents the limitation: isinstance(x, HumanResumable) is a necessary
    but insufficient check. The real guard is the gateway raising a clear contract
    violation error (TypeError) when calling the mismatched method, rather than
    silently degrading. Future readers: if this matches unexpectedly, that is
    expected behavior of @runtime_checkable — the protocol cannot distinguish
    unrelated `resume` methods. The fix belongs in the gateway's error handling,
    not in making the Protocol more restrictive.
    """

    class MediaPlayer:
        """An unrelated class with a resume() method (sync, not async)."""

        def resume(self):
            """Play from where it left off."""
            ...

    # @runtime_checkable will accept it because it only checks for method presence.
    assert isinstance(MediaPlayer(), HumanResumable)
