"""PrepareStep._load_skill_instructions resolves a qualified skill_name, loads by bare."""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.loop.steps.prepare import PrepareStep
from ctx_weft.core.orchestrator.capability_cache import CapabilityCache
from ctx_weft.protocols.capability import SkillCapability, SkillDefinition
from ctx_weft.protocols.context import ProviderContext


class _SkillProvider:
    name = "local_skill"

    def __init__(self):
        self.loaded_with = None

    async def load_definition(self, skill_name, ctx):
        self.loaded_with = skill_name
        return SkillDefinition(skill_id=f"local_skill:{skill_name}",
                               skill_name=skill_name, instructions="BODY")


async def test_loads_by_bare_name_from_qualified_skill_name() -> None:
    cache = CapabilityCache()
    cache.put("agt_1", [SkillCapability(id="local_skill:pdf", name="pdf", description="d")])
    provider = _SkillProvider()
    state = SimpleNamespace(agent=SimpleNamespace(id="agt_1"))
    ctx = SimpleNamespace(
        capability_cache=cache,
        skill_provider_index={"local_skill": provider},
        provider_ctx=ProviderContext(session_id="s1", tenant_id="default"),
    )
    out = await PrepareStep()._load_skill_instructions(state, ctx, "local_skill__pdf")
    assert "BODY" in out
    assert provider.loaded_with == "pdf"  # bare name handed to the provider
