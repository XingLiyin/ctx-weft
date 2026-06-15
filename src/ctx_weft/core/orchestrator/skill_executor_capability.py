"""SkillExecutorCapabilityProvider：技能 Level3 执行工具。

与 ControlCapabilityProvider 同级，runtime 初始化时自动注册。
@skill_executor_tool 装饰器参照 @control_tool，从 Annotated 注解提取 schema；
runtime-injected 参数（ctx / skill_provider / skill_name）通过 _SKIP 排除在外。

索引机制：
  ProviderRegistry 注册新 SkillCapabilityProvider 时调用 mark_dirty()；
  首次 invoke 时异步重建 _index: skill_name → SkillCapabilityProvider。
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from ctx_weft.core.utils import extract_schema
from ctx_weft.protocols.capability import (
    Capability,
    CapabilityEvent,
    CapabilityProviderInfo,
    SkillCapability,
    SkillCapabilityProvider,
    ToolCapability,
    ToolCapabilityProvider,
    qualify,
)
from ctx_weft.protocols.context import ProviderContext

if TYPE_CHECKING:
    from ctx_weft.core.runtime import ProviderRegistry

logger = logging.getLogger(__name__)

PROVIDER_NAME = "skill_executor"
_SKIP: frozenset[str] = frozenset({"ctx", "skill_provider", "skill_name"})

# Qualified (LLM-facing) names for the skill_executor tools — use anywhere these tools
# are named to the LLM in prose (e.g. the PrepareStep runtime note).
# The `skill_executor` prefix already conveys "skill", so the bare tool names omit it.
LIST_FILES_NAME = qualify(f"{PROVIDER_NAME}:list_files")
READ_FILE_NAME = qualify(f"{PROVIDER_NAME}:read_file")
EXEC_SCRIPT_NAME = qualify(f"{PROVIDER_NAME}:exec_script")

# ── @skill_executor_tool 装饰器 ───────────────────────────────────────────────

# name → (ToolCapability, async handler fn, valid_keys)
_SKILL_EXECUTOR_TOOLS: dict[str, tuple[ToolCapability, Callable, frozenset[str]]] = {}


def skill_executor_tool(fn: Callable) -> Callable:
    """装饰器：从 Annotated 注解提取 schema，注册到 _SKILL_EXECUTOR_TOOLS。
    runtime-injected 参数（ctx / skill_provider / skill_name）从 schema 排除。
    """
    first_line = (fn.__doc__ or "").strip().split("\n")[0].strip()
    cap = ToolCapability(
        id=f"{PROVIDER_NAME}:{fn.__name__}",
        name=fn.__name__,
        kind="tool",
        purposes=["act"],
        description=first_line,
        input_schema=extract_schema(fn, exclude=_SKIP),
        side_effects=False,
    )
    valid_keys = frozenset(inspect.signature(fn).parameters.keys()) - _SKIP
    _SKILL_EXECUTOR_TOOLS[fn.__name__] = (cap, fn, valid_keys)
    return fn


# ── SkillResult ───────────────────────────────────────────────────────────────

@dataclass
class SkillResult:
    content: str
    is_error: bool = False


# ── 工具定义 ──────────────────────────────────────────────────────────────────

@skill_executor_tool
async def list_files(
    pattern: Annotated[str, "Glob pattern to match files within the skill directory (default '**/*')"] = "**/*",
    limit: Annotated[int, "Maximum number of results to return (default 200)"] = 200,
    *,
    skill_provider: SkillCapabilityProvider,
    skill_name: str,
    ctx: ProviderContext,
) -> SkillResult:
    """List files in the current skill's directory."""
    content = await skill_provider.list_files(skill_name, pattern, limit, ctx)
    return SkillResult(content=content)


@skill_executor_tool
async def read_file(
    path: Annotated[str, "Relative path to a file within the skill directory"],
    *,
    skill_provider: SkillCapabilityProvider,
    skill_name: str,
    ctx: ProviderContext,
) -> SkillResult:
    """Read a file from the current skill's directory."""
    try:
        content = await skill_provider.load_resource(skill_name, path, ctx)
        return SkillResult(content=content)
    except (ValueError, FileNotFoundError) as exc:
        return SkillResult(content=str(exc), is_error=True)


@skill_executor_tool
async def exec_script(
    script_path: Annotated[str, "Relative path to the script within the skill directory"],
    args: Annotated[str, "Command-line argument string appended after the script path"] = "",
    *,
    skill_provider: SkillCapabilityProvider,
    skill_name: str,
    ctx: ProviderContext,
) -> SkillResult:
    """Execute a script from the current skill's directory."""
    try:
        content = await skill_provider.exec_script(skill_name, script_path, args, ctx)
        return SkillResult(content=content)
    except Exception as exc:
        return SkillResult(content=str(exc), is_error=True)


# ── SkillExecutorCapabilityProvider ──────────────────────────────────────────

class SkillExecutorCapabilityProvider(ToolCapabilityProvider):
    """默认内置，runtime 初始化时自动注册，与 ControlCapabilityProvider 同级。

    索引：skill_name → SkillCapabilityProvider
    注册新 SkillCapabilityProvider 时调用 mark_dirty()，
    首次 invoke 时延迟重建索引。
    """

    name = PROVIDER_NAME

    def __init__(self, provider_registry: "ProviderRegistry") -> None:
        self._registry = provider_registry
        # qualified skill name → (provider, bare skill name)
        self._index: dict[str, tuple[SkillCapabilityProvider, str]] = {}
        self._dirty = True

    def mark_dirty(self) -> None:
        """ProviderRegistry 注册新 SkillCapabilityProvider 时调用。"""
        self._dirty = True

    async def _ensure_index(self, ctx: ProviderContext) -> None:
        if not self._dirty:
            return
        self._index.clear()
        for p in self._registry.get_capability_providers():
            if not isinstance(p, SkillCapabilityProvider):
                continue
            try:
                for cap in await p.list(ctx):
                    if isinstance(cap, SkillCapability):
                        key = qualify(cap.id)
                        if key not in self._index:
                            self._index[key] = (p, cap.name)   # 先注册的 provider 优先
            except Exception:
                logger.exception("SkillExecutorCapabilityProvider: index build failed for %s", p.name)
        self._dirty = False
        logger.debug("SkillExecutorCapabilityProvider: index rebuilt (%d skills)", len(self._index))

    # ── ToolCapabilityProvider ────────────────────────────────────────────────

    async def list(self, ctx: ProviderContext) -> list[Capability]:
        return [cap for cap, *_ in _SKILL_EXECUTOR_TOOLS.values()]

    def invoke(
        self,
        capability_id: str,
        arguments: dict[str, Any],
        ctx: ProviderContext,
    ) -> AsyncIterator[CapabilityEvent]:
        return self._dispatch(capability_id, arguments, ctx)

    async def _dispatch(
        self,
        capability_id: str,
        arguments: dict[str, Any],
        ctx: ProviderContext,
    ) -> AsyncIterator[CapabilityEvent]:
        tool_name = capability_id.split(":")[-1]
        entry = _SKILL_EXECUTOR_TOOLS.get(tool_name)
        if entry is None:
            yield CapabilityEvent(kind="error",
                                  payload={"code": "UNKNOWN_TOOL", "message": tool_name})
            return

        skill_name = ctx.skill_name
        if not skill_name:
            yield CapabilityEvent(kind="error",
                                  payload={"code": "NO_SKILL_ASSIGNED",
                                           "message": "task.settings has no skill_name"})
            return

        await self._ensure_index(ctx)
        entry_p = self._index.get(skill_name)
        if entry_p is None:
            yield CapabilityEvent(kind="error",
                                  payload={"code": "SKILL_NOT_FOUND",
                                           "message": f"skill '{skill_name}' not in index"})
            return
        provider, bare_skill_name = entry_p

        _, fn, valid_keys = entry
        filtered = {k: v for k, v in arguments.items() if k in valid_keys}

        try:
            result: SkillResult = await fn(
                **filtered,
                skill_provider=provider,
                skill_name=bare_skill_name,
                ctx=ctx,
            )
        except Exception as exc:
            logger.exception("skill_executor: %s failed", tool_name)
            yield CapabilityEvent(kind="error",
                                  payload={"code": "SKILL_EXEC_ERROR", "message": str(exc)})
            return

        if result.is_error:
            yield CapabilityEvent(kind="error",
                                  payload={"code": "SKILL_EXEC_ERROR", "message": result.content})
        else:
            yield CapabilityEvent(kind="result", payload={"content": result.content})

    async def cancel(self, invocation_id: str, ctx: ProviderContext) -> None:
        pass

    async def describe(self, ctx: ProviderContext) -> CapabilityProviderInfo:
        return CapabilityProviderInfo(
            name=self.name,
            capability_count=len(_SKILL_EXECUTOR_TOOLS),
            supports_streaming=False,
            supports_cancel=False,
            description=self.description,
        )
