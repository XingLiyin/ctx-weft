"""compact 触发 / act 停止阈值改用 effective_limit(context_limit, reserved_output_tokens)。

behavior 测试：直接跑 ActStep._account_tokens，验证停止阈值的基准是
effective_limit 而非裸 context_limit——reserve>0 时二者不同，能真正区分新旧口径。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ctx_weft.core.loop.steps.act import _account_tokens
from ctx_weft.core.models.agent import LoopGuard
from ctx_weft.core.estimate import effective_limit
from ctx_weft.protocols import LLMUsage, MemoryAddress

pytestmark = pytest.mark.asyncio


class _FakeMemory:
    async def count_recent(self, scope, types, ctx):
        return 0


def _state(*, context_limit=100_000, reserved_output_tokens=8192):
    guard = LoopGuard(context_limit=context_limit, reserved_output_tokens=reserved_output_tokens)
    agent = SimpleNamespace(id="a1", loop_guard=guard)
    session = SimpleNamespace(token_used=0)
    scope = MemoryAddress(session_id="s1", task_id="t1", agent_id="a1")
    return SimpleNamespace(agent=agent, session=session, scope=scope)


def _ctx():
    return SimpleNamespace(memory=_FakeMemory(), provider_ctx=SimpleNamespace())


async def test_act_stop_threshold_uses_effective_limit_not_context_limit():
    """reserve=8192 时 eff < context_limit：prompt_tokens 落在 [eff*0.8, context_limit*0.8)
    区间必须命中停止（旧口径按 context_limit*0.8 判会漏判，新口径按 eff*0.8 判会命中）。"""
    ctx_limit, reserve = 100_000, 8192
    eff = effective_limit(ctx_limit, reserve)
    assert eff < ctx_limit  # 前提：reserve 确实收窄了预算

    prompt_tokens = int(eff * 0.8)  # 恰好命中新阈值
    assert prompt_tokens < int(ctx_limit * 0.8)  # 但仍低于旧阈值——能区分新旧口径

    state = _state(context_limit=ctx_limit, reserved_output_tokens=reserve)
    hit = await _account_tokens(state, _ctx(), LLMUsage(prompt_tokens=prompt_tokens, completion_tokens=0))
    assert hit is True


async def test_act_stop_threshold_below_effective_limit_does_not_trigger():
    ctx_limit, reserve = 100_000, 8192
    eff = effective_limit(ctx_limit, reserve)
    prompt_tokens = int(eff * 0.8) - 1

    state = _state(context_limit=ctx_limit, reserved_output_tokens=reserve)
    hit = await _account_tokens(state, _ctx(), LLMUsage(prompt_tokens=prompt_tokens, completion_tokens=0))
    assert hit is False
