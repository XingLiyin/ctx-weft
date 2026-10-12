"""agent 的模型选择跨重启存活——这是 D1「切换在重放里不存在」的修复。

D1：SessionResumed 的 payload 带 llm_model，但 reducers.py:405-412 那个
分支不读它；全仓唯一写 SessionView.llm_model 的地方是 SessionCreated。
于是任何一次切换都不进投影，recover_agent 每次把会话拉回创建时的模型。
"""
from __future__ import annotations

from ctx_weft.core.control.reducers import reduce_events
from ctx_weft.core.utils.clock import now_utc
from ctx_weft.core.utils.ids import generate_id
from ctx_weft.protocols.events import Event, EventType


def _ev(t, agent_id, payload):
    return Event(
        id=generate_id("evt"), run_id="run_1", sequence=0, session_id="s1",
        type=t, timestamp=now_utc(), agent_id=agent_id, payload=payload,
    )


def _created():
    return _ev(EventType.SESSION_CREATED, None, {"root_agent_id": "agt_a"})


def test_instantiated_carries_choice_into_view():
    view = reduce_events([
        _ev(EventType.AGENT_INSTANTIATED, "agt_a",
            {"template_id": "tpl", "template_version": "1",
             "llm_account": "acct_a", "llm_model": "mdl_a"}),
    ], "run_1")
    av = view.agents["agt_a"]
    assert (av.llm_account, av.llm_model) == ("acct_a", "mdl_a")


def test_llm_changed_overrides():
    view = reduce_events([
        _ev(EventType.AGENT_INSTANTIATED, "agt_a",
            {"template_id": "tpl", "template_version": "1",
             "llm_account": "acct_a", "llm_model": "mdl_a"}),
        _ev(EventType.AGENT_LLM_CHANGED, "agt_a",
            {"llm_account": "acct_b", "llm_model": "mdl_b",
             "reason": "user_selected"}),
    ], "run_1")
    av = view.agents["agt_a"]
    assert (av.llm_account, av.llm_model) == ("acct_b", "mdl_b")


def test_llm_changed_does_not_touch_task_or_session_state():
    """纯赋值：不入队、不改任何 task 状态、不触发调度。"""
    view = reduce_events([
        _created(),
        _ev(EventType.AGENT_INSTANTIATED, "agt_a", {"template_id": "tpl"}),
        _ev(EventType.AGENT_LLM_CHANGED, "agt_a",
            {"llm_account": "a", "llm_model": "m", "reason": "user_selected"}),
    ], "run_1")
    assert view.session_status == "RUNNING"   # 未被这条事件改动
    assert view.tasks == {}
