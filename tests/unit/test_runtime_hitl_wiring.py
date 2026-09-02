"""Runtime 的 HITL 接线与应答入口（段 2 · Task 8）。"""

from __future__ import annotations

import inspect

import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.protocols.hitl import (
    HitlAsk,
    HitlReply,
    NoResumeDelivery,
    ResumeHint,
    ToolResultDelivery,
    UserTurnDelivery,
)
from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_runtime

pytestmark = pytest.mark.asyncio


def _runtime() -> CtxWeftRuntime:
    return make_runtime(agent_provider=InlineAgentTemplateProvider())


def _runtime_with_recorded_resume() -> tuple[CtxWeftRuntime, list[tuple]]:
    """构造一个 runtime，把 `recover_session` / 用户回合注入替换成记录桩。

    记录的元组形态：
    - ``("recover_session", session_id, resumed_task_id)`` —— 未带 resume_hint 的
      ToolResultDelivery 续跑；
    - ``("recover_session", session_id, resumed_task_id, llm_model)`` —— 带了
      resume_hint.llm_model 覆盖时多一位，值就是覆盖后的 model；
    - ``("inject_user_turn", session_id, task_id)`` —— UserTurnDelivery 续跑。
    """
    rt = _runtime()
    calls: list[tuple] = []

    async def fake_recover_session(session_id, *, resumed_task_id=None, user_reply=None,
                                    llm_account=None, llm_model=None):
        if user_reply is not None:
            calls.append(("inject_user_turn", session_id, resumed_task_id))
        elif llm_model is not None:
            calls.append(("recover_session", session_id, resumed_task_id, llm_model))
        else:
            calls.append(("recover_session", session_id, resumed_task_id))

    rt.recover_session = fake_recover_session  # type: ignore[method-assign]
    return rt, calls


def _ask_tool_result(tool_call_id: str) -> HitlAsk:
    return HitlAsk(form="approval", delivery=ToolResultDelivery(tool_call_id=tool_call_id))


def _ask_user_turn(task_id: str) -> HitlAsk:
    return HitlAsk(form="wait", delivery=UserTurnDelivery(task_id=task_id))


def _ask_no_resume() -> HitlAsk:
    return HitlAsk(form="wait", delivery=NoResumeDelivery())


class _AcceptingSlot:
    """一个总是宣称已消费投递的等待槽——模拟「热等待方还在」。"""

    def deliver(self, decision) -> bool:
        return True


async def test_no_setter_injection_remains():
    """三个 setter 全部消失——构造完即可用，没有半成品窗口。"""
    import ctx_weft.core.runtime as mod

    src = inspect.getsource(mod)
    for name in ("set_cold_resolve_handler", "set_cold_decision_lookup",
                 "set_content_normalizer"):
        assert name not in src


async def test_hitl_service_is_usable_immediately_after_construction():
    rt = _runtime()
    assert rt.hitl is not None and rt.hitl_registry is not None


async def test_reply_returns_the_view_and_drives_resume_by_delivery():
    """冷续跑由**返回值**驱动，不挂总线订阅（spec §7.3 订正）。"""
    rt, calls = _runtime_with_recorded_resume()
    req = await rt.hitl.open(_ask_tool_result("call_1"), session_id="s1", task_id="t1",
                             tool_call_id="call_1", stage="tool")
    view = await rt.reply_to_hitl(HitlReply(hitl_id=req.id, outcome="accepted"))
    assert view is not None and view.outcome == "accepted"
    assert calls == [("recover_session", "s1", "t1")]


async def test_user_turn_delivery_injects_instead_of_reconciling():
    rt, calls = _runtime_with_recorded_resume()
    req = await rt.hitl.open(_ask_user_turn("t1"), session_id="s1", task_id="t1", stage="tool")
    await rt.reply_to_hitl(HitlReply(hitl_id=req.id, outcome="accepted", message="继续"))
    assert calls[0][0] == "inject_user_turn"


async def test_no_resume_delivery_triggers_nothing():
    rt, calls = _runtime_with_recorded_resume()
    req = await rt.hitl.open(_ask_no_resume(), session_id="s1", task_id="t1", stage="tool")
    await rt.reply_to_hitl(HitlReply(hitl_id=req.id, outcome="cancelled"))
    assert calls == []


async def test_a_claimed_hot_reply_does_not_trigger_cold_resume():
    """热投递已就地续跑，再触发一次冷续跑就是双投。"""
    rt, calls = _runtime_with_recorded_resume()
    req = await rt.hitl.open(_ask_tool_result("call_1"), session_id="s1", task_id="t1",
                             tool_call_id="call_1", stage="tool")
    rt.hitl_registry.attach_slot(req.id, _AcceptingSlot())
    await rt.reply_to_hitl(HitlReply(hitl_id=req.id, outcome="accepted"))
    assert calls == []


async def test_replying_twice_resumes_at_most_once():
    """应答入口可能被重试（host 超时重发 / 用户连点）——第二次是 no-op。"""
    rt, calls = _runtime_with_recorded_resume()
    req = await rt.hitl.open(_ask_tool_result("call_1"), session_id="s1", task_id="t1",
                             tool_call_id="call_1", stage="tool")
    await rt.reply_to_hitl(HitlReply(hitl_id=req.id, outcome="accepted"))
    assert await rt.reply_to_hitl(HitlReply(hitl_id=req.id, outcome="accepted")) is None
    assert len(calls) == 1


async def test_resume_hint_overrides_the_model_for_this_resume_only():
    rt, calls = _runtime_with_recorded_resume()
    req = await rt.hitl.open(_ask_tool_result("call_1"), session_id="s1", task_id="t1",
                             tool_call_id="call_1", stage="tool")
    await rt.reply_to_hitl(HitlReply(hitl_id=req.id, outcome="accepted",
                                     resume_hint=ResumeHint(llm_model="big")))
    assert calls[0][-1] == "big"
