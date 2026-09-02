"""HitlWaiter：热等待与超时驱逐（段 2 · Task 2）。

驱逐**不是失败**，是热→冷降级：返回 None，请求保持未决，由 gateway 翻译成 park。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from ctx_weft.core.hitl.registry import HitlRegistry
from ctx_weft.core.loop.hitl_waiter import FutureWaitSlot, HitlWaiter
from ctx_weft.protocols.hitl import HitlAsk, HitlDecision, ToolResultDelivery

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _open(reg: HitlRegistry, hitl_id: str = "hit_1") -> None:
    reg.open(
        HitlAsk(form="approval", delivery=ToolResultDelivery(tool_call_id="call_1")),
        hitl_id=hitl_id,
        session_id="s1",
        task_id="t1",
        tool_call_id="call_1",
        created_at=T0,
    )


async def test_wait_returns_the_decision_when_it_is_delivered():
    reg = HitlRegistry()
    _open(reg)
    waiter = HitlWaiter(reg)
    task = asyncio.create_task(waiter.wait("hit_1"))
    await asyncio.sleep(0)  # 让 waiter 挂上槽
    res = reg.resolve("hit_1", HitlDecision(outcome="accepted", message="go"), T0)
    assert res is not None
    _req, slot = res
    assert slot is not None and slot.deliver(
        HitlDecision(outcome="accepted", message="go")
    )
    assert (await task).message == "go"


async def test_wait_returns_none_when_the_slot_is_evicted_by_timeout():
    reg = HitlRegistry()
    _open(reg)
    assert await HitlWaiter(reg, timeout_sec=0).wait("hit_1") is None


async def test_eviction_leaves_the_request_pending():
    """驱逐只释放内存，不改持久状态——请求仍未决，答案晚到照常走冷路径。"""
    reg = HitlRegistry()
    _open(reg)
    await HitlWaiter(reg, timeout_sec=0).wait("hit_1")
    assert reg.get("hit_1").resolved is False
    assert reg.get("hit_1").slot is None  # 槽已撤销


async def test_answer_arriving_first_wins_over_a_later_eviction():
    """竞态单一权威：应答先到即热投递，随后的驱逐是 no-op。"""
    reg = HitlRegistry()
    _open(reg)
    waiter = HitlWaiter(reg, timeout_sec=30)
    task = asyncio.create_task(waiter.wait("hit_1"))
    await asyncio.sleep(0)
    _req, slot = reg.resolve("hit_1", HitlDecision(outcome="accepted"), T0)
    assert slot.deliver(HitlDecision(outcome="accepted")) is True
    assert (await task) is not None


async def test_deliver_on_an_evicted_slot_returns_false():
    """槽已被撤销 ⟹ 投递不被接受 ⟹ 调用方据此走冷续跑，不当成已消费。"""
    slot = FutureWaitSlot()
    slot.abandon()
    assert slot.deliver(HitlDecision(outcome="accepted")) is False


async def test_deliver_twice_returns_false_the_second_time():
    slot = FutureWaitSlot()
    assert slot.deliver(HitlDecision(outcome="accepted")) is True
    assert slot.deliver(HitlDecision(outcome="rejected")) is False


async def test_wait_on_unknown_id_raises_keyerror():
    with pytest.raises(KeyError):
        await HitlWaiter(HitlRegistry()).wait("nope")


async def test_none_timeout_waits_indefinitely():
    """默认永不超时——超时是内存旋钮，不是 UX 语义。"""
    reg = HitlRegistry()
    _open(reg)
    waiter = HitlWaiter(reg, timeout_sec=None)
    task = asyncio.create_task(waiter.wait("hit_1"))
    await asyncio.sleep(0.05)
    assert not task.done()
    _req, slot = reg.resolve("hit_1", HitlDecision(outcome="accepted"), T0)
    slot.deliver(HitlDecision(outcome="accepted"))
    assert (await task) is not None


async def test_external_cancellation_propagates_and_is_not_mistaken_for_eviction():
    """外部取消（cancel_token）必须传播，不能被误认为是驱逐。"""
    reg = HitlRegistry()
    _open(reg)
    waiter = HitlWaiter(reg, timeout_sec=30)
    task = asyncio.create_task(waiter.wait("hit_1"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
