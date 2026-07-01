"""AgentTemplate / IdentityFacet / TemplateResolver。

Identity 是 AgentTemplate 的内禀字段（一等公民），不嵌套在 Capability 里。
Capability 通过 capability_refs 引用外部能力。

详见设计文档 §4.6。
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable, Literal

from ctx_weft.protocols.capability import Purpose
from ctx_weft.protocols.context import ProviderContext

CapabilityMode = Literal["optional", "required", "forbidden"]


# ── Identity ──────────────────────────────────────────────────────────────────


@dataclass
class IdentityFacet:
    """Agent 在某个 purpose 下的身份呈现。

    例：
      facets["act"] → Actor 阶段的 SOUL（人格、价值观、行为风格）
      facets["observe"] → Observer 阶段的 ROLE（职责定义、评估准则）
    """

    text: str  # 该 purpose 下的身份文本（SOUL 或 ROLE 内容）
    style: str | None = None  # 该 purpose 的输出风格偏好（可选）


# ── Capability Reference ──────────────────────────────────────────────────────


@dataclass
class CapabilityRef:
    """Template 中对外部 capability 的声明引用。"""

    capability_id: str  # 匹配某 provider 返回的 Capability.id
    mode: CapabilityMode = "optional"
    # optional  → 由 retrieve(ctx) 决定是否出现
    # required  → 强制加载，走 list() 精确查找
    # forbidden → 强制排除，即使 retrieve() 返回也过滤掉
    # 注：purposes 不在 ref 中——以 Capability 自身声明为准（v0.3 决议）


# ── Config 子结构 ──────────────────────────────────────────────────────────────


@dataclass
class MemoryConfig:
    """Memory 配置（从 template 拷贝到 Agent 实例化时快照）。"""

    short_window_size: int = 20
    summary_threshold: int = 20
    use_long_term: bool = True
    subscribed_blackboard_topics: list[str] = field(default_factory=list)


@dataclass
class LoopConfig:
    """Loop 配置（从 template 拷贝到 Agent 实例化时快照）。

    完整字段见设计文档 §6.8.2（含 compact 阈值等）。
    """

    max_turns_per_act: int = 50
    max_turns_per_observe: int = 5       # ObserveStep ReAct 循环上限
    max_turns_per_agent: int = 20
    timeout_per_step_sec: int = 120
    failure_threshold: int = 3
    max_spawn_depth: int = 4
    compact_token_ratio: float = 0.8
    # 压缩「压到」目标比率（滞后区下沿）：触发后一路升级直到 token 估算 < 此比率 * context_limit。
    # 0 = 无滞后，回退等于 compact_token_ratio（压到刚低于触发比率即停）。应设得比 compact_token_ratio 低。
    compact_target_ratio: float = 0.0
    compact_message_delta: int = 20      # DEPRECATED（2026-07-01）：compact 改纯预算驱动，本字段不再被读
    compact_keep_last: int = 6           # 保留底线（非触发门）：agent 层折叠保留的胶囊数；更老的折成摘要
    collapse_keep_last: int = 3          # 保留底线（非触发门）：task 坍缩保留的最近段摘要条数
    # 派发前压缩阈值：派发工具调用执行前，若本轮 prompt token / context_limit >= 此值，
    # 先对父自身 task 层对话压缩一次（fold_task），使父 resume 更精简、inherit 快照为
    # 压缩后版本。0 = 关闭（默认）。通常设得比 compact_token_ratio 更早触发。
    predispatch_compact_token_ratio: float = 0.0
    # 短任务（叶子）保留阈值（spec 2026-06-23）：finish 时 task 层对话 token ≤ threshold
    # 且 LLM_RESPONSE 轮次 ≤ turn_cap → 不 close（保留完整对话）；否则 close 成残留。
    short_task_token_threshold: int = 1000
    short_task_turn_cap: int = 2


# ── AgentTemplate ─────────────────────────────────────────────────────────────


@dataclass
class AgentTemplate:
    """Agent 实例化的蓝图。由 host 在 template registration 阶段构建并管理。
    core 通过 TemplateResolver 协议读取。
    """

    id: str
    name: str
    version: str  # semver；既是 template 版本也是 identity 版本

    # 身份定义（inline，一等字段）
    identity: dict[Purpose, IdentityFacet]
    # 外部能力引用：tool / skill / sub-agent
    capability_refs: list[CapabilityRef]

    # 子配置
    memory_config: MemoryConfig
    loop_config: LoopConfig
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentTemplateSummary:
    """用于发现/列表场景（如 sub-agent 选择）。"""

    id: str
    name: str
    version: str
    description: str


# ── TemplateResolver ──────────────────────────────────────────────────────────


@runtime_checkable
class TemplateResolver(Protocol):
    """core 与 host 之间关于 template 的唯一接口。host 实现。"""

    @abstractmethod
    async def get(
        self,
        template_id: str,
        version: str | None,
        ctx: ProviderContext,
    ) -> AgentTemplate:
        """获取 template 定义。version=None 取最新；指定 version 用于实例化时 pin。"""
        ...

    @abstractmethod
    async def list_summaries(
        self,
        ctx: ProviderContext,
    ) -> list[AgentTemplateSummary]:
        """列出可用 template 摘要——用于 sub-agent 发现、admin 界面等。"""
        ...
