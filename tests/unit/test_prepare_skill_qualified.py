"""Compilation resolves a qualified selected skill and loads Level 2 by bare name."""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.assembler import ContextAssembler
from ctx_weft.core.assembler.assembler import AssemblerDeps
from ctx_weft.core.assembler.sources.capability import CapabilitySource
from ctx_weft.core.capabilities.cache import CapabilityCache
from ctx_weft.protocols.capability import (
    CapabilityProviderInfo,
    SkillCapability,
    SkillCapabilityProvider,
    SkillDefinition,
)
from ctx_weft.protocols.context import ProviderContext
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider


class _SkillProvider(SkillCapabilityProvider):
    name = "local_skill"

    def __init__(self):
        self.loaded_with = None

    async def list(self, ctx):
        return [SkillCapability(id="local_skill:pdf", name="pdf", description="d")]

    async def describe(self, ctx):
        return CapabilityProviderInfo(name=self.name)

    async def load_definition(self, skill_name, ctx):
        self.loaded_with = skill_name
        return SkillDefinition(
            skill_id=f"local_skill:{skill_name}",
            skill_name=skill_name,
            instructions="BODY",
        )

    async def list_files(self, skill_name, pattern, limit, ctx):
        return ""

    async def load_resource(self, skill_name, resource_path, ctx):
        return ""

    async def exec_script(self, skill_name, script_path, args, ctx):
        return ""


async def test_loads_by_bare_name_from_qualified_skill_name() -> None:
    capability = SkillCapability(
        id="local_skill:pdf",
        name="pdf",
        description="d",
    )
    cache = CapabilityCache()
    cache.put("agt_1", [capability])
    provider = _SkillProvider()
    request = SimpleNamespace(
        purpose="act",
        bound_capabilities=[capability],
        agent=SimpleNamespace(id="agt_1"),
        task=SimpleNamespace(
            id="tsk_1",
            title="Task",
            description="",
            user_prompt="Use the selected skill",
            user_prompt_in_memory=False,
            settings=SimpleNamespace(skill_name="local_skill__pdf"),
        ),
        scope=SimpleNamespace(agent_id="agt_1", task_id="tsk_1"),
        session=SimpleNamespace(context_limit=20_000, reserved_output_tokens=0),
        template=None,
        intent=None,
        extra={},
        token_counter=len,
    )
    deps = AssemblerDeps(
        memory=InMemoryMemoryProvider(), knowledge_providers=[],
        capability_cache=cache,
        capability_provider_index={"local_skill": provider},
        provider_ctx=ProviderContext(session_id="s1", tenant_id="default"),
    )

    assembler = ContextAssembler([CapabilitySource()], deps)
    inputs = await assembler.assemble(request)
    assert provider.loaded_with is None
    assert inputs.capabilities == [{"provider": "local_skill", "id": "local_skill:pdf"}]
    prompt = await assembler.compile(inputs, template=request.template)
    assert provider.loaded_with == "pdf"
    assert "BODY" in prompt.messages[0].content
    assert "skill_executor__exec_script" in prompt.messages[0].content
