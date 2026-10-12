"""HumanConfirmationAuthorizer 退化为无状态判断（段 2 · Task 3）。"""

from __future__ import annotations

import dataclasses

from ctx_weft.protocols.capability import HumanGatedAuthorizer, ToolCapability
from ctx_weft.protocols.context import ProviderContext
from ctx_weft.protocols.hitl import HitlDecision, ToolResultDelivery
from ctx_weft.providers.authorizer import HumanConfirmationAuthorizer

CAP = ToolCapability(id="fs:bash_exec", name="bash_exec", description="runs shell",
                     input_schema={})
CTX = ProviderContext(session_id="s1", tenant_id="default", task_id="t1", agent_id="a1")


async def test_constructing_it_takes_no_arguments_at_all():
    """host 再也拿不到 HITL 的把手——解耦成为结构性事实。"""
    a = HumanConfirmationAuthorizer()
    assert not any(f.name == "hitl_manager" for f in dataclasses.fields(a))


async def test_first_call_yields_a_needs_human_ask():
    d = await HumanConfirmationAuthorizer().authorize(CAP, CTX, {"command": "ls"},
                                                      tool_call_id="call_1")
    assert d.allowed is False and d.needs_human is not None
    ask = d.needs_human
    assert ask.form == "approval"
    assert ask.delivery == ToolResultDelivery(tool_call_id="call_1")
    assert ask.subject_id == "fs:bash_exec"
    assert ask.proposal == {"command": "ls"}
    assert ask.reply_as_result is False


async def test_it_implements_the_optional_gated_interface():
    assert isinstance(HumanConfirmationAuthorizer(), HumanGatedAuthorizer)


async def test_accepted_decision_allows_and_passes_note_and_modified_args():
    d = await HumanConfirmationAuthorizer().on_decision(
        CAP, CTX, {"command": "ls"}, "call_1",
        HitlDecision(outcome="accepted", message="careful",
                     modified_arguments={"command": "ls -l"}))
    assert d.allowed is True and d.message == "careful"
    assert d.modified_arguments == {"command": "ls -l"}


async def test_rejected_decision_denies_and_passes_the_guidance():
    d = await HumanConfirmationAuthorizer().on_decision(
        CAP, CTX, {}, "call_1", HitlDecision(outcome="rejected", message="先列目录"))
    assert d.allowed is False and d.message == "先列目录"


async def test_unknown_outcome_does_not_allow():
    """安全默认由**发起方**显式写出，不是 core 偷偷替它决定（spec §9.4）。"""
    d = await HumanConfirmationAuthorizer().on_decision(
        CAP, CTX, {}, "call_1", HitlDecision(outcome="escalated", message="转风控"))
    assert d.allowed is False


async def test_it_never_queries_a_decision_cache():
    """决定是喂进来的：authorizer 无状态、无查询、无 I/O，可纯函数式单测。"""
    import inspect

    import ctx_weft.providers.authorizer.human as mod

    src = inspect.getsource(mod)
    assert "find_resolved_for_tool_call" not in src
    assert "hitl_manager" not in src
