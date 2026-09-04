from __future__ import annotations

import pytest

from ctx_weft.core.errors import AgentNotFound
from ctx_weft.protocols.agent import AgentDetail, AgentSummary
from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_runtime

pytestmark = pytest.mark.asyncio


def _rt():
    return make_runtime(agent_provider=InlineAgentTemplateProvider())


def _plant(rt, agent_id, parent, session_id="s1", status="idle"):
    from ctx_weft.core.orchestrator.agent_registry import _AgentRecord

    reg = rt._agent_registry
    reg._agents[agent_id] = _AgentRecord(
        session_id=session_id, tenant_id="default", template_id="tpl",
        parent_agent_id=parent, spawn_depth=0 if parent is None else 1,
        memory_config=None, loop_config=None, status=status,
    )
    if parent is not None:
        reg._children.setdefault(parent, set()).add(agent_id)


async def test_list_agents_returns_flat_list():
    rt = _rt()
    _plant(rt, "root", None)
    _plant(rt, "kid", "root")

    out = rt.list_agents("s1")
    assert {a.agent_id for a in out} == {"root", "kid"}
    assert all(isinstance(a, AgentSummary) for a in out)
    kid = next(a for a in out if a.agent_id == "kid")
    assert kid.parent_agent_id == "root"


async def test_list_agents_filtered_by_parent():
    rt = _rt()
    _plant(rt, "root", None)
    _plant(rt, "kid1", "root")
    _plant(rt, "grandkid", "kid1")

    out = rt.list_agents("s1", parent_agent_id="root")
    assert {a.agent_id for a in out} == {"kid1"}, "只返回直接子 agent"


async def test_list_agents_excludes_terminated_by_default():
    rt = _rt()
    _plant(rt, "alive", None)
    _plant(rt, "dead", None, status="terminated")

    assert {a.agent_id for a in rt.list_agents("s1")} == {"alive"}
    assert {a.agent_id for a in rt.list_agents("s1", include_terminated=True)} == {"alive", "dead"}


async def test_get_agent_returns_detail():
    rt = _rt()
    _plant(rt, "root", None)
    d = rt.get_agent("root")
    assert isinstance(d, AgentDetail)
    assert d.template_id == "tpl"
    assert d.session_id == "s1"


async def test_get_agent_unknown_raises():
    rt = _rt()
    with pytest.raises(AgentNotFound):
        rt.get_agent("ghost")
