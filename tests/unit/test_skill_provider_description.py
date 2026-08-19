"""skill provider 的组描述：composer 把它写在 `#### local_skill skills` 标题下，
是 actor 挑 skill 派发时唯一能看到的用法说明——须点明 skill 不是可调用工具、
只能经 delegate_task 绑到任务上触发，且 skill 自带文件只能用 skill_executor 工具读取/执行。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ctx_weft.protocols.context import ProviderContext
from ctx_weft.providers.capability_skill_local import LocalSkillCapabilityProvider

pytestmark = pytest.mark.asyncio


def _provider(tmp_path: Path) -> LocalSkillCapabilityProvider:
    return LocalSkillCapabilityProvider(tmp_path / "skills")


def test_description_names_delegate_and_skill_executor_tools(tmp_path: Path) -> None:
    desc = _provider(tmp_path).description
    for frag in (
        "control__delegate_task",
        "skill_name",
        "skill_executor__read_file",
        "skill_executor__list_files",
        "skill_executor__exec_script",
    ):
        assert frag in desc, f"组描述须点名 {frag}；实得 {desc!r}"


def test_description_warns_off_filesystem_and_shell(tmp_path: Path) -> None:
    desc = _provider(tmp_path).description.lower()
    assert "shell" in desc and "filesystem" in desc, \
        "须明确禁止拿 filesystem / shell 工具去操作 skill 文件"


async def test_describe_surfaces_the_description(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    info = await provider.describe(ProviderContext(session_id="s1", tenant_id="default"))
    assert info.description == provider.description
