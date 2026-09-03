"""AgentLlmChanged 与 AgentInstantiated 的空值口径刻意相反，必须各自钉住（总账 A6）。

reducers.py 里 AGENT_LLM_CHANGED 分支是无条件覆盖（空值也写，纯赋值：「切回账号
默认」是合法选择）；AGENT_INSTANTIATED 分支反过来，空值不覆盖已有选择（存量事件
不带这两个字段，写空会把回放里已经生效的选择抹掉）。这个不对称此前零测试、零
golden 覆盖——本文件把两个方向各钉一条。
"""

from __future__ import annotations

from ctx_weft.core.control.reducers import reduce_events
from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols.events import Event, EventType


def _ev(t, agent_id, payload):
    return Event(
        id=generate_id("evt"), run_id="run_1", sequence=0, session_id="s1",
        type=t, timestamp=now_utc(), agent_id=agent_id, payload=payload,
    )


def _instantiated(*, account: str, model: str):
    return _ev(EventType.AGENT_INSTANTIATED, "agt_1", {
        "template_id": "tpl", "llm_account": account, "llm_model": model,
    })


def _llm_changed(*, account: str, model: str):
    return _ev(EventType.AGENT_LLM_CHANGED, "agt_1", {
        "llm_account": account, "llm_model": model, "reason": "user_selected",
    })


def test_agent_llm_changed_overwrites_with_empty():
    """空 ModelChoice 是「切回账号默认」的合法选择，必须能写空。"""
    view = reduce_events([
        _instantiated(account="acct", model="mdl"),
        _llm_changed(account="", model=""),
    ], "run_1")
    assert view.agents["agt_1"].llm_account == ""
    assert view.agents["agt_1"].llm_model == ""


def test_agent_instantiated_does_not_overwrite_with_empty():
    """存量事件不带这两个字段——空值不许覆盖已有选择。"""
    view = reduce_events([
        _instantiated(account="acct", model="mdl"),
        _instantiated(account="", model=""),      # 存量形态
    ], "run_1")
    assert view.agents["agt_1"].llm_account == "acct"
    assert view.agents["agt_1"].llm_model == "mdl"
