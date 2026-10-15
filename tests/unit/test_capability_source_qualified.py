"""CapabilitySource keeps raw tools; compilation qualifies their LLM names."""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.assembler.sources.capability import CapabilitySource, build_llm_tools
from ctx_weft.core.utils.estimate import estimate_tokens
from ctx_weft.protocols.capability import ToolCapability


async def _collect_tool_block():
    cap = ToolCapability(
        id="mcp:github:create_issue", name="create_issue",
        description="Create an issue.", input_schema={"type": "object"},
        purposes=["act"],
    )
    request = SimpleNamespace(bound_capabilities=[cap], purpose="act", token_counter=estimate_tokens)
    blocks = [b async for b in CapabilitySource().fetch(request, deps=None)]
    return blocks[0]


async def test_llm_tool_name_is_qualified() -> None:
    block = await _collect_tool_block()
    assert build_llm_tools([block.value], "act")[0].name == "mcp__github__create_issue"


async def test_block_capability_name_metadata_is_qualified_for_tools() -> None:
    block = await _collect_tool_block()
    assert block.metadata["capability_name"] == "mcp__github__create_issue"
