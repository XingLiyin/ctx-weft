"""TemplateLookup：qualified 反查保 provider 归属 + cap.id 前缀精确路由（spec 2026-07-22）。"""

from __future__ import annotations

import pytest

from ctx_weft.core.errors import TemplateNotFoundError
from ctx_weft.core.orchestrator.template_lookup import TemplateLookup
from ctx_weft.core.registry import ProviderRegistry
from ctx_weft.protocols.capability import (
    AgentCapability, AgentCapabilityProvider, CapabilityProviderInfo,
)
from ctx_weft.protocols.context import ProviderContext
from ctx_weft.protocols.template import AgentTemplate, LoopConfig, MemoryConfig

_CTX = ProviderContext(session_id="s1", tenant_id="default")


def _template(tid: str) -> AgentTemplate:
    return AgentTemplate(id=tid, name=tid, version="1", identity={},
                         capability_refs=[], memory_config=MemoryConfig(),
                         loop_config=LoopConfig())


class _Prov(AgentCapabilityProvider):
    """内存 agent provider 桩：name 可含冒号（多段），get_template 记录调用。"""

    def __init__(self, name: str, templates: dict[str, AgentTemplate],
                 list_error: bool = False, get_error: bool = False) -> None:
        self.name = name
        self._templates = templates
        self._list_error = list_error
        self._get_error = get_error
        self.get_calls: list[str] = []

    async def list(self, ctx):
        if self._list_error:
            raise RuntimeError("list boom")
        return [AgentCapability(id=f"{self.name}:{tid}", name=tid, template_name=tid)
                for tid in self._templates]

    async def get_template(self, template_id, version, ctx):
        self.get_calls.append(template_id)
        if self._get_error:
            raise RuntimeError("get boom")
        return self._templates.get(template_id)

    async def describe(self, ctx):
        return CapabilityProviderInfo(name=self.name)


def _lookup(*provs: _Prov) -> TemplateLookup:
    reg = ProviderRegistry()
    for p in provs:
        reg.register_capability(p)
    return TemplateLookup(reg)


# ── resolve_qualified ────────────────────────────────────────────────────────

async def test_resolve_qualified_returns_full_cap_id() -> None:
    lookup = _lookup(_Prov("agent", {"planner": _template("planner")}))
    assert await lookup.resolve_qualified("agent__planner", _CTX) == "agent:planner"


async def test_resolve_qualified_miss_passes_through() -> None:
    lookup = _lookup(_Prov("agent", {"planner": _template("planner")}))
    assert await lookup.resolve_qualified("nope__x", _CTX) == "nope__x"


async def test_resolve_qualified_skips_broken_provider() -> None:
    broken = _Prov("bad", {}, list_error=True)
    good = _Prov("agent", {"planner": _template("planner")})
    lookup = _lookup(broken, good)
    assert await lookup.resolve_qualified("agent__planner", _CTX) == "agent:planner"


# ── get_template ─────────────────────────────────────────────────────────────

async def test_get_template_routes_by_prefix() -> None:
    t = _template("planner")
    lookup = _lookup(_Prov("agent", {"planner": t}))
    assert await lookup.get_template("agent:planner", None, _CTX) is t


async def test_get_template_multisegment_provider_name() -> None:
    t = _template("researcher")
    lookup = _lookup(_Prov("mcp:github", {"researcher": t}))
    assert await lookup.get_template("mcp:github:researcher", None, _CTX) is t


async def test_routed_provider_miss_does_not_fall_through() -> None:
    """路由确定后其余 provider 不参与——即使别家有同名模板。"""
    empty = _Prov("agent", {})
    other = _Prov("other", {"planner": _template("planner")})
    lookup = _lookup(empty, other)
    with pytest.raises(TemplateNotFoundError):
        await lookup.get_template("agent:planner", None, _CTX)
    assert other.get_calls == []


async def test_bare_id_raises_with_canonical_hint() -> None:
    lookup = _lookup(_Prov("agent", {"planner": _template("planner")}))
    with pytest.raises(TemplateNotFoundError, match="provider:name"):
        await lookup.get_template("planner", None, _CTX)


async def test_unknown_prefix_raises() -> None:
    lookup = _lookup(_Prov("agent", {"planner": _template("planner")}))
    with pytest.raises(TemplateNotFoundError):
        await lookup.get_template("nope:planner", None, _CTX)


async def test_no_agent_providers_message() -> None:
    lookup = TemplateLookup(ProviderRegistry())
    with pytest.raises(TemplateNotFoundError, match="no AgentCapabilityProvider"):
        await lookup.get_template("agent:planner", None, _CTX)


async def test_provider_fault_propagates() -> None:
    lookup = _lookup(_Prov("agent", {"planner": _template("planner")}, get_error=True))
    with pytest.raises(RuntimeError, match="get boom"):
        await lookup.get_template("agent:planner", None, _CTX)
