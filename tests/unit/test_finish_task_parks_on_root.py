"""Plain replies replace completion calls, with or without an observer ROLE."""
import asyncio
import pytest
from ctx_weft.core.loop.signals import HitlPark
from ctx_weft.core.loop.steps.act import ActStep
from ctx_weft.providers.llm.adapters.mock import MockLLMAdapter, MockResponse
from tests.integration.test_interactive_task import _act_state_ctx
from tests.step_helpers import patch_observe_start


@pytest.mark.asyncio
@pytest.mark.parametrize("observe_role", [True, False])
@pytest.mark.parametrize("unattended", [True, False])
async def test_plain_reply_routes_to_observation(monkeypatch, observe_role, unattended):
    boundaries = []
    def launch(state, ctx, *, boundary=""):
        boundaries.append(boundary)
        return asyncio.ensure_future(asyncio.sleep(0))
    patch_observe_start(monkeypatch, launch)
    state, ctx, task, hitl, memory = _act_state_ctx(unattended,
        MockLLMAdapter(responses=[MockResponse(text="Actual answer")]), observe_role=observe_role)
    if unattended:
        outcome = await ActStep().execute(state, ctx)
        assert outcome.next_step == "observe"
        assert outcome.state_patch["act_exit_reason"] == "normal"
    else:
        with pytest.raises(HitlPark):
            await ActStep().execute(state, ctx)
        assert boundaries == ["plain_text"]
        assert hitl.list_pending("s1")
    assert task.outputs == "Actual answer"
