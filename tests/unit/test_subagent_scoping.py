"""Sub-agent scoping: an agent binds ONLY its declared `subagents`, not every template.

AgentCapabilityProvider 的协议默认 retrieve() 返回 []：全目录只经 list() 暴露给
required-ref 精确查找，永不自动召回——allowlist 语义在协议基类（spec 2026-07-22）。
"""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.capabilities.resolver import CapabilityResolver
from ctx_weft.protocols.capability import (
    AgentCapability, AgentCapabilityProvider, CapabilityProviderInfo,
)
from ctx_weft.protocols.context import ProviderContext
from ctx_weft.protocols.template import CapabilityRef


class _CatalogProvider(AgentCapabilityProvider):
    name = "agent"

    async def list(self, ctx):
        return [
            AgentCapability(id="agent:planner", name="planner", template_name="planner"),
            AgentCapability(id="agent:default", name="default", template_name="default"),
        ]

    async def get_template(self, template_id, version, ctx):
        return None

    async def describe(self, ctx):
        return CapabilityProviderInfo(name=self.name)


def _template(refs):
    return SimpleNamespace(id="default", capability_refs=refs)


async def test_only_declared_subagent_is_bound() -> None:
    provider = _CatalogProvider()
    template = _template([CapabilityRef(capability_id="agent:planner", mode="required")])
    ctx = ProviderContext(session_id="s1", tenant_id="default")
    bound = await CapabilityResolver().resolve(template, task=None, providers=[provider], ctx=ctx)
    assert {c.id for c in bound} == {"agent:planner"}  # NOT agent:default
