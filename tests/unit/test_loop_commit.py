"""Metadata must start after the real commit, exactly once per run."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from ctx_weft.core.loop.commit import (
    commit_round,
    defer_until_commit,
    discard_uncommitted_round,
    rearm_after_hot_reply,
)
from ctx_weft.core.loop.driver import HOT_REPLY_ROUND_KEY, ROUND_COMMITTED_KEY
from ctx_weft.core.loop.signals import HitlPark, RoundDiscarded


def context():
    state = NS(task=NS(id="task"), run_id="actor", extra={})
    manager = NS(commit_round=AsyncMock(), is_round_open=lambda tid: True,
                 round_hitl_id=lambda tid: "hitl", abandon_round=AsyncMock(), bind_round_run=Mock())
    ctx = NS(task_manager=manager, steps=NS(start=Mock()), hitl=None)
    return state, ctx


async def test_metadata_is_deferred_and_commit_is_idempotent():
    state, ctx = context()
    order = []
    ctx.task_manager.commit_round.side_effect = lambda tid: order.append("commit")
    ctx.steps.start.side_effect = lambda *a: order.append("start")
    defer_until_commit(state, ctx)
    assert not ctx.steps.start.called
    await commit_round(state, ctx)
    await commit_round(state, ctx)
    defer_until_commit(state, ctx)  # Prepare may run again during context recovery.
    assert order == ["commit", "start"]
    ctx.steps.start.assert_called_once_with("recognize_intent", state, ctx)


async def test_prepare_after_compact_commit_can_start_metadata_once():
    state, ctx = context()
    await commit_round(state, ctx)
    assert not ctx.steps.start.called
    defer_until_commit(state, ctx)
    defer_until_commit(state, ctx)
    ctx.steps.start.assert_called_once()


async def test_failed_commit_does_not_start_metadata_or_stamp_state():
    state, ctx = context()
    defer_until_commit(state, ctx)
    ctx.task_manager.commit_round.side_effect = RuntimeError("storage failed")
    with pytest.raises(RuntimeError):
        await commit_round(state, ctx)
    assert not state.extra.get(ROUND_COMMITTED_KEY)
    assert not ctx.steps.start.called
    ctx.task_manager.commit_round.side_effect = None
    await commit_round(state, ctx)
    ctx.steps.start.assert_called_once()


async def test_discard_before_commit_does_not_launch_metadata():
    state, ctx = context()
    defer_until_commit(state, ctx)
    with pytest.raises(RoundDiscarded):
        await discard_uncommitted_round(state, ctx)
    assert not ctx.steps.start.called
    ctx.task_manager.commit_round.assert_not_awaited()


async def test_hot_reply_rearms_only_its_own_window():
    state, ctx = context()
    await commit_round(state, ctx)
    rearm_after_hot_reply(state, ctx, "other")
    assert state.extra[ROUND_COMMITTED_KEY]
    rearm_after_hot_reply(state, ctx, "hitl")
    ctx.task_manager.bind_round_run.assert_called_once_with("task", "actor", starts_run=False)
    assert not state.extra[ROUND_COMMITTED_KEY]
    assert state.extra[HOT_REPLY_ROUND_KEY] == "hitl"
    with pytest.raises(HitlPark):
        await discard_uncommitted_round(state, ctx)
    ctx.task_manager.abandon_round.assert_awaited_once_with("task")
