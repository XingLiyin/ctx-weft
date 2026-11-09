"""Stop admission precedes initialization/locking and is checked again after waits."""

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock

import pytest

from ctx_weft.core.models.task import Task
from tests.unit.test_runtime_agent_api import _plant, _rt

OPERATIONS = ["pause_agent", "pause_session", "cancel_session"]


def _owner():
    rt = _rt()
    _plant(rt, "agent", None, status="running")
    tm = rt._task_managers["s1"]
    task = Task(id="task", session_id="s1", status="ACTIVE", assigned_agent_id="agent")
    tm.register_task(task)
    rt._agent_lifecycle_manager._agents["agent"].current_task_id = task.id
    tokens = rt._register_run_tokens("s1", task.id)
    return rt, tm, task, tokens


def _signal(rt, operation):
    return getattr(rt, operation)("agent" if operation == "pause_agent" else "s1")


def _assert_clean(rt, tm, tokens):
    assert not tokens.pause.is_paused and not tokens.cancel.is_cancelled
    assert not tm._cancelled and not rt._pausing and not rt._pause_claimed
    assert rt._agent_lifecycle_manager.status_of("agent") == "running"


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_protected_stop_skips_initialization_and_held_session_lock(monkeypatch, operation):
    rt, tm, task, tokens = _owner()
    initialize = AsyncMock(side_effect=AssertionError("A skipped stop must not initialize"))
    monkeypatch.setattr(rt, "initialize", initialize)
    with tm.finalized_write(task.id):
        async with rt._session_guard("s1"):
            result = await asyncio.wait_for(_signal(rt, operation), 1)
    assert result == ([] if operation == "pause_agent" else False)
    initialize.assert_not_awaited()
    _assert_clean(rt, tm, tokens)


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_write_started_during_initialization_is_checked_before_lock(monkeypatch, operation):
    rt, tm, task, tokens = _owner()
    entered, release = asyncio.Event(), asyncio.Event()

    async def initialize(sid):
        entered.set()
        await release.wait()

    monkeypatch.setattr(rt, "initialize", initialize)
    request = None
    try:
        async with rt._session_guard("s1"):
            request = asyncio.create_task(_signal(rt, operation))
            await asyncio.wait_for(entered.wait(), 1)
            with tm.finalized_write(task.id):
                release.set()
                result = await asyncio.wait_for(asyncio.shield(request), 1)
        assert result == ([] if operation == "pause_agent" else False)
        _assert_clean(rt, tm, tokens)
    finally:
        release.set()
        if request is not None:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_write_started_while_waiting_for_lock_is_checked_after_lock(monkeypatch, operation):
    rt, tm, task, tokens = _owner()
    initialized = asyncio.Event()

    async def initialize(sid):
        initialized.set()

    monkeypatch.setattr(rt, "initialize", initialize)
    request = None
    try:
        with ExitStack() as protection:
            async with rt._session_guard("s1"):
                request = asyncio.create_task(_signal(rt, operation))
                await asyncio.wait_for(initialized.wait(), 1)
                assert not request.done()
                protection.enter_context(tm.finalized_write(task.id))
            result = await asyncio.wait_for(asyncio.shield(request), 1)
        assert result == ([] if operation == "pause_agent" else False)
        _assert_clean(rt, tm, tokens)
    finally:
        if request is not None:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)


async def test_other_agents_finalized_write_does_not_block_targeted_pause(monkeypatch):
    rt, tm, _task, tokens = _owner()
    _plant(rt, "other", None, status="running")
    other = Task(id="other-task", session_id="s1", status="ACTIVE", assigned_agent_id="other")
    tm.register_task(other)
    rt._agent_lifecycle_manager._agents["other"].current_task_id = other.id
    other_tokens = rt._register_run_tokens("s1", other.id)
    monkeypatch.setattr(rt, "initialize", AsyncMock())
    with tm.finalized_write(other.id):
        assert await rt.pause_agent("agent") == ["agent"]
    assert tokens.pause.is_paused and not other_tokens.pause.is_paused
