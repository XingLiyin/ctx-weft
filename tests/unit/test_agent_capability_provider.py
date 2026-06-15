"""TemplateAgentCapabilityProvider maps template summaries to AgentCapability."""

from __future__ import annotations

from ctx_weft.core.orchestrator.agent_capability import (
    PROVIDER_NAME, TemplateAgentCapabilityProvider,
)
from ctx_weft.protocols.capability import AgentCapability
from ctx_weft.protocols.context import ProviderContext
from ctx_weft.protocols.template import AgentTemplateSummary


class _Resolver:
    async def list_summaries(self, ctx):
        return [AgentTemplateSummary(id="planner", name="Planner", version="1",
                                     description="plans things")]
    async def get(self, *a, **k):  # unused here
        raise NotImplementedError


async def test_lists_templates_as_agent_capabilities() -> None:
    prov = TemplateAgentCapabilityProvider(_Resolver())
    caps = await prov.list(ProviderContext(session_id="s1", tenant_id="default"))
    assert len(caps) == 1
    cap = caps[0]
    assert isinstance(cap, AgentCapability)
    assert cap.id == "agent:planner"
    assert cap.name == "planner"
    assert cap.template_name == "planner"
    assert cap.description == "plans things"
    assert PROVIDER_NAME == "agent"
