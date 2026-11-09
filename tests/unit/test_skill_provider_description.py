"""Catalog usage and provider-specific resource guidance reach their audiences."""

from __future__ import annotations

from pathlib import Path

import pytest

from ctx_weft.protocols.context import ProviderContext
from ctx_weft.providers.capability_skill_local import LocalSkillCapabilityProvider


def _provider(tmp_path: Path) -> LocalSkillCapabilityProvider:
    return LocalSkillCapabilityProvider(tmp_path / "skills")


def test_description_names_delegate_and_skill_executor_tools(tmp_path: Path) -> None:
    desc = _provider(tmp_path).description
    for frag in (
        "control__delegate_task",
        "control__delegate_plan",
        "skill_name",
        "skill_executor__read_file",
        "skill_executor__list_files",
        "skill_executor__exec_script",
    ):
        assert frag in desc, f"Catalog description must name {frag}: {desc!r}"


def test_catalog_separates_skill_resources_from_workspace_files(tmp_path: Path) -> None:
    desc = _provider(tmp_path).description.lower()
    assert "filesystem tools for workspace files" in desc
    assert "SKILL_DIR" not in desc


@pytest.mark.asyncio
async def test_describe_surfaces_the_description(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    info = await provider.describe(ProviderContext(session_id="s1", tenant_id="default"))
    assert info.description == provider.description

    skill = tmp_path / "skills" / "example"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: example\ndescription: test\n---\nRun scripts/example.py.\n",
        encoding="utf-8",
    )
    provider.invalidate_cache()
    definition = await provider.load_definition("example", ProviderContext(session_id="s1"))
    assert definition is not None
    assert "Run scripts/example.py." in definition.instructions
    assert "SKILL_DIR" not in definition.instructions
    assert "SKILL_DIR" in provider.execution_instructions
    assert ("%SKILL_DIR%" in provider.execution_instructions) != ("$SKILL_DIR" in provider.execution_instructions)
