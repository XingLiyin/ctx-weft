"""TemplateAgentCapabilityProvider maps template summaries to AgentCapability."""

from __future__ import annotations

from ctx_weft.core.orchestrator.agent_capability import (
    PROVIDER_NAME, TemplateAgentCapabilityProvider,
)
from ctx_weft.protocols.capability import AgentCapability
from ctx_weft.protocols.context import ProviderContext
from ctx_weft.protocols.template import (
    AgentTemplate, AgentTemplateSummary, LoopConfig, MemoryConfig,
)

_TEMPLATE = AgentTemplate(
    id="planner", name="Planner", version="1",
    identity={}, capability_refs=[],
    memory_config=MemoryConfig(), loop_config=LoopConfig(),
)


class _Resolver:
    def __init__(self) -> None:
        self.get_calls: list[tuple[str, str | None]] = []

    async def list_summaries(self, ctx):
        return [AgentTemplateSummary(id="planner", name="Planner", version="1",
                                     description="plans things")]

    async def get(self, template_id, version, ctx):
        self.get_calls.append((template_id, version))
        if template_id == "planner":
            return _TEMPLATE
        raise KeyError(template_id)


_CTX = ProviderContext(session_id="s1", tenant_id="default")


async def test_lists_templates_as_agent_capabilities() -> None:
    prov = TemplateAgentCapabilityProvider(_Resolver())
    caps = await prov.list(_CTX)
    assert len(caps) == 1
    cap = caps[0]
    assert isinstance(cap, AgentCapability)
    assert cap.id == "agent:planner"
    assert cap.name == "planner"
    assert cap.template_name == "planner"
    assert cap.description == "plans things"
    assert cap.version == "1"          # 新增：listing 携带信息性 version
    assert PROVIDER_NAME == "agent"


async def test_get_template_delegates_to_resolver() -> None:
    resolver = _Resolver()
    prov = TemplateAgentCapabilityProvider(resolver)
    t = await prov.get_template("planner", None, _CTX)
    assert t is _TEMPLATE
    assert resolver.get_calls == [("planner", None)]


async def test_get_template_unknown_id_returns_none() -> None:
    prov = TemplateAgentCapabilityProvider(_Resolver())
    assert await prov.get_template("nope", None, _CTX) is None


async def test_retrieve_defaults_to_empty_allowlist() -> None:
    """allowlist 语义在基类：list 出全目录，retrieve 永不自动召回。"""
    prov = TemplateAgentCapabilityProvider(_Resolver())
    assert await prov.retrieve(_CTX) == []
