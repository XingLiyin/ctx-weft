"""summarize_for_compact must produce its summary from the LLM (no truncation fallback).

A transient outage that exhausts self-heal must propagate LLMOutageError, not return "".
"""
from types import SimpleNamespace

import pytest

from ctx_weft.protocols import LLMChunk, LLMOutageError
from ctx_weft.core.loop.steps import compact as compact_mod

pytestmark = pytest.mark.asyncio


class _Assembler:
    async def assemble(self, request):
        return SimpleNamespace(system="sys", messages=[])


def _state_ctx():
    agent = SimpleNamespace(runtime={"llm_model": "mock"})
    state = SimpleNamespace(agent=agent, scope=None, task=None, session=None,
                            extra={}, transcript=[])
    ctx = SimpleNamespace(assembler=_Assembler(), llm=None,
                          cancel_token=None, event_bus=None, config=None)
    return state, ctx


async def test_summarize_for_compact_returns_llm_text(monkeypatch):
    async def _ok(ctx, state, request):
        for t in ["sum", "mary"]:
            yield LLMChunk(kind="token", text=t)
    monkeypatch.setattr(compact_mod, "stream_llm_resilient", _ok)
    state, ctx = _state_ctx()
    out = await compact_mod.summarize_for_compact(state, ctx)
    assert out == "summary"


async def test_summarize_for_compact_propagates_outage(monkeypatch):
    async def _raise(ctx, state, request):
        raise LLMOutageError("self-heal exhausted")
        yield  # pragma: no cover  (make this an async generator)
    monkeypatch.setattr(compact_mod, "stream_llm_resilient", _raise)
    state, ctx = _state_ctx()
    with pytest.raises(LLMOutageError):
        await compact_mod.summarize_for_compact(state, ctx)
