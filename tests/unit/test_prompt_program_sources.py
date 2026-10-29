from pathlib import Path
from types import SimpleNamespace

import pytest

from ctx_weft.core.assembler.assembler import (
    AssemblerDeps,
    ContextAssembler,
    ContextBlock,
    ContextRequest,
)
from ctx_weft.core.assembler.builder import PromptBuilder
from ctx_weft.core.assembler.sources.capability import CapabilitySource
from ctx_weft.core.capabilities.cache import CapabilityCache
from ctx_weft.protocols import ToolCapability
from tests.assembler_testkit import listed_providers, context_from_blocks


def test_prompt_assets_have_only_purpose_and_runtime_surfaces() -> None:
    import ctx_weft.core.assembler.prompts as prompts

    root = Path(prompts.__file__).with_name("prompt_assets")
    assert {path.name for path in root.iterdir()} == {"README.md", "steps", "runtime"}
    assert not hasattr(prompts, "connector")
    assert not hasattr(prompts, "render_template_file")
    assert not hasattr(prompts, "HEADINGS")
    assert not hasattr(prompts, "BLOCK_FALLBACK")


@pytest.mark.asyncio
async def test_jinja_program_places_generic_source_blocks() -> None:
    record = SimpleNamespace(id="memory-1", content="shared result")
    block = ContextBlock(
        id="blackboard-1",
        source="blackboard:topic",
        kind="blackboard",
        metadata={"title": "Child result"},
        value=record,
    )
    request = SimpleNamespace(
        purpose="act",
        intent=None,
        task=SimpleNamespace(
            id="task-1",
            title="Task",
            description="",
            user_prompt="do it",
            user_prompt_in_memory=False,
        ),
        template=None,
        extra={},
        token_counter=len,
        session=SimpleNamespace(context_limit=10_000, reserved_output_tokens=0),
    )

    prompt = PromptBuilder(len).compile(context_from_blocks(request, (block,)))

    assert prompt.messages
    assert "## Shared Context: Child result" in prompt.messages[0].content
    assert "shared result" in prompt.messages[0].content
    assert block.value is record


@pytest.mark.parametrize("purpose", ["act", "observe", "compact", "recognize_intent"])
def test_each_purpose_is_an_executable_top_level_template(purpose: str) -> None:
    task = SimpleNamespace(
        id="task-1",
        title="Task",
        description="",
        user_prompt="do it",
        user_prompt_in_memory=False,
        outputs="",
        unattended=False,
    )
    request = SimpleNamespace(
        purpose=purpose,
        intent=None,
        task=task,
        template=None,
        extra={},
        token_counter=len,
        session=SimpleNamespace(context_limit=20_000, reserved_output_tokens=0),
    )

    prompt = PromptBuilder(len).compile(context_from_blocks(request, ()))

    assert prompt.messages
    assert prompt.metadata["purpose"] == purpose


@pytest.mark.asyncio
async def test_context_assembler_compiles_without_composer_or_budget() -> None:
    record = SimpleNamespace(id="memory-1", content="shared result")

    class Source:
        name = "static"

        async def fetch(self, request, deps):
            yield ContextBlock(
                id="blackboard-1",
                source="static",
                kind="blackboard",
                value=record,
            )

    task = SimpleNamespace(
        id="task-1",
        title="Task",
        description="",
        user_prompt="do it",
        user_prompt_in_memory=False,
    )
    session = SimpleNamespace(
        id="session-1",
        context_limit=20_000,
        reserved_output_tokens=0,
    )
    request = ContextRequest(
        purpose="act",
        scope=SimpleNamespace(session_id="session-1", task_id="task-1", agent_id="agent-1"),
        task=task,
        agent=SimpleNamespace(id="agent-1"),
        session=session,
        template=None,
        bound_capabilities=[],
        token_counter=len,
    )
    assembler = ContextAssembler(
        sources=[Source()],
        deps=AssemblerDeps(
            memory=object(),
            knowledge_providers=[],
            provider_ctx=SimpleNamespace(session_id="session-1", task_id="task-1"),
        ),
    )

    inputs = await assembler.assemble(request)
    prompt = await assembler.compile(inputs, template=request.template, token_counter=request.token_counter)

    assert "shared result" in prompt.messages[0].content


@pytest.mark.asyncio
async def test_tools_and_capability_prose_share_the_collected_snapshot() -> None:
    capability = ToolCapability(
        id="local:search",
        name="search",
        description="Search records",
        input_schema={"type": "object", "properties": {}},
        purposes=["act"],
    )
    task = SimpleNamespace(
        id="task-1",
        title="Task",
        description="",
        user_prompt="search",
        user_prompt_in_memory=False,
    )
    request = ContextRequest(
        purpose="act",
        scope=SimpleNamespace(session_id="session-1", task_id="task-1", agent_id="agent-1"),
        task=task,
        agent=SimpleNamespace(id="agent-1"),
        session=SimpleNamespace(
            id="session-1", context_limit=20_000, reserved_output_tokens=0),
        template=None,
        bound_capabilities=[capability],
        token_counter=len,
    )
    assembler = ContextAssembler(
        sources=[CapabilitySource()],
        deps=AssemblerDeps(
            memory=object(),
            capability_provider_index=listed_providers(request.bound_capabilities),
            knowledge_providers=[],
            provider_ctx=SimpleNamespace(session_id="session-1", task_id="task-1"),
        ),
    )

    inputs = await assembler.assemble(request)
    prompt = await assembler.compile(inputs, template=request.template, token_counter=request.token_counter)

    assert [tool.name for tool in prompt.tools] == ["local__search"]
    assert "local__search" in prompt.messages[0].content
    assert prompt.metadata["tools_reserved_tokens"] > 0


@pytest.mark.asyncio
async def test_tools_follow_capabilities_pinned_after_assembly() -> None:
    initial = ToolCapability(
        id="local:search",
        name="search",
        description="Search records",
        input_schema={"type": "object"},
        purposes=["act"],
    )
    pinned = ToolCapability(
        id="local:inspect",
        name="inspect",
        description="Inspect a record",
        input_schema={"type": "object"},
        purposes=["act"],
    )
    cache = CapabilityCache()
    cache.put("agent-1", [initial])
    request = ContextRequest(
        purpose="act",
        scope=SimpleNamespace(
            session_id="session-1", task_id="task-1", agent_id="agent-1"),
        task=SimpleNamespace(
            id="task-1", title="Task", description="", user_prompt="search",
            user_prompt_in_memory=False,
        ),
        agent=SimpleNamespace(id="agent-1"),
        session=SimpleNamespace(
            id="session-1", context_limit=20_000, reserved_output_tokens=0),
        template=None,
        bound_capabilities=[initial],
        token_counter=len,
    )
    assembler = ContextAssembler(
        sources=[CapabilitySource()],
        deps=AssemblerDeps(
            memory=object(),
            capability_provider_index=listed_providers(request.bound_capabilities),
            knowledge_providers=[],
            provider_ctx=SimpleNamespace(session_id="session-1", task_id="task-1"),
            capability_cache=cache,
        ),
    )

    inputs = await assembler.assemble(request)
    prompt = await assembler.compile(inputs, template=request.template, token_counter=request.token_counter)
    assert [tool.name for tool in prompt.tools] == ["local__search"]

    cache.pin("agent-1", "task-1", [pinned])

    from ctx_weft.core.loop.llm_gateway import select_request_tool_refs

    assert [tool.name for tool in prompt.tools] == ["local__search"]
    assert [ref["id"] for ref in select_request_tool_refs(prompt.inputs, SimpleNamespace(capability_cache=cache), request)] == ["local:search", "local:inspect"]
