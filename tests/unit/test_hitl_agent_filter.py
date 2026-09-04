"""HITL 读模型的 agent 维度（2026-09-04 spec §5.2）。

HITL 自 09-03 起已经彻底 agent 化（HitlReply.agent_id 必填、cancel_agent 按
agent 过滤未决项），只有查询接口还停在 session 维度。这里补上，并把
delivery 这个原始事实暴露出去——它此前只活在 core 内部，host 想知道
「等的是面板还是一句话」只能靠一个 session 级派生串。
"""

from __future__ import annotations

from datetime import UTC, datetime

from ctx_weft.core.hitl.registry import HitlRegistry
from ctx_weft.protocols.hitl import (
    Delivery,
    HitlAsk,
    NoResumeDelivery,
    ToolResultDelivery,
    UserTurnDelivery,
)


def _open(reg, hitl_id, *, session_id, agent_id, delivery: Delivery):
    """照 HitlRegistry.open 的既有签名登记一条未决项。

    真实签名（core/hitl/registry.py:128）：
        open(self, ask: HitlAsk, *, hitl_id, session_id, task_id,
             agent_id="", tool_call_id="", stage, created_at,
             invocation_key="", tenant_id="default") -> PendingHitl

    `stage` 与 `created_at` 都没有默认值，必须显式传。provider 唯一需要构造
    的类型是 `HitlAsk` 本体——代码库里没有 `AskUser` 这个子类。
    """
    return reg.open(
        HitlAsk(form="wait", delivery=delivery, prompt="?"),
        hitl_id=hitl_id, session_id=session_id, task_id="tsk_1",
        agent_id=agent_id, tenant_id="default",
        stage="test", created_at=datetime.now(UTC),
    )


def test_list_pending_filters_by_agent():
    reg = HitlRegistry()
    _open(reg, "h1", session_id="s1", agent_id="agt_1", delivery=UserTurnDelivery(task_id="tsk_1"))
    _open(reg, "h2", session_id="s1", agent_id="agt_2", delivery=UserTurnDelivery(task_id="tsk_1"))

    ids = [r.id for r in reg.list_pending(agent_id="agt_1")]
    assert ids == ["h1"]


def test_agent_and_session_filters_compose():
    reg = HitlRegistry()
    _open(reg, "h1", session_id="s1", agent_id="agt_1", delivery=UserTurnDelivery(task_id="tsk_1"))
    _open(reg, "h2", session_id="s2", agent_id="agt_1", delivery=UserTurnDelivery(task_id="tsk_1"))

    assert [r.id for r in reg.list_pending("s2", agent_id="agt_1")] == ["h2"]
    assert reg.list_pending("s1", agent_id="agt_2") == []


def test_no_filter_returns_all():
    """回归：既有调用方（不传过滤）行为不变。"""
    reg = HitlRegistry()
    _open(reg, "h1", session_id="s1", agent_id="agt_1", delivery=UserTurnDelivery(task_id="tsk_1"))
    _open(reg, "h2", session_id="s2", agent_id="agt_2", delivery=ToolResultDelivery(tool_call_id="c1"))
    assert len(reg.list_pending()) == 2


def test_view_carries_delivery():
    """delivery 是原始事实，host 据此自己判「面板还是一句话」。"""
    reg = HitlRegistry()
    _open(reg, "h1", session_id="s1", agent_id="agt_1", delivery=UserTurnDelivery(task_id="tsk_1"))
    view = reg.list_pending()[0].to_view()
    assert isinstance(view.delivery, UserTurnDelivery)


def test_view_delivery_defaults_to_no_resume():
    """装填期占位项没有 delivery——默认值必须是安全的那个，不是 None。"""
    from ctx_weft.protocols.hitl import HitlRequestView

    v = HitlRequestView(
        id="h1", form="wait", session_id="s1", task_id="t1",
        created_at=datetime.now(UTC),
    )
    assert isinstance(v.delivery, NoResumeDelivery)
