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
import os
from dataclasses import dataclass
from pathlib import Path

from ctx_weft.protocols.capability import (
    Capability,
    CapabilityProviderInfo,
    SkillCapability,
    SkillCapabilityProvider,
    SkillDefinition,
)
from ctx_weft.protocols.context import ProviderContext
from ctx_weft.providers._encoding import decode_console
from ctx_weft.providers._script_runner import run_with_liveness

from ._parser import SkillFileMetadata, load_skill_md

logger = logging.getLogger(__name__)

PROVIDER_NAME = "local_skill"


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

class LocalSkillCapabilityProvider(SkillCapabilityProvider):
    """从本地目录实现 Level1（list）/ Level2（load_definition）/ Level3（files/resource/script）。"""

    name = PROVIDER_NAME

    def __init__(
        self,
        skills_dir: Path,
        *,
        script_timeout_sec: int = 60,
        idle_timeout_sec: float = 90,
        hard_cap_sec: float = 600,
        output_limit_chars: int = 65536,
        python_executable: str | None = None,
    ) -> None:
        self._dir = skills_dir
        self._index: dict[str, _SkillEntry] | None = None
        self._script_timeout_sec = script_timeout_sec
        self._idle_timeout_sec = idle_timeout_sec
        self._hard_cap_sec = hard_cap_sec
        self._output_limit_chars = output_limit_chars
        # 运行 .py 脚本用的解释器；None=用 PATH 上的裸 `python`。打包（冻结）形态下
        # PATH 上通常没有 Python，由 host 注入随包内置的解释器路径（同 bash venv 那份）。
        self._python_executable = python_executable

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

    # ── Level 3：script execution ─────────────────────────────────────────────

    async def exec_script(
        self, skill_name: str, script_path: str, args: str, ctx: ProviderContext,
    ) -> str:
        entry = self._require_entry(skill_name)
        skill_root = entry.skill_dir.resolve()
        resolved = (entry.skill_dir / script_path).resolve()
        try:
            resolved.relative_to(skill_root)
        except ValueError:
            raise ValueError(f"script_path '{script_path}' escapes skill directory")
        if not resolved.is_file():
            raise FileNotFoundError(f"script '{script_path}' not found in skill '{skill_name}'")

        # 用引号包裹脚本路径（处理路径中的空格），args 原样追加到命令字符串，
        # 不能放进列表再 join，否则含空格的多参数串会被整体加引号变成单参数。
        # .py 用配置的解释器（None→裸 python）；解释器路径也加引号（内置 runtime
        # 可能落在带空格的目录，如 Program Files）。
        if resolved.suffix == ".py":
            interp = self._python_executable or "python"
            base = f'"{interp}" "{resolved}"'
        else:
            base = f'"{resolved}"'
        cmd = f"{base} {args}" if args else base

        env = {**os.environ, "SKILL_DIR": str(skill_root), "PYTHONIOENCODING": "utf-8"}
        result = await run_with_liveness(
            cmd,
            cwd=str(skill_root),   # 在 skill 目录下运行，脚本内相对路径可直接使用
            env=env,
            idle_timeout_sec=self._idle_timeout_sec,
            hard_cap_sec=self._hard_cap_sec,
            output_limit_bytes=self._output_limit_chars,
        )
        stdout = result.stdout[: self._output_limit_chars]
        stderr = result.stderr[: self._output_limit_chars]

        if result.timed_out:
            survivor_note = (
                "" if result.terminated_clean
                else f" WARNING: {len(result.survivors)} process(es) may still be "
                     "running; outputs may be partial."
            )
            raise RuntimeError(
                f"exec_script timed out ({result.timeout_kind}).{survivor_note}\n"
                f"stdout: {stdout}\nstderr: {stderr}"
            )
        if result.exit_code != 0:
            raise RuntimeError(
                f"script exited with code {result.exit_code}\n"
                f"stdout: {stdout}\nstderr: {stderr}"
            )
        return stdout
