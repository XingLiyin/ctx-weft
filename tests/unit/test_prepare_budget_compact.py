from types import SimpleNamespace
import pytest

from ctx_weft.core.loop.steps.prepare import PrepareStep

pytestmark = pytest.mark.asyncio


async def test_should_compact_pure_budget_true():
    state = SimpleNamespace(agent=SimpleNamespace(
        loop_config=SimpleNamespace(compact_token_ratio=0.8),
        loop_guard=SimpleNamespace(context_limit=1000)))
    ctx = SimpleNamespace()
    assert await PrepareStep()._should_compact(state, ctx, token_estimate=850) is True


async def test_should_compact_below_ratio_false_even_with_many_messages():
    # 消息条数门控已废：token 低就不压，无论消息多少
    state = SimpleNamespace(agent=SimpleNamespace(
        loop_config=SimpleNamespace(compact_token_ratio=0.8),
        loop_guard=SimpleNamespace(context_limit=1000)))
    ctx = SimpleNamespace()
    assert await PrepareStep()._should_compact(state, ctx, token_estimate=200) is False
