"""Capability 协议层。

三种 capability 子类：
  ToolCapability   — LLM 可直接调用，走 ToolCapabilityProvider.invoke()
  SkillCapability  — 轻量描述符，不含 dir/source；provider 负责所有加载/执行
  AgentCapability  — sub-agent 模板描述符，由 orchestrator spawn

三种 provider 子类：
  ToolCapabilityProvider  — 实现 invoke() / cancel()
  SkillCapabilityProvider — 实现 Level2 load_definition() + Level3 list_files/load_resource/exec_script
  AgentCapabilityProvider — list() 返回 AgentCapability，无额外方法
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

from ctx_weft.protocols.context import ProviderContext


# ── Purpose ───────────────────────────────────────────────────────────────────

Purpose = Literal["act", "observe", "compact", "recognize_intent"]


# ── Qualified tool name ────────────────────────────────────────────────────────


def qualify(capability_id: str) -> str:
    """capability_id → provider-qualified, LLM-safe tool name.

    LLM function names allow only ``^[A-Za-z0-9_-]{1,64}$`` — colons are invalid,
    so the ``provider:tool`` id is encoded with ``__`` as the separator
    (``mcp:github:create_issue`` → ``mcp__github__create_issue``). This is the
    single canonical tool name the LLM sees, stores in memory, and is classified
    by. Routing still uses the raw ``cap.id``.
    """
    return capability_id.replace(":", "__")


# ── Capability 基类 + 三个子类 ─────────────────────────────────────────────────

@dataclass
class Capability:
    id: str
    name: str
    kind: Literal["tool", "skill", "agent"]
    description: str = ""
    purposes: list[Purpose] = field(default_factory=lambda: ["act"])


@dataclass
class ToolCapability(Capability):
    """LLM 可直接调用，走 ToolCapabilityProvider.invoke()。"""
    kind: str = "tool"
    input_schema: dict[str, Any] = field(default_factory=dict)
    side_effects: bool = False


@dataclass
class SkillCapability(Capability):
    """轻量描述符：不含 dir / source / remote_source_name。
    provider 通过 cap.id prefix 隐式关联（"local_skill:x" → LocalSkillCapabilityProvider）。
    """
    kind: str = "skill"
    triggers: list[str] = field(default_factory=list)
    version: str = ""


@dataclass
class AgentCapability(Capability):
    """Sub-agent 模板描述符，由 orchestrator 负责 spawn。"""
    kind: str = "agent"
    template_name: str = ""


# ── Level 2（按需加载，不进 cache）────────────────────────────────────────────

@dataclass
class SkillDefinition:
    skill_id: str      # 对应 SkillCapability.id
    skill_name: str
    instructions: str  # SKILL.md frontmatter 之后的全文


# ── 公共事件 / 元信息 ──────────────────────────────────────────────────────────

@dataclass
class CapabilityEvent:
    kind: Literal["progress", "stdout", "stderr", "result", "error"]
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class CapabilityProviderInfo:
    name: str
    capability_count: int = 0
    supports_streaming: bool = True
    supports_cancel: bool = True
    description: str = ""


# ── Provider 基类 + 三个子类 ───────────────────────────────────────────────────

class CapabilityProvider(ABC):
    """所有 provider 的基类：仅声明 list()、retrieve() 和 describe()。"""

    name: str
    # 可选：描述本 provider 的 capability 集合能做什么、应当如何使用。
    # 内置 provider 可在定义时由用户自定义；MCP provider 用 server 的
    # initialize.instructions 填充。装配时按 provider 分块写入 LLM prompt。
    description: str = ""

    @abstractmethod
    async def list(self, ctx: ProviderContext) -> list[Capability]: ...

    async def retrieve(self, ctx: ProviderContext) -> list[Capability]:
        """根据当前上下文返回可能需要的 capability 子集。

        默认回落到 list()；provider 可按需覆盖以实现语义检索或按
        ctx.task_settings / ctx.extra 过滤。
        """
        return await self.list(ctx)

    @abstractmethod
    async def describe(self, ctx: ProviderContext) -> CapabilityProviderInfo: ...


class ToolCapabilityProvider(CapabilityProvider, ABC):
    """暴露 LLM 可直接调用的工具，经由 CapabilityGateway dispatch。"""

    @abstractmethod
    def invoke(
        self,
        capability_id: str,
        arguments: dict[str, Any],
        ctx: ProviderContext,
    ) -> AsyncIterator[CapabilityEvent]: ...

    @abstractmethod
    async def cancel(self, invocation_id: str, ctx: ProviderContext) -> None: ...


class SessionScopedCapabilityProvider(CapabilityProvider, ABC):
    """持有 per-session 内存状态的 provider：core 在 session 结束时统一清理。

    与具体状态语义无关——control provider 持有 TaskManager、文件系统 provider 持有
    workspace 路径等，各自如何登记是实现细节；协议只约定「session 结束时按 id 释放」
    这一通用清理钩子。core（runtime）遍历所有此类 provider 调 deregister_session()，
    无需知道它们各自持有什么。
    """

    @abstractmethod
    def deregister_session(self, session_id: str) -> None: ...


class SkillCapabilityProvider(CapabilityProvider, ABC):
    """管理技能目录/远端源。list() 返回 SkillCapability（Level 1）。
    Level 2 / Level 3 均由子类实现；SkillExecutorCapabilityProvider 路由到此。
    """

    @abstractmethod
    async def load_definition(
        self, skill_name: str, ctx: ProviderContext,
    ) -> SkillDefinition | None:
        """Level 2：加载 SKILL.md 主体（instructions）。"""
        ...

    @abstractmethod
    async def list_files(
        self, skill_name: str, pattern: str, limit: int, ctx: ProviderContext,
    ) -> str:
        """Level 3：列举技能目录下匹配 pattern 的文件。"""
        ...

    @abstractmethod
    async def load_resource(
        self, skill_name: str, resource_path: str, ctx: ProviderContext,
    ) -> str:
        """Level 3：读取技能目录内的参考文件。"""
        ...

    @abstractmethod
    async def exec_script(
        self, skill_name: str, script_path: str, args: str, ctx: ProviderContext,
    ) -> str:
        """Level 3：执行技能目录内的脚本，返回 stdout。"""
        ...


class AgentCapabilityProvider(CapabilityProvider, ABC):
    """列出可用 sub-agent 模板。list() 返回 AgentCapability；无额外方法。"""
    pass
