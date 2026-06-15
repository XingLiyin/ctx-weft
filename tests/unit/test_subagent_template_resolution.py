"""LoomeXRuntime._resolve_subagent_template maps qualified agent name → template_name."""

from __future__ import annotations

from loomex_core.core.runtime import LoomeXRuntime, ProviderRegistry
from loomex_core.protocols.context import ProviderContext
from loomex_core.protocols.template import AgentTemplateSummary


class _Resolver:
    async def list_summaries(self, ctx):
        return [AgentTemplateSummary(id="planner", name="Planner", version="1", description="")]
    async def get(self, *a, **k):
        raise NotImplementedError


def _runtime() -> LoomeXRuntime:
    return LoomeXRuntime(template_resolver=_Resolver(), providers=ProviderRegistry())


async def test_qualified_resolves_to_template_name() -> None:
    rt = _runtime()
    ctx = ProviderContext(session_id="s1", tenant_id="default")
    assert await rt._resolve_subagent_template("agent__planner", ctx) == "planner"


async def test_bare_value_passes_through() -> None:
    rt = _runtime()
    ctx = ProviderContext(session_id="s1", tenant_id="default")
    assert await rt._resolve_subagent_template("planner", ctx) == "planner"
