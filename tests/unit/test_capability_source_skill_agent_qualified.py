"""CapabilitySource: skill & agent blocks carry the qualified LLM name."""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.assembler.sources.capability import CapabilitySource
from ctx_weft.core.utils.estimate import estimate_tokens
from ctx_weft.protocols.capability import AgentCapability, SkillCapability


async def _blocks(cap):
    request = SimpleNamespace(bound_capabilities=[cap], purpose="act", token_counter=estimate_tokens)
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


async def _blocks_for(cap, purpose):
    request = SimpleNamespace(bound_capabilities=[cap], purpose=purpose, token_counter=estimate_tokens)
    return [b async for b in CapabilitySource().fetch(request, deps=None)]


async def test_skill_filtered_out_for_non_act_purposes() -> None:
    # skill defaults to purposes=["act"] → delegation only happens in act
    cap = SkillCapability(id="local_skill:pdf-tools", name="pdf-tools", description="d")
    assert await _blocks_for(cap, "act")  # present in act
    for purpose in ("observe", "compact", "recognize_intent"):
        assert await _blocks_for(cap, purpose) == [], f"skill leaked into {purpose}"


async def test_agent_filtered_out_for_non_act_purposes() -> None:
    cap = AgentCapability(id="agent:planner", name="planner", template_name="planner",
                          description="d")
    assert await _blocks_for(cap, "act")  # present in act
    for purpose in ("observe", "compact", "recognize_intent"):
        assert await _blocks_for(cap, purpose) == [], f"agent leaked into {purpose}"


async def test_skill_with_explicit_purpose_honored() -> None:
    # explicit purposes override the act-only default
    cap = SkillCapability(id="local_skill:reviewer", name="reviewer", description="d",
                          purposes=["act", "observe"])
    assert await _blocks_for(cap, "observe")          # shown where declared
    assert await _blocks_for(cap, "compact") == []    # not elsewhere
