"""MemoryProvider 协议（统一）。

定位：ctx-weft 中唯一的 memory 抽象。承载所有"短期 / 长期 / Blackboard"职能——
它们在 ctx-weft 中是**同一概念**。

核心思想：Provider 是一个**事件摄取 + 多模召回**的黑盒：
- core 把所有重要事件通过 ingest() 喂给 provider
- Provider 自由决定如何内化
- core 通过三种 recall 接口取回：recall_recent / recall_topic / recall_semantic

详见设计文档 §4.3。
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from ctx_weft.protocols.context import ContentPart, ProviderContext


# ── Event types ───────────────────────────────────────────────────────────────


class MemoryEventType(StrEnum):
    """Memory 事件类型——core 通过 ingest() 喂给 provider 的事件分类。

    继承 StrEnum 以保持 JSON / 字符串语义。

    分层见 spec/06 §3：每个类型由 EVENT_LAYER 唯一映射到一层（task / agent / session）。

    【目标形态·待重构】完整设计见 docs/superpowers/specs/2026-07-06-memory-protocol-v2-design.md。
    一句话：**记忆按 scope 归档、按 address 定位**。要点：现状 type 承担双重职责（归属路由 +
    内容筛选）且两套回合编码并存（task 层 type 区分 / agent 层 role+metadata 区分），框架机制
    演进屡次穿透协议铸新类型、事后僵尸化。v2 沿「内容种类」慢轴重画，方法面 11 → 8：
      kind:  CONVERSATION_TURN | SUMMARY | TOOL_AUDIT | PUBLICATION（封死，永不为新机制扩）
      scope: TASK | AGENT | SESSION —— 归属范围（原 MemoryLayer 更名；"层"误导纵向堆叠，实为
        横向归属分区），MemoryEvent 显式字段，EVENT_LAYER 退役为 legacy 兜底；可随执行模型
        缓慢生长（如将来的 USER/TENANT）
      address: MemoryAddress（原 MemoryScope 数据类更名——它是坐标不是范围）：全址 = ingest
        归档地址，半址 = 过滤模式（None 字段 = 通配，非法字段抛 ValueError）
      role:  user | assistant | tool —— 回合按 LLM message 模型编码，机制住 metadata（约定注册表）
      读 = 三种记忆动作：load_view(address, scope, kinds=None) 全量幸存视图、时间正序、无
        limit/count（22 调用点普查：limit 是伪能力兼丢最老端的潜伏 bug）/ recall_topic /
        recall_semantic；recall_recent_by_agent 消解为半址 MemoryAddress(agent_id=A)。
      写 = ingest + fold(supersede_ids, replacements)：遗忘+补偿原子完成（修徒手 supersede+
        ingest 的崩溃丢摘要窗口）；keep_last/protect_types 策展政策上移框架侧，apply_compact 消亡。
    演进规则：新框架机制 = 新 metadata 约定，永不铸新 kind；存量旧类型零数据迁移、读侧统一归一
    （legacy_dispatch shim 届时并入唯一归一化模块；postgres 列名 layer 不改，provider 内部映射）。
    """

    # ── task 层（一次 task 的私有执行对话）──
    USER_PROMPT = "user_prompt"            # 用户任务输入；retry 时也追加于此
    LLM_RESPONSE = "llm_response"          # actor LLM 一轮的完整输出（metadata.tool_calls 供无损重建）
    TOOL_INVOCATION = "tool_invocation"    # actor 一次「真实能力」工具调用（仅审计，不进装配）
    TOOL_RESULT = "tool_result"            # 「真实能力」工具返回值
    TASK_COMPACT_SUMMARY = "task_compact_summary"    # task compact（observe active）产出的 [Context so far]

    # ── agent 层（该 agent 的对话日志，跨 task 持久）──
    AGENT_COMPACT_SUMMARY = "agent_compact_summary"  # agent compact 折叠超龄单元产出的滚动经验摘要
    # agent 层统一对话回合：dispatch 框/result（gateway plan 框 + finalize 铸框/回填）、finish 对
    # （finalize 合成、bg observe 替换）、inherit 快照（spawn 镜像父视图）。与 task 层同构——
    # 靠 role（assistant/tool）+ metadata（tool_calls/tool_call_id/origin_task_id/parent_task_id）
    # 区分回合并无损重建，不再按事件类型区分。
    AGENT_CONVERSATION_TURN = "agent_conversation_turn"

    # ── session / topic ──
    BLACKBOARD_PUBLISH = "blackboard_publish"  # 显式 topic 发布（见 spec/04）

    # ── legacy（§5.0 枚举永不物理删除；写侧已死，仅读侧兼容存量数据）──
    TASK_DISPATCH = "task_dispatch"               # legacy：新数据不再写；读侧 normalize_legacy_dispatch 归一为 AGENT_CONVERSATION_TURN（assistant 回合）
    TASK_DISPATCH_RESULT = "task_dispatch_result"  # legacy：同上（tool 回合）；线上无存量后删 legacy_dispatch 模块即可日落
    OBSERVER_SUMMARY = "observer_summary"  # 半僵尸：尚存两个写点（runtime tracking flush / suspend 挂起摘要）但不进装配，仅影响计数/估算口径
    COMPACT_SUMMARY = "compact_summary"    # 死类型：已无写点（apply_compact 按层写 TASK/AGENT_COMPACT_SUMMARY）；读侧仅 prepare 估算仍带到


class MemoryLayer(StrEnum):
    """Memory 分层（spec/06 §2）。scope key 与 seq 计数按层分区。

    【目标形态更名 MemoryScope】"层"误导为纵向抽象堆叠，实为横向归属分区（这条记忆归谁：
    task-scoped 私有转录 / agent-scoped 跨 task 经验 / session-scoped 共享黑板）；
    见 v2 设计 §2 命名注记。"""

    TASK = "task"        # tenant|session|task|<task_id>
    AGENT = "agent"      # tenant|session|agent|<agent_id>
    SESSION = "session"  # tenant|session


# 事件类型 → 层（spec/06 §3）。唯一映射，ingest/recall 据此选 scope key。
# 注：「type 唯一决定层」已在松动——apply_compact 显式传 layer、provider recall 宽容混层；
# 目标形态下 layer 是 MemoryEvent 显式字段，本映射仅为 legacy 类型兜底（见 MemoryEventType docstring）。
EVENT_LAYER: dict[MemoryEventType, MemoryLayer] = {
    MemoryEventType.USER_PROMPT: MemoryLayer.TASK,
    MemoryEventType.LLM_RESPONSE: MemoryLayer.TASK,
    MemoryEventType.TOOL_INVOCATION: MemoryLayer.TASK,
    MemoryEventType.TOOL_RESULT: MemoryLayer.TASK,
    MemoryEventType.TASK_COMPACT_SUMMARY: MemoryLayer.TASK,
    MemoryEventType.AGENT_COMPACT_SUMMARY: MemoryLayer.AGENT,
    MemoryEventType.AGENT_CONVERSATION_TURN: MemoryLayer.AGENT,
    MemoryEventType.BLACKBOARD_PUBLISH: MemoryLayer.SESSION,
    # legacy 类型（写侧已死，读侧兼容存量）
    MemoryEventType.TASK_DISPATCH: MemoryLayer.AGENT,
    MemoryEventType.TASK_DISPATCH_RESULT: MemoryLayer.AGENT,
    MemoryEventType.OBSERVER_SUMMARY: MemoryLayer.AGENT,
    MemoryEventType.COMPACT_SUMMARY: MemoryLayer.AGENT,
}


def layer_for_types(types: list[MemoryEventType]) -> MemoryLayer:
    """从一次召回请求的类型集合推导层；要求同层，混层抛错（spec/06 §8）。"""
    layers = {EVENT_LAYER[t] for t in types}
    if len(layers) != 1:
        raise ValueError(f"recall types span multiple memory layers: {layers} for {types}")
    return layers.pop()


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass
class MemoryScope:
    """记忆范围限定。

    【目标形态更名 MemoryAddress】它是坐标不是范围：全址 = ingest 归档地址，
    半址 = load_view 过滤模式（None 字段 = 通配）；"scope" 一名让位给归属范围枚举
    （原 MemoryLayer）。见 v2 设计 §3。"""

    session_id: str
    task_id: str | None = None
    agent_id: str | None = None


@dataclass
class MemoryEvent:
    """ingest 的输入：一条要写入的 memory 事件。"""

    type: MemoryEventType
    scope: MemoryScope
    content: str | list[ContentPart]
    timestamp: datetime
    # 以下字段有默认值
    # 调用方预生成 record id（v2 设计 §4 · 2026-07-27 增补，投影化前置）。
    # 给定 → provider 必须采用并按 id 幂等（重复 ingest = no-op）；None → provider 生成。
    id: str | None = None
    role: Literal["user", "assistant", "system", "tool"] | None = None
    topic: str | None = None  # 用于 topic-style 事件（含父子 task 通信）
    causation_id: str | None = None  # 关联上游 event
    metadata: dict = field(default_factory=dict)


@dataclass
class MemoryRecord:
    """recall 返回的统一表示。"""

    id: str
    type: MemoryEventType
    content: str | list[ContentPart]
    timestamp: datetime
    role: Literal["user", "assistant", "system", "tool"] | None = None
    topic: str | None = None
    score: float | None = None  # 仅 recall_semantic 时填
    metadata: dict = field(default_factory=dict)


@dataclass
class Subscription:
    """task 对 topic 的订阅（持久化）。

    task_id = 订阅方任务（读取该 topic 的 task）；"" 表示 session 级订阅（如 long_term_*）。
    """

    session_id: str
    topic: str
    cursor: int  # 已读到的 seq_no
    # subtask=自己派生的子任务结果（可 review/reopen）；predecessor=同 plan 前序结果（只读）；
    # long_term_*=跨 session 长期上下文。
    intent: Literal["subtask", "predecessor", "long_term_background", "long_term_project_log"]
    task_id: str = ""
    priority: int = 5


@dataclass
class CompactResult:
    """apply_compact 返回。"""

    events_before: int  # apply_compact 之前未 superseded 的事件数
    events_after: int  # 之后未 superseded 的事件数（含新写入的 TASK/AGENT_COMPACT_SUMMARY）
    summary_event_id: str  # 新写入的 TASK/AGENT_COMPACT_SUMMARY 事件 id（按 layer 定类型）


@dataclass
class MemoryProviderInfo:
    """Provider 能力声明。"""

    name: str
    supports_semantic: bool = False  # 是否支持 recall_semantic
    supports_topic: bool = True  # 是否支持 topic / subscription
    supports_compact_archival: bool = True  # apply_compact 是否真的归档
    max_event_size_bytes: int | None = None


# ── Protocol ──────────────────────────────────────────────────────────────────


@runtime_checkable
class MemoryProvider(Protocol):
    """统一 memory：摄取所有事件 + 多模召回。单实例，必需。"""

    name: str

    # ── 摄取（write）──

    @abstractmethod
    async def ingest(
        self,
        event: MemoryEvent,
        ctx: ProviderContext,
    ) -> str:
        """写入一个 memory event；返回 event id。

        core 调用方必须为每个重要事件调用 ingest；provider 自由决定是否持久化、
        如何索引。

        id 契约（v2 设计 §4 · 2026-07-27 增补）：event.id 给定时必须采用并原样回显，
        且**按 id 幂等**——已存在的 id（含已 superseded）= no-op，返回该 id，不比对
        内容、不重复写入、不推进任何计数器。None → provider 自行生成。
        """
        ...

    # ── 召回（read，三种模式）──

    @abstractmethod
    async def recall_recent(
        self,
        scope: MemoryScope,
        types: list[MemoryEventType],
        limit: int,
        ctx: ProviderContext,
    ) -> list[MemoryRecord]:
        """按时间倒序返回最近 N 条指定类型事件。必需实现。

        PrepareStep 装配 messages 段的主路径。
        """
        ...

    @abstractmethod
    async def recall_recent_by_agent(
        self,
        agent_scope: MemoryScope,
        types: list[MemoryEventType],
        limit: int,
        ctx: ProviderContext,
    ) -> list[MemoryRecord]:
        """召回某 agent 名下**所有 task** 的 task 层记录（按 agent_id 跨 task，忽略 task_id）。

        统一 AgentRecall 装配路径用：OPEN task 的对话据此还原（CLOSED task 的对话已被
        close 时 supersede，不会返回）。按 timestamp 倒序，每条 metadata["task_id"] 标来源。

        【目标形态下消解】本方法与 recall_recent 在两个 provider 里都是同一条查询、仅差
        匹配 task_id 还是 agent_id；layer 显式化 + selector 语义后并入统一 recall（见
        MemoryEventType docstring），过渡期留薄包装。迁移时注意：现有三个调用点传的是带
        task_id 的全量 scope，须显式改为 task_id=None 的 selector。
        """
        ...

    @abstractmethod
    async def recall_topic(
        self,
        topic: str,
        since: int,
        ctx: ProviderContext,
    ) -> tuple[list[MemoryRecord], int]:
        """按 topic 拉取（自 since seq_no 之后），返回 (events, new_cursor)。

        必需实现。父子 task 通信 + 跨 session 订阅式上下文用。
        """
        ...

    @abstractmethod
    async def recall_semantic(
        self,
        query: str,
        scope: MemoryScope,
        top_k: int,
        ctx: ProviderContext,
    ) -> list[MemoryRecord]:
        """语义相似度召回。可选——core 默认实现返空；外部实现核心能力。

        Provider 通过 describe() 声明是否支持。
        """
        ...

    # ── 订阅（cross-session 长期上下文）──

    @abstractmethod
    async def subscribe_topic(
        self,
        session_id: str,
        topic: str,
        intent: Literal["subtask", "predecessor", "long_term_background", "long_term_project_log"],
        ctx: ProviderContext,
        task_id: str = "",
    ) -> str:
        """task 订阅 topic；返回 subscription id。幂等：同 (session, task, topic) 重复订阅保留游标。"""
        ...

    @abstractmethod
    async def list_subscriptions(
        self,
        session_id: str,
        ctx: ProviderContext,
        task_id: str | None = None,
    ) -> list[Subscription]:
        """列出订阅。task_id 给定时只返回该 task 的订阅 + session 级订阅（task_id=""）；None 返回全部。"""
        ...

    # ── 压缩（compact 触发时使用）──

    @abstractmethod
    async def apply_compact(
        self,
        scope: MemoryScope,
        summary: str,
        keep_last: int,
        ctx: ProviderContext,
        layer: MemoryLayer = MemoryLayer.AGENT,
        protect_types: tuple[MemoryEventType, ...] = (),
        since_last: MemoryEventType | None = None,
    ) -> CompactResult:
        """折叠指定 layer 的 scope（spec/06 §7）。

        - layer=TASK：task compact，写 TASK_COMPACT_SUMMARY，折叠 task 层执行转录。
        - layer=AGENT：agent compact，写 AGENT_COMPACT_SUMMARY，按「完整派发对」折叠 agent 层。

        把该 layer scope 内、超出 keep_last 范围的事件标记 superseded
        （core 默认实现物理 archive；外部实现可能仅更新索引）。

        since_last（段作用域折叠，2026-07-21）：非 None 时归档池限定在「最后一条 active
        该类型记录之后」——段边界折叠传 USER_PROMPT，短段免折残留的前段 raw 不被跨段
        折入本摘要（防合并摘要抢锚到前一条 UP 之前）。该类型记录不存在 → 不限定。

        排序契约（2026-07-21，实现方必须遵守）：段界搜索、归档池切分、锚点判定一律按
        **渲染序 (timestamp, seq_no)**，与 recall 的 timestamp 序一致。不得用裸 seq_no——
        存在 timestamp 回填、seq 更高的合法记录（L3 坍缩 UP，见 collapse_task_layer），
        seq 序会把段界推到所有 raw 之后（摘要照写、raw 不折）。

        锚点语义：摘要落「被折区起点之后第一条幸存事件之前」；段尾无幸存者则锚到被折段
        末条事件位置（不用 now()，防迟到摘要越过新 USER_PROMPT）。
        """
        ...

    @abstractmethod
    async def supersede(
        self,
        event_ids: list[str],
        ctx: ProviderContext,
    ) -> int:
        """把给定 event id 标记为 superseded（此后不再被 recall）。返回实际标记的条数。

        语义判定留框架、provider 只按 id 执行。主要调用方：finalize 折 task 层末 raw 段、
        fold_root_experience 折超 keep_last 顶层单元（agent 层 conversation turn + task 层
        胶囊跨层一并折）、bg observe 替换 finish 对占位。已 superseded / 不存在的 id 跳过。
        """
        ...

    # ── 工具 ──

    @abstractmethod
    async def count_recent(
        self,
        scope: MemoryScope,
        types: list[MemoryEventType],
        ctx: ProviderContext,
    ) -> int:
        """计数（供 PrepareStep 估算消息数）。"""
        ...

    @abstractmethod
    async def describe(self, ctx: ProviderContext) -> MemoryProviderInfo:
        """返回 provider 能力声明。"""
        ...
