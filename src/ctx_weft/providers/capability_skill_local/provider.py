"""LocalSkillCapabilityProvider：从本地目录加载技能。

目录结构：
  skills_dir/
    my_skill/
      SKILL.md          ← frontmatter（Level1 元数据）+ 主体（Level2 instructions）
      references/       ← Level3 参考文档
      scripts/          ← Level3 可执行脚本

SkillCapability 仅暴露轻量描述符；skill_dir 等实现细节保存在内部 _SkillEntry。
"""

from __future__ import annotations

import dataclasses
import glob as _glob
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ctx_weft.protocols.capability import (
    Capability,
    CapabilityProviderInfo,
    SkillCapability,
    SkillDefinition,
    qualify,
)

if TYPE_CHECKING:
    from ctx_weft.protocols.capability import CapabilityEvent

from ctx_weft.protocols.context import ProviderContext
from ctx_weft.providers.capability_skill_shell import ShellSkillCapabilityProvider

from ._parser import SkillFileMetadata, load_skill_md

logger = logging.getLogger(__name__)

PROVIDER_NAME = "local_skill"

# skill_executor / control 的 LLM 可见名。此处不从 core.orchestrator import 那两组常量：
# providers 层对 core 只依赖 core.utils 这一个叶子模块，不给它加上行依赖；名字由 qualify
# 现拼，与那边同一口径（provider:tool → provider__tool）。
_DELEGATE_TASK_NAME = qualify("control:delegate_task")
_SKILL_READ_FILE_NAME = qualify("skill_executor:read_file")
_SKILL_LIST_FILES_NAME = qualify("skill_executor:list_files")
_SKILL_EXEC_SCRIPT_NAME = qualify("skill_executor:exec_script")

# The catalog explains skill selection. Resource-access details accompany the
# selected skill's definition, so they remain visible even with a single skill.
SKILL_PROVIDER_DESCRIPTION = (
    "Skills provide task instructions and bundled resources. "
    f"Assign one with {_DELEGATE_TASK_NAME}(..., skill_name='{PROVIDER_NAME}__<name>'). "
    "Its instructions load into the delegated task. "
    f"Within that task, use {_SKILL_LIST_FILES_NAME} and {_SKILL_READ_FILE_NAME} for skill "
    f"resources, and {_SKILL_EXEC_SCRIPT_NAME}(command=...) to run commands. "
    "Use filesystem tools for workspace files."
)


# ── 内部索引条目 ──────────────────────────────────────────────────────────────

@dataclass
class _SkillEntry:
    """provider 内部使用，不暴露到协议层。"""
    meta: SkillFileMetadata
    skill_dir: Path


# ── 扫描 ──────────────────────────────────────────────────────────────────────

def _scan(skills_dir: Path) -> dict[str, _SkillEntry]:
    """扫描 skills_dir/*/SKILL.md，返回 name → _SkillEntry。"""
    index: dict[str, _SkillEntry] = {}
    if not skills_dir.exists():
        return index
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").exists():
            continue
        try:
            meta, _ = load_skill_md(skill_dir)
            name = meta.name or skill_dir.name
            if not meta.name:
                meta = dataclasses.replace(meta, name=name)
            index[name] = _SkillEntry(meta=meta, skill_dir=skill_dir)
        except Exception:
            logger.exception("LocalSkillCapabilityProvider: failed to scan '%s'", skill_dir)
    return index


# ── Provider ──────────────────────────────────────────────────────────────────

class LocalSkillCapabilityProvider(ShellSkillCapabilityProvider):
    """从本地目录实现 Level1（list）/ Level2（load_definition）/ Level3（files/resource/script）。"""

    name = PROVIDER_NAME
    # 整组 skill 的用法说明，随 describe() 与 capability blocks 进 prompt。
    description = SKILL_PROVIDER_DESCRIPTION

    def __init__(
        self,
        skills_dir: Path,
        *,
        script_timeout_sec: int = 60,
        idle_timeout_sec: float = 90,
        hard_cap_sec: float = 600,
        output_limit_chars: int = 65536,
        python_executable: str | None = None,
        bash_runner: "Callable[[str, ProviderContext], AsyncIterator[CapabilityEvent]] | None" = None,
    ) -> None:
        self._dir = skills_dir
        self._index: dict[str, _SkillEntry] | None = None
        super().__init__(
            script_timeout_sec=script_timeout_sec,
            idle_timeout_sec=idle_timeout_sec,
            hard_cap_sec=hard_cap_sec,
            output_limit_chars=output_limit_chars,
            python_executable=python_executable,
            bash_runner=bash_runner,
        )

    def _get_index(self) -> dict[str, _SkillEntry]:
        if self._index is None:
            self._index = _scan(self._dir)
        return self._index

    def invalidate_cache(self) -> None:
        """外部触发时（如远端 sync 后）重置扫描缓存。"""
        self._index = None

    def _require_entry(self, skill_name: str) -> _SkillEntry:
        entry = self._get_index().get(skill_name)
        if entry is None:
            raise KeyError(f"skill '{skill_name}' not found")
        return entry

    # ── Level 1：list ─────────────────────────────────────────────────────────

    async def list(self, ctx: ProviderContext) -> list[Capability]:
        return [
            SkillCapability(
                id=f"{PROVIDER_NAME}:{e.meta.name}",
                name=e.meta.name,
                description=e.meta.description,
                triggers=e.meta.triggers,
                version=e.meta.version,
            )
            for e in self._get_index().values()
        ]

    async def describe(self, ctx: ProviderContext) -> CapabilityProviderInfo:
        return CapabilityProviderInfo(
            name=self.name,
            capability_count=len(self._get_index()),
            supports_streaming=False,
            supports_cancel=False,
            description=self.description,
        )

    # ── Level 2：load_definition ──────────────────────────────────────────────

    async def load_definition(
        self, skill_name: str, ctx: ProviderContext,
    ) -> SkillDefinition | None:
        try:
            entry = self._require_entry(skill_name)
        except KeyError:
            return None
        try:
            _, body = load_skill_md(entry.skill_dir)
            return SkillDefinition(
                skill_id=f"{PROVIDER_NAME}:{skill_name}",
                skill_name=skill_name,
                instructions=body,
            )
        except Exception:
            logger.exception("LocalSkillCapabilityProvider: load_definition failed for '%s'", skill_name)
            return None

    # ── Level 3：file listing ─────────────────────────────────────────────────

    async def list_files(
        self, skill_name: str, pattern: str, limit: int, ctx: ProviderContext,
    ) -> str:
        entry = self._require_entry(skill_name)
        matches = _glob.glob(pattern, root_dir=str(entry.skill_dir), recursive=True)
        files = sorted(
            f for f in matches
            if (entry.skill_dir / f).is_file()
            and not any(part.startswith(".") for part in Path(f).parts)
        )
        truncated = len(files) > limit
        if truncated:
            files = files[:limit]
        result = "\n".join(files) or "(no files)"
        if truncated:
            result += f"\n[truncated at {limit}]"
        return result

    # ── Level 3：resource loading ─────────────────────────────────────────────

    async def load_resource(
        self, skill_name: str, resource_path: str, ctx: ProviderContext,
    ) -> str:
        entry = self._require_entry(skill_name)
        skill_root = entry.skill_dir.resolve()
        full = (entry.skill_dir / resource_path).resolve()
        try:
            full.relative_to(skill_root)
        except ValueError:
            raise ValueError(f"resource_path '{resource_path}' escapes skill directory")
        if not full.is_file():
            raise FileNotFoundError(f"resource '{resource_path}' not found in skill '{skill_name}'")
        return full.read_text(encoding="utf-8")

    # ── Shell execution resource lease ────────────────────────────────────────

    @asynccontextmanager
    async def borrow_skill_root(
        self, skill_name: str, ctx: ProviderContext,
    ) -> AsyncIterator[Path]:
        """本地资源永久保留；执行基类只在本次调用期间借用目录。"""
        yield self._require_entry(skill_name).skill_dir.resolve()
