"""CtxWeftRuntime._resolve_subagent_template maps qualified agent name → template_name."""

from __future__ import annotations

from ctx_weft.core.runtime import CtxWeftRuntime, ProviderRegistry
from ctx_weft.protocols.context import ProviderContext
from ctx_weft.protocols.template import AgentTemplateSummary


class _Resolver:
    async def list_summaries(self, ctx):
        return [AgentTemplateSummary(id="planner", name="Planner", version="1", description="")]
    async def get(self, *a, **k):
        raise NotImplementedError


def _runtime() -> CtxWeftRuntime:
    return CtxWeftRuntime(template_resolver=_Resolver(), providers=ProviderRegistry())


async def test_qualified_resolves_to_template_name() -> None:
    rt = _runtime()
    ctx = ProviderContext(session_id="s1", tenant_id="default")
    assert await rt._resolve_subagent_template("agent__planner", ctx) == "planner"


async def test_bare_value_passes_through() -> None:
    rt = _runtime()
    ctx = ProviderContext(session_id="s1", tenant_id="default")
    assert await rt._resolve_subagent_template("planner", ctx) == "planner"
