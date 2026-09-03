"""冷 resume 重建 agent 时，template_id 取各自的，缺失才回落 session 模板。

此前 `recover_session` 的 pre_resolved 把 **每个** agent 都填成 session root 的
template_id —— 因为 AgentView 里根本没有这个字段。授权收口到 `ctx.agent_template_id`
之后，这意味着重启后子 agent 顶着 root 的模板身份做策略判定。
"""

from __future__ import annotations

from ctx_weft.core.control.converters import agents_from_projection
from ctx_weft.core.control.types import AgentView

ROOT = "agt_root"
SUB = "agt_sub"
LEGACY = "agt_legacy"
ROOT_TMPL = "tpl_root"
SUB_TMPL = "tpl_researcher"


def _views() -> dict[str, AgentView]:
    return {
        ROOT: AgentView(id=ROOT, spawn_depth=0, parent_agent_id=None, template_id=ROOT_TMPL),
        SUB: AgentView(id=SUB, spawn_depth=1, parent_agent_id=ROOT, template_id=SUB_TMPL),
        # 存量事件流：子 agent 没发过 AgentInstantiated → 投影里 template_id 为空
        LEGACY: AgentView(id=LEGACY, spawn_depth=1, parent_agent_id=ROOT, template_id=""),
    }


def test_subagent_keeps_its_own_template() -> None:
    agents = agents_from_projection(
        _views(), session_id="s1", tenant_id="default",
        fallback_template_id=ROOT_TMPL,
    )

    assert agents[SUB].template_id == SUB_TMPL


def test_missing_template_falls_back_to_session_template() -> None:
    """存量数据不得报错，行为与改动前逐字一致（回落 session 模板）。"""
    agents = agents_from_projection(
        _views(), session_id="s1", tenant_id="default",
        fallback_template_id=ROOT_TMPL,
    )

    assert agents[LEGACY].template_id == ROOT_TMPL


def test_tree_fields_and_identity_preserved() -> None:
    agents = agents_from_projection(
        _views(), session_id="s1", tenant_id="t9",
        fallback_template_id=ROOT_TMPL,
    )

    sub = agents[SUB]
    assert (sub.id, sub.session_id, sub.tenant_id) == (SUB, "s1", "t9")
    assert (sub.spawn_depth, sub.parent_agent_id) == (1, ROOT)
    assert set(agents) == {ROOT, SUB, LEGACY}
