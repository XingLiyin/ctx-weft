"""Test spies for the driver's observe scheduling boundary."""

from unittest.mock import patch

from ctx_weft.core.loop.driver import StepDriver


def patch_observe_start(monkeypatch, callback):
    """Intercept observation launches without disabling metadata execution."""
    original = StepDriver.start

    def start(self, name, state, ctx, *, options=None):
        if name == "observe":
            return callback(state, ctx, boundary=(options or {})["boundary"])
        return original(self, name, state, ctx, options=options)

    monkeypatch.setattr(StepDriver, "start", start)


def suppress_observe_start():
    original = StepDriver.start

    def start(self, name, state, ctx, *, options=None):
        if name == "observe":
            return None
        return original(self, name, state, ctx, options=options)

    return patch.object(StepDriver, "start", start)
