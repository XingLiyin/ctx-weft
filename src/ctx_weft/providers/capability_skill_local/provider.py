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
    qualify,
)
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ctx_weft.protocols.capability import CapabilityEvent

from ctx_weft.protocols.context import ProviderContext
from ctx_weft.providers._encoding import decode_console
from ctx_weft.providers._script_runner import run_with_liveness

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

# composer 把它渲染成 "#### local_skill skills" 标题下的引子段（见 composer._render_grouped_
# _section）。受众是**还没绑 skill、正在挑 skill 派发**的 actor：它在这里第一次看到 skill 清单，
# 需要知道 skill 不是工具、怎么触发、以及 skill 自带文件归 skill_executor 管。已绑定 skill 的
# 任务另有 PrepareStep 的运行时说明（prepare._SKILL_SCRIPT_RUNTIME_NOTE），两处受众不同。
SKILL_PROVIDER_DESCRIPTION = (
    "Skills are instruction bundles, not callable tools — never invoke a skill name as a tool. "
    f"To run one, delegate a task bound to it: {_DELEGATE_TASK_NAME}(..., "
    f"skill_name='{PROVIDER_NAME}__<name>'). The skill's instructions are then loaded into that "
    "task. Inside such a task, reach the skill's own files only through the skill_executor tools "
    f"— {_SKILL_READ_FILE_NAME}, {_SKILL_LIST_FILES_NAME}, {_SKILL_EXEC_SCRIPT_NAME}. Never read, "
    "search, or run skill files with the filesystem or shell tools: skill paths do not resolve "
    "from the session workspace."
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

class LocalSkillCapabilityProvider(SkillCapabilityProvider):
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
        self._script_timeout_sec = script_timeout_sec
        self._idle_timeout_sec = idle_timeout_sec
        self._hard_cap_sec = hard_cap_sec
        self._output_limit_chars = output_limit_chars
        # 运行 .py 脚本用的解释器；None=用 PATH 上的裸 `python`。打包（冻结）形态下
        # PATH 上通常没有 Python，由 host 注入随包内置的解释器路径（同 bash venv 那份）。
        self._python_executable = python_executable
        # 注入后,.py 执行委托给 bash_exec 流水线(workspace venv/安全/超时/隐藏窗口)。
        # None → 回退本地直跑(_exec_direct)。
        self._bash_runner = bash_runner

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

        if self._bash_runner is not None:
            return await self._exec_via_bash(skill_root, resolved, args, ctx)
        return await self._exec_direct(skill_root, resolved, args)

    async def _exec_via_bash(
        self, skill_root: Path, resolved: Path, args: str, ctx: ProviderContext
    ) -> str:
        """委托给 bash_exec 流水线:.py 用裸 python(由 venv 激活解析),脚本走绝对路径。
        注入 SKILL_DIR 与 skill 自己的超时;收集 result/error 事件转成返回值/异常。"""
        base = f'python "{resolved}"' if resolved.suffix == ".py" else f'"{resolved}"'
        cmd = f"{base} {args}" if args else base

        extra = {
            **ctx.extra,
            "extra_env": {**(ctx.extra.get("extra_env") or {}), "SKILL_DIR": str(skill_root)},
            "bash_idle_timeout_sec": self._idle_timeout_sec,
            "bash_hard_cap_sec": self._hard_cap_sec,
            "bash_max_output_bytes": self._output_limit_chars,
        }
        ctx2 = dataclasses.replace(ctx, extra=extra)

        content = ""
        exit_code = 0
        async for ev in self._bash_runner(cmd, ctx2):
            if ev.kind == "error":
                msg = ev.payload.get("message") or ev.payload.get("code") or "skill exec failed"
                raise RuntimeError(msg)
            if ev.kind == "result":
                content = ev.payload.get("content", "")
                exit_code = (ev.payload.get("metadata") or {}).get("exit_code", 0)
        if exit_code != 0:
            raise RuntimeError(f"script exited with code {exit_code}\n{content[: self._output_limit_chars]}")
        return content[: self._output_limit_chars]

    async def _exec_direct(self, skill_root: Path, resolved: Path, args: str) -> str:
        """无 bash_runner 时的回退:本地直跑,cwd=skill_dir,解释器用内置/裸 python。"""
        if resolved.suffix == ".py":
            interp = self._python_executable or "python"
            base = f'"{interp}" "{resolved}"'
        else:
            base = f'"{resolved}"'
        cmd = f"{base} {args}" if args else base

        env = {**os.environ, "SKILL_DIR": str(skill_root), "PYTHONIOENCODING": "utf-8"}
        result = await run_with_liveness(
            cmd,
            cwd=str(skill_root),
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
