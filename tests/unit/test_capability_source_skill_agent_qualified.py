"""CapabilitySource: skill & agent blocks carry the qualified LLM name."""

from __future__ import annotations

from types import SimpleNamespace

from loomex_core.core.assembler.sources.capability import CapabilitySource
from loomex_core.protocols.capability import AgentCapability, SkillCapability


async def _blocks(cap):
    request = SimpleNamespace(bound_capabilities=[cap], purpose="act")
    return [b async for b in CapabilitySource().fetch(request, deps=None)]


async def test_skill_block_name_qualified() -> None:
    cap = SkillCapability(id="local_skill:pdf-tools", name="pdf-tools", description="d")
    block = (await _blocks(cap))[0]
    assert block.metadata["capability_name"] == "local_skill__pdf-tools"
    assert block.metadata["capability_kind"] == "skill"


async def test_agent_block_name_qualified() -> None:
    cap = AgentCapability(id="agent:planner", name="planner", template_name="planner",
                          description="d")
    block = (await _blocks(cap))[0]
    assert block.metadata["capability_name"] == "agent__planner"
    assert block.metadata["capability_kind"] == "agent"
