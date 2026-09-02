"""Runtime 的 HITL 接线与应答入口（段 2 · Task 8）。"""

from __future__ import annotations

import inspect

import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.errors import InvalidContentError
from ctx_weft.protocols import ImagePart
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
    view = await rt.reply_to_hitl(HitlReply(hitl_id=req.id, outcome="accepted"))
    # 不能只看 calls == []——resolve() 失败（未知 id / 已终局）时同样不产生任何 call，
    # 两种情况必须区分开：这里断言的是「已终局且真被消费」，不是「resolve 失败了」。
    assert view is not None and view.outcome == "accepted"
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


async def test_hitl_reply_intake_is_wired_to_the_runtime_shared_normalizer():
    """`ReplyIntake` 必须挂着 `_normalize_hitl_content`——不是某个桩、不是恒等变换。

    这是与旧 `set_content_normalizer` 那道接线等价的钉子：以前 `test_hitl_multimodal_
    validation.py` 直接断言 legacy manager 的 normalizer 就是这个方法；新路径下没有
    setter 可断言了，改断言 `ReplyIntake` 构造时收到的就是它，防止有人以后悄悄拿掉。
    """
    rt = _runtime()
    assert rt.hitl._intake._normalizer == rt._normalize_hitl_content


async def test_reply_with_a_disallowed_image_media_type_is_rejected_and_stays_pending():
    """校验能力在唯一的生产路径（`reply_to_hitl`）上是活的，不是被静默拆掉的接线。

    白名单外的 media type 必须在 `validate_content` 就地拒绝——不落库、不发事实、
    请求原样保持未决，供人类重新提交一个合法答复。
    """
    rt = _runtime()
    req = await rt.hitl.open(_ask_tool_result("call_1"), session_id="s1", task_id="t1",
                             tool_call_id="call_1", stage="tool")
    events: list = []

    async def _record(ev):
        events.append(ev)

    rt._event_bus.subscribe(None, _record)  # type: ignore[attr-defined]

    with pytest.raises(InvalidContentError):
        await rt.reply_to_hitl(HitlReply(
            hitl_id=req.id, outcome="accepted",
            message=[ImagePart(data="AAAA", media_type="image/bmp")],
        ))

    pending = rt.hitl_registry.get(req.id)
    assert pending is not None and pending.resolved is False
    assert events == []


async def test_a_failed_cold_resume_after_commit_is_logged_loudly_and_still_raises(
    caplog,
):
    """resolve() 已经不可逆地提交——续跑失败没有第二次机会,必须在日志里带上 hitl_id
    响亮地留痕,而不是只悄悄传给 host（复审 cheap fix）。"""
    rt = _runtime()
    req = await rt.hitl.open(_ask_tool_result("call_1"), session_id="s1", task_id="t1",
                             tool_call_id="call_1", stage="tool")

    async def _boom(*a, **kw):
        raise RuntimeError("owner TM rebuild exploded")

    rt.recover_session = _boom  # type: ignore[method-assign]

    with caplog.at_level("ERROR"):
        with pytest.raises(RuntimeError, match="owner TM rebuild exploded"):
            await rt.reply_to_hitl(HitlReply(hitl_id=req.id, outcome="accepted"))

    assert req.id in caplog.text
    # 应答本身已经不可逆地提交——即便续跑失败，请求也真的终局了。
    assert rt.hitl_registry.get(req.id).resolved is True
