"""Sub-agent scoping: an agent binds ONLY its declared `subagents`, not every template.

The TemplateAgentCapabilityProvider must NOT leak the full template catalog through
`retrieve()`; agent capabilities enter only via the template's required refs (which the
host loader builds from the `subagents` frontmatter).
"""

from __future__ import annotations

from types import SimpleNamespace

from loomex_core.core.orchestrator.agent_capability import TemplateAgentCapabilityProvider
from loomex_core.core.orchestrator.capability_resolver import CapabilityResolver
from loomex_core.protocols.context import ProviderContext
from loomex_core.protocols.template import AgentTemplateSummary, CapabilityRef


class _Resolver:
    async def list_summaries(self, ctx):
        return [
            AgentTemplateSummary(id="planner", name="Planner", version="1", description="p"),
            AgentTemplateSummary(id="default", name="Default", version="1", description="d"),
        ]
    async def get(self, *a, **k):
        raise NotImplementedError


def _template(refs):
    return SimpleNamespace(id="default", capability_refs=refs)


async def test_only_declared_subagent_is_bound() -> None:
    provider = TemplateAgentCapabilityProvider(_Resolver())
    # template declares only `planner` as a sub-agent
    template = _template([CapabilityRef(capability_id="agent:planner", mode="required")])
    ctx = ProviderContext(session_id="s1", tenant_id="default")
    bound = await CapabilityResolver().resolve(template, task=None, providers=[provider], ctx=ctx)
    ids = {c.id for c in bound}
    assert ids == {"agent:planner"}  # NOT agent:default, even though it exists in the catalog
