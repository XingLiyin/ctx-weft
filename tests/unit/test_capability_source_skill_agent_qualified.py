"""CapabilitySource: skill & agent blocks carry the qualified LLM name."""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.assembler.sources.capability import CapabilitySource
from ctx_weft.core.utils.estimate import estimate_tokens
from ctx_weft.protocols.capability import AgentCapability, SkillCapability
from tests.assembler_testkit import compile_prompt, prompt_request


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


async def test_source_returns_skill_raw_for_every_purpose() -> None:
    """Source 只收集；purpose 过滤属于最终编译。"""
    cap = SkillCapability(id="local_skill:pdf-tools", name="pdf-tools", description="d")
    assert await _blocks_for(cap, "act")
    for purpose in ("observe", "compact", "recognize_intent"):
        block = (await _blocks_for(cap, purpose))[0]
        assert block.value is cap


async def test_source_returns_agent_raw_for_every_purpose() -> None:
    cap = AgentCapability(id="agent:planner", name="planner", template_name="planner",
                          description="d")
    assert await _blocks_for(cap, "act")
    for purpose in ("observe", "compact", "recognize_intent"):
        block = (await _blocks_for(cap, purpose))[0]
        assert block.value is cap


async def test_jinja_compile_honors_capability_purposes() -> None:
    cap = SkillCapability(id="local_skill:reviewer", name="reviewer", description="d",
                          purposes=["act", "observe"])
    blocks = await _blocks_for(cap, "compact")

    observe = compile_prompt(blocks, prompt_request(purpose="observe"))
    compact = compile_prompt(blocks, prompt_request(purpose="compact"))
    observe_text = observe.system + "\n" + "\n".join(
        str(message.content) for message in observe.messages)
    compact_text = compact.system + "\n" + "\n".join(
        str(message.content) for message in compact.messages)

    assert "local_skill__reviewer" in observe_text
    assert "local_skill__reviewer" not in compact_text
