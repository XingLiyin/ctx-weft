"""Capability 协议层。

三种 capability 子类：
  ToolCapability   — LLM 可直接调用，走 ToolCapabilityProvider.invoke()
  SkillCapability  — 轻量描述符，不含 dir/source；provider 负责所有加载/执行
  AgentCapability  — sub-agent 模板描述符，由 orchestrator spawn

三种 provider 子类：
  ToolCapabilityProvider  — 实现 invoke() / cancel()
  SkillCapabilityProvider — 实现 Level2 load_definition() + Level3 list_files/load_resource/exec_script
  AgentCapabilityProvider — list() 发现 + get_template() 加载，发现与加载同源
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from ctx_weft.protocols.context import ProviderContext

if TYPE_CHECKING:
    from ctx_weft.protocols.template import AgentTemplate


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

    def __post_init__(self) -> None:
        # 归一 description：声明为 str，但 provider 可能透传 None（如无描述的 MCP 工具 /
        # sub-agent 模板）。None 会经 CapabilitySource 落成 ContextBlock.content=None，
        # 装配期 content_to_text 迭代 None 崩溃。在唯一构造入口堵住，覆盖所有子类/provider。
        if self.description is None:
            self.description = ""


@dataclass
class ToolCapability(Capability):
    """LLM 可直接调用，走 ToolCapabilityProvider.invoke()。"""
    kind: str = "tool"
    input_schema: dict[str, Any] = field(default_factory=dict)
    side_effects: bool = False
    spillable: bool = True  # 输出超长时是否允许 gateway 落盘；可重新派生的只读工具置 False


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
    version: str = ""  # 信息性（listing 展示）；加载一律 version=None 取最新


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
    # Gateway 注入 ``ctx.invocation_id``（本次执行的唯一 id）：需要支持取消的 provider 应据此登记
    # 在途句柄（任务/进程/请求），以便后续 cancel(invocation_id) 对应。``ctx.extra["tool_call_id"]``
    # 是发起本次调用的模型 tool_call id（可重放，用于 §6 配对），与 invocation_id 区别见两者文档。

    @abstractmethod
    async def cancel(self, invocation_id: str, ctx: ProviderContext) -> None:
        """取消一次在途执行。``invocation_id`` 即 invoke 时经 ``ctx.invocation_id`` 注入的同一个 id。

        多数本地 provider 无需实现（取消经 ``CancelledError`` 传播到 invoke 协程的 finally，如
        bash 的 terminate_tree 杀进程树）；需要显式取消通知的 provider（如 MCP 远端）按 invocation_id
        查到登记的句柄并取消。best-effort：被 gateway 在取消路径上调用，不应抛出。
        """
        ...


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
    """列出可用 sub-agent 模板，并负责加载自己列出的模板。

    发现与加载同源（spec 2026-07-22）：list() 返回的每个 AgentCapability.template_name，
    本 provider 的 get_template() 必须能加载。TemplateLookup 按 cap.id 前缀路由到本
    provider 后，传入的是**局部模板名**（前缀已剥掉）。
    """

    @abstractmethod
    async def get_template(
        self, template_id: str, version: str | None, ctx: ProviderContext,
    ) -> "AgentTemplate | None":
        """加载模板定义。不认识该 id → 返回 None（由 TemplateLookup 转成
        TemplateNotFoundError）；仅真实故障（IO/网络/解析错误）才抛异常。
        version=None 取最新。"""
        ...

    async def retrieve(self, ctx: ProviderContext) -> list[Capability]:
        """默认不自动召回：sub-agent 只经模板声明的 `subagents` required refs 绑定
        （allowlist），永不把整个模板目录泄漏给 agent。确需语义召回的 provider 可覆盖。"""
        return []


# ── 授权契约 ───────────────────────────────────────────────────────────────────


@dataclass
class AuthorizationDecision:
    """一次授权的结构化结果。"""

    allowed: bool
    message: str = ""                              # 反馈 / 拒绝指导，回灌给 LLM（allow / deny 都可带）
    modified_arguments: dict[str, Any] | None = None  # allow 时的有效参数（None = 用原参）
    defer: bool = False                            # spec/07 §7：挂起本次调用（不放行也不拒绝；gateway 绝不 invoke）


class Authorizer(ABC):
    """对一次 capability 调用作授权决定。

    核心方法 ``authorize`` 对**一次工具调用**作放行/拦截决定，并可携带回灌给 LLM 的
    ``message``（反馈/拒绝指导）与 allow 时的 ``modified_arguments``（改写参数）。
    曾有一个基于 ``authorize`` 的批量 ``filter`` 默认实现，**已删除**：它零调用点，且对
    ``HumanConfirmationAuthorizer`` 会**真的发出一个 HITL 请求并等人**——把「列一下有哪些
    工具可见」变成「向人类逐个求批」。真需要装配期可见性过滤时应另行设计，届时必须显式
    排除会挂起的 authorizer。

    只收 ``ProviderContext``（session/task/agent/模板 标识齐备），不收 core 的 Agent/Task
    状态对象——契约层不依赖 core 状态，host 自实现时也只需面对 protocols。
    内置实现见 ``ctx_weft.providers.authorizer``。
    """

    @abstractmethod
    async def authorize(
        self,
        capability: Capability,
        ctx: ProviderContext,
        arguments: dict[str, Any] | None = None,
        *,
        tool_call_id: str = "",
    ) -> AuthorizationDecision: ...
