"""`send_message` 建**新 task** 时，必须先等该 agent 上一轮仍在飞的后台折叠。

## 这条保证原来漏在哪

`_run_loop` 入口的「段 recap 强一致」（spec 2026-07-16 §2）从前只有 task 一个轴：

    await await_pending_recap(task.id)

`_task_pending` 按 `task_id` 键，所以它覆盖的是「**同一个 task** 的下一轮 run」——
retry、resume、reconcile 重放。而 agent-centric 那批改造新增的
`send_message` → `_start_task_for_agent` 建的是**新 task**：新 task 的 task 轴是空的，
上一轮那次仍在飞的 close 边界折叠挂在旧 `task_id` 下，**没有任何人等它**。

窗口不是偶发而是必然：close 边界的 `launch_recap` 恒在 `TaskFinished`
**之前**登记（同协程、无 await 间隔，见 `TurnHandle.wait_for_finish` docstring 第 2 节），
所以调用方一见到终态就发下一条消息时，折叠一定还在飞。

后果是静默的：新 task 的首次装配走 `AgentRecallSource` → `recall_recent_by_agent`，
那个查询过滤 `scope is MemoryScope.TASK and not is_superseded`——折叠没落地时，上一轮的
raw 还没被 supersede，于是它们**整段**进了新一轮的 prompt，而不是折出来的那一条胶囊。
不报错、不产生错误内容，只是白胀一轮的量（长会话上会顶到 context 上限）。

原修法是把等待从 task 轴提到 agent 轴：两轴同步登记，`_run_loop` 入口都等。

## 2026-09-22：两道屏障一并拆除，这个窗口被**接受**为代价

拆除的依据是那次的另一半改造——**段界水位线**（`launch_recap` 钉住段界，
`segment_fold` 按它算折叠池）。它治的是真正的正确性问题：迟到的折叠会抢走段界、把
折叠池变空，摘要落到 `now_utc()` 分支排到新消息之后（见 tests/unit/test_segment_fold.py）。

上面描述的那个窗口**不是正确性问题**：不报错、不产生错误内容，只是 prompt 白胀一轮。
用它换掉「人回复要等一次后台 LLM 往返」，已确认接受。下面第 2 条用例因此反了过来——
它现在守的是「不等」，以及守住两轴登记本身（水位线之外，`TurnHandle.wait_for_finish`
与 `_fire_session_done` 仍在用这两个槽）。
"""

from __future__ import annotations

import asyncio

import pytest

from ctx_weft.core.loop.background import runner
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from tests.integration.test_minimal_loop import (
    InlineAgentTemplateProvider, make_echo_template, make_runtime,
)

pytestmark = pytest.mark.asyncio


def _clear_registries() -> None:
    runner._task_pending.clear()
    runner._task_pending_run_id.clear()
    runner._agent_pending.clear()


@pytest.fixture(autouse=True)
def _isolated_pending_registries():
    """两个登记表是模块级全局的，用例之间必须互不残留。"""
    _clear_registries()
    yield
    _clear_registries()


# ── 1. 登记 / 清理的两轴对称 ──────────────────────────────────────────────────


async def test_agent_axis_waits_until_the_pending_fold_completes():
    """`await_..._for_agent` 真的挂住，直到那次折叠跑完。"""
    release = asyncio.Event()
    finished = False

    async def _fold() -> None:
        nonlocal finished
        await release.wait()
        finished = True

    runner._agent_pending["agt_1"] = asyncio.create_task(_fold())

    waiter = asyncio.create_task(runner.await_pending_recap_for_agent("agt_1"))
    await asyncio.sleep(0)          # 让 waiter 真正挂上去
    assert not waiter.done(), "折叠还没跑完，等待方不该返回"
    assert not finished

    release.set()
    await asyncio.wait_for(waiter, timeout=1)
    assert finished, "等待方返回时，折叠必须已经落地"


async def test_agent_axis_is_a_noop_when_nothing_is_in_flight():
    """无 pending 零开销直通——不能为不存在的后台任务空等。"""
    await asyncio.wait_for(
        runner.await_pending_recap_for_agent("agt_never_launched"), timeout=1)

    done = asyncio.create_task(asyncio.sleep(0))
    await done
    runner._agent_pending["agt_2"] = done
    await asyncio.wait_for(
        runner.await_pending_recap_for_agent("agt_2"), timeout=1)


async def test_clear_pending_clears_both_axes_but_only_its_own_registration():
    """done 回调按身份比对后清两个轴；被更晚一次 launch 覆盖过的 key 不能误删。"""
    t_old = asyncio.create_task(asyncio.sleep(0))
    await t_old
    runner._task_pending["tsk_1"] = t_old
    runner._agent_pending["agt_1"] = t_old

    runner._clear_pending(t_old, "tsk_1", "agt_1")
    assert "tsk_1" not in runner._task_pending
    assert "agt_1" not in runner._agent_pending

    # 覆盖场景：新 launch 已经占了同一个 key，旧任务的 done 回调不该动它
    t_new = asyncio.create_task(asyncio.sleep(0))
    await t_new
    runner._task_pending["tsk_2"] = t_new
    runner._agent_pending["agt_2"] = t_new
    runner._clear_pending(t_old, "tsk_2", "agt_2")
    assert runner._task_pending["tsk_2"] is t_new, "旧回调把新登记删掉了"
    assert runner._agent_pending["agt_2"] is t_new


# ── 2. 真正的回归：新 task 的 run 不得越过在途折叠 ────────────────────────────


async def test_new_task_run_does_not_wait_for_previous_tasks_pending_fold():
    """agent 已终态 → `send_message` 建新 task，其 run **不等**上一轮折叠落地。

    这条曾是「agent 轴 recap 强一致」的回归守卫，2026-09-22 随两道屏障一并反转（理由见
    模块 docstring）。它现在守的是那次拆除真的生效了：新一轮不为上一轮的后台 LLM 往返
    买单。代价是这一轮的装配可能读到尚未 supersede 的 raw——prompt 白胀一轮，已接受。
    """
    from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse

    fold_done = False
    release = asyncio.Event()
    llm_saw_fold_done: list[bool] = []

    class _RecordingLLM(MockLLMAdapter):
        # `complete` 是**同步返回 AsyncIterator** 的（不是 async def），照原形状覆盖：
        # 每次被调时记一笔「此刻上一轮的折叠落地了没」，其余原样委托。
        def complete(self, request, stream: bool = True):      # type: ignore[override]
            llm_saw_fold_done.append(fold_done)
            return super().complete(request, stream)

    llm = _RecordingLLM(responses=[MockResponse(text="ok") for _ in range(40)])
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())
    runtime = make_runtime(agent_provider=resolver, llm=llm)
    runtime.providers.register_memory(InMemoryMemoryProvider())

    from ctx_weft.core.runtime import SessionStartParams
    handle = await runtime.start_session(SessionStartParams.create(
        template_id="agent:tpl_echo", user_prompt="第一轮", context_limit=200_000,
    ))
    await handle.wait_for_finish(timeout=30)
    agent_id = handle.agent_id

    # 造出「上一轮的 close 边界折叠还在飞」这个必然窗口：登记一个我们能掐着放行的
    # 在途折叠，key 是 agent（对应真实代码里 launch 时的两轴同步登记）。
    async def _slow_fold() -> None:
        nonlocal fold_done
        await release.wait()
        fold_done = True

    runner._agent_pending[agent_id] = asyncio.create_task(_slow_fold())

    llm_saw_fold_done.clear()
    send = asyncio.create_task(runtime.send_message(agent_id, "第二轮"))

    # 给新 run 充分的调度机会：屏障拆掉之后，它该在 `release` 之前就把 LLM 调起来。
    for _ in range(50):
        await asyncio.sleep(0)
    assert llm_saw_fold_done and not any(llm_saw_fold_done), (
        "新 task 的 run 仍在等上一轮折叠 —— 两道屏障没拆干净，人回复还在为后台往返买单"
    )

    release.set()
    handle2 = await asyncio.wait_for(send, timeout=30)
    await handle2.wait_for_finish(timeout=30)

    assert llm_saw_fold_done, "新一轮始终没跑起来"
    # 这一轮从头到尾都跑在折叠之前——正是「不等」的直接证据。反过来说，若将来有人给
    # 这条路重新加上等待，上面那条断言会先炸，不会静默地把延迟加回来。
    assert not any(llm_saw_fold_done), (
        "新一轮有 LLM 调用发生在折叠落地之后 —— 屏障可能被重新引入，"
        f"逐次观测到的 fold_done={llm_saw_fold_done}"
    )
