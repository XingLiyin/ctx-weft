import pytest
from types import SimpleNamespace
import ctx_weft.core.loop.steps.background_observe as bo


@pytest.mark.asyncio
async def test_compact_boundary_skips_when_no_active_raw(fake_state_ctx, monkeypatch):
    state, ctx = fake_state_ctx
    # Augment agent with max_turns_per_observe
    state.agent.loop_config = SimpleNamespace(compact_keep_last=2, max_turns_per_observe=3)
    # 该段已折叠：active LLM_RESPONSE 计数为 0
    async def _count(scope, types, pctx):
        return 0
    monkeypatch.setattr(ctx.memory, "count_recent", _count)
    called = {"react": False}
    async def _react(*a, **k):
        called["react"] = True
        return None, ""
    monkeypatch.setattr(bo, "run_observe_react", _react)

    await bo._run_background_observe(state, ctx, boundary="interrupt")

    assert called["react"] is False  # 护栏跳过，未跑 LLM observe


@pytest.mark.asyncio
async def test_close_boundary_not_guarded(fake_state_ctx, monkeypatch):
    state, ctx = fake_state_ctx
    # Augment agent with max_turns_per_observe
    state.agent.loop_config = SimpleNamespace(compact_keep_last=2, max_turns_per_observe=3)
    async def _count(scope, types, pctx):
        return 0  # 即便为 0，close 边界也不受护栏影响
    monkeypatch.setattr(ctx.memory, "count_recent", _count)
    called = {"react": False}
    async def _react(*a, **k):
        called["react"] = True
        from ctx_weft.core.orchestrator.control_capability import ControlResult
        return ControlResult(content="r", metadata={}), ""
    monkeypatch.setattr(bo, "run_observe_react", _react)

    await bo._run_background_observe(state, ctx, boundary="finish")

    assert called["react"] is True  # close 边界照常跑
