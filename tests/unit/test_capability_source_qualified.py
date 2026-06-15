"""CapabilitySource: tool blocks carry the qualified LLM name."""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.assembler.sources.capability import CapabilitySource
from ctx_weft.protocols.capability import ToolCapability


async def _collect_tool_block():
    cap = ToolCapability(
        id="mcp:github:create_issue", name="create_issue",
        description="Create an issue.", input_schema={"type": "object"},
        purposes=["act"],
    )
    request = SimpleNamespace(bound_capabilities=[cap], purpose="act")
    blocks = [b async for b in CapabilitySource().fetch(request, deps=None)]
    return blocks[0]


async def test_llm_tool_name_is_qualified() -> None:
    block = await _collect_tool_block()
    assert block.metadata["llm_tool"].name == "mcp__github__create_issue"


async def test_block_capability_name_metadata_is_qualified_for_tools() -> None:
    block = await _collect_tool_block()
    assert block.metadata["capability_name"] == "mcp__github__create_issue"
