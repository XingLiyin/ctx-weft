"""_run_loop 入口的段 recap 强一致等待（spec 2026-07-16 §2）。

驱动 CtxWeftRuntime._run_loop（unbound + fake self）：driver 首步必须在
本 task 在途后台 recap 完成之后才执行；无 pending 时直通。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import ctx_weft.core.loop.steps.background_observe as bo
from ctx_weft.core.loop.driver import LoopState
from ctx_weft.core.runtime import CtxWeftRuntime
from ctx_weft.core.state.models import NormalTaskSettings, Session, Task
from ctx_weft.protocols import MemoryAddress

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clear_pending():
    bo._task_pending.clear()
    yield
    bo._task_pending.clear()


class _FakeBus:
    async def emit(self, event) -> None:
        pass


class _RecordingDriver:
    """driver.run 是 async generator：首次迭代时记录时刻，不产出任何 outcome。"""

    def __init__(self, order: list):
        self._order = order

    async def run(self, state, ctx):
        self._order.append("driver_started")
        if False:  # 使函数成为 async generator，且不产出任何 outcome
            yield


def _make_state_and_task():
    task = Task(
        id="t1", session_id="s1", status="ACTIVE",
        assigned_agent_id="ag1", creator_agent_id="ag1",
        settings=NormalTaskSettings(),
    )
    session = Session(id="s1", tenant_id="default", user_prompt="hi", status="RUNNING")
    agent = SimpleNamespace(id="ag1")
    scope = MemoryAddress(session_id="s1", task_id="t1", agent_id="ag1")
    state = LoopState(run_id="r1", session=session, task=task, agent=agent, scope=scope)
    return state, task, agent


def _fake_runtime_self():
    return SimpleNamespace(
        _event_bus=_FakeBus(),
        _capability_cache=SimpleNamespace(evict=lambda agent_id: None),
    )


async def test_run_start_waits_for_pending_recap():
    """有在途 recap：driver 首步必须排在 recap 完成之后。"""
    order: list = []

    async def slow_recap():
        await asyncio.sleep(0.02)
        order.append("recap_done")

    state, task, agent = _make_state_and_task()
    bo._task_pending[task.id] = asyncio.create_task(slow_recap())

    await CtxWeftRuntime._run_loop(
        _fake_runtime_self(), state, None, _RecordingDriver(order),
        "r1", "prepare", task, agent,
    )

    assert order == ["recap_done", "driver_started"], \
        f"run 必须等 recap 折完才开跑，实得 {order}"


async def test_run_start_passthrough_without_pending():
    """无 pending：直通，driver 正常执行。"""
    order: list = []
    state, task, agent = _make_state_and_task()

    await CtxWeftRuntime._run_loop(
        _fake_runtime_self(), state, None, _RecordingDriver(order),
        "r1", "prepare", task, agent,
    )

    assert order == ["driver_started"]


async def test_run_start_swallows_errored_recap():
    """在途 recap 以异常终结：run 启动 await 防御吞掉，driver 照常执行（段保 raw 降级）。"""
    order: list = []

    async def errored_recap():
        raise RuntimeError("guard region boom")

    state, task, agent = _make_state_and_task()
    bo._task_pending[task.id] = asyncio.create_task(errored_recap())
    # 注意：不 sleep(0) 先驱动 task——一旦它先落异常终态，
    # await_pending_background_observe 的 `not pending.done()` 短路会跳过 shield，
    # 异常永不出这个函数，测试就测不到 _run_loop 这层的防御。保持 task 未跑完时进入
    # _run_loop，让 shield-await 在途中真正接住异常。

    await CtxWeftRuntime._run_loop(
        _fake_runtime_self(), state, None, _RecordingDriver(order),
        "r1", "prepare", task, agent,
    )

    assert order == ["driver_started"], "recap 异常不得阻断 run"
