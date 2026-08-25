"""SkillExecutor: index keyed by qualified skill name; provider gets the bare name."""

from __future__ import annotations

from collections.abc import AsyncIterator

from ctx_weft.core.orchestrator.skill_executor_capability import (
    SkillExecutorCapabilityProvider,
)
from ctx_weft.protocols.capability import (
    Capability, CapabilityProviderInfo, SkillCapability, SkillCapabilityProvider,
    SkillDefinition,
)
from ctx_weft.protocols.context import ProviderContext


class _FakeSkillProvider(SkillCapabilityProvider):
    name = "local_skill"

    def __init__(self) -> None:
        self.list_files_called_with: str | None = None

    async def list(self, ctx) -> list[Capability]:
        return [SkillCapability(id="local_skill:pdf", name="pdf", description="d")]

    async def describe(self, ctx) -> CapabilityProviderInfo:
        return CapabilityProviderInfo(name=self.name, capability_count=1)

    async def load_definition(self, skill_name, ctx) -> SkillDefinition | None:
        return SkillDefinition(skill_id=f"local_skill:{skill_name}", skill_name=skill_name,
                               instructions="body")

    async def list_files(self, skill_name, pattern, limit, ctx) -> str:
        self.list_files_called_with = skill_name
        return "files"

    async def load_resource(self, skill_name, resource_path, ctx) -> str:
        return "res"

    async def exec_script(self, skill_name, script_path, args, ctx) -> str:
        return "out"


class _Registry:
    def __init__(self, providers):
        self._p = providers

    def get_capability_providers(self):
        return list(self._p)


async def test_dispatch_routes_by_qualified_and_passes_bare_name() -> None:
    skill_provider = _FakeSkillProvider()
    executor = SkillExecutorCapabilityProvider(_Registry([skill_provider]))
    # ctx.skill_name is now the QUALIFIED skill name.
    ctx = ProviderContext(session_id="s1", tenant_id="default", skill_name="local_skill__pdf")
    events = [
        ev async for ev in executor.invoke(
            "skill_executor:list_files", {"pattern": "**/*"}, ctx,
        )
    ]
    assert any(e.kind == "result" for e in events)
    # the provider's Level3 method received the BARE skill name
    assert skill_provider.list_files_called_with == "pdf"


async def test_unknown_qualified_skill_errors() -> None:
    executor = SkillExecutorCapabilityProvider(_Registry([_FakeSkillProvider()]))
    ctx = ProviderContext(session_id="s1", tenant_id="default", skill_name="local_skill__missing")
    events = [
        ev async for ev in executor.invoke("skill_executor:list_files", {}, ctx)
    ]
    assert any(e.kind == "error" and e.payload.get("code") == "SKILL_NOT_FOUND" for e in events)


class _LongFileSkillProvider(_FakeSkillProvider):
    async def load_resource(self, skill_name, resource_path, ctx) -> str:
        return "".join(f"line{i}\n" for i in range(1, 5001))


async def test_read_file_returns_one_window_not_the_whole_file() -> None:
    executor = SkillExecutorCapabilityProvider(_Registry([_LongFileSkillProvider()]))
    ctx = ProviderContext(session_id="s1", tenant_id="default", skill_name="local_skill__pdf")
    events = [
        ev async for ev in executor.invoke(
            "skill_executor:read_file", {"path": "references/long.md"}, ctx,
        )
    ]
    content = next(e.payload["content"] for e in events if e.kind == "result")
    assert "line2000" in content and "line2001" not in content   # 默认 2000 行一屏
    assert "offset=2001" in content                              # 尾部给出续读方式


async def test_read_file_pages_forward_by_offset() -> None:
    executor = SkillExecutorCapabilityProvider(_Registry([_LongFileSkillProvider()]))
    ctx = ProviderContext(session_id="s1", tenant_id="default", skill_name="local_skill__pdf")
    events = [
        ev async for ev in executor.invoke(
            "skill_executor:read_file", {"path": "x", "offset": 2001, "limit": 2}, ctx,
        )
    ]
    content = next(e.payload["content"] for e in events if e.kind == "result")
    assert "line2001" in content and "line2002" in content
    assert "line2000" not in content and "line2003" not in content


async def test_read_file_bad_window_is_tool_error_not_crash() -> None:
    executor = SkillExecutorCapabilityProvider(_Registry([_LongFileSkillProvider()]))
    ctx = ProviderContext(session_id="s1", tenant_id="default", skill_name="local_skill__pdf")
    events = [
        ev async for ev in executor.invoke(
            "skill_executor:read_file", {"path": "x", "offset": 0}, ctx,
        )
    ]
    assert any(e.kind == "error" and e.payload.get("code") == "SKILL_EXEC_ERROR" for e in events)
