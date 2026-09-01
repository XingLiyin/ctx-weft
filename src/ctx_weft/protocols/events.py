"""Event 领域的 host-facing 契约：事件数据类型 + EventBus + EventStore。

划界判据（spec 2026-08-27-protocols-layer-event-contracts-design §2，
用户 2026-08-28 裁定改为三层）：
**契约进 `protocols/`，实现进 `providers/`，`core/` 只留编排。**

故本模块装：`Event` / `EventFilter` / `EventType` 与两个常量集（host 要构造事件、
要持久化、要按类型分派）、`EventBus` 协议（README 明说 host 可换 Redis Streams）、
`EventStore` 协议与 `RunSnapshot`（host 必须实现 append + read_by_session）。

**不装**：`InProcessEventBus` / `InMemoryEventStore`（内置实现，在
`providers/events/bus/in_process/bus.py` / `providers/events/store/in_memory/store.py`）、
`TASK_STATUS_BY_EVENT`（core 的投影逻辑，且依赖 core 的 `TaskStatus`）。

⚠️ **本模块不得 import `ctx_weft.core` 的任何东西。** protocols 是比 core 低的层；
反向依赖会让 `protocols/context.py` 那个刻意的惰性绑定失去意义，并在某些 import
顺序下变成真实的循环导入。`tests/unit/test_protocols_events_relocation.py` 钉住这条。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    # 仅类型注解用；`protocols/events.py` 运行时只依赖 stdlib 的现状不变
    # （同层 import 不违反层序 ast 守卫，但保持现状更稳，见文件顶部说明）。
    from ctx_weft.protocols.context import ProviderContext


@dataclass
class Event:
    """事件基类。所有事件共享此结构；payload 字段按事件类型而异（详见 §9.4-§9.6）。"""

    id: str  # evt_ULID
    run_id: str | None  # 一次 loop run 的标识；某些 session 级事件可为 None
    sequence: int  # 在同一 run_id 内单调递增
    session_id: str
    type: str  # 见 EVENT_TYPES
    timestamp: datetime
    tenant_id: str = "default"
    task_id: str | None = None
    agent_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    causation_id: str | None = None
    schema_version: int = 1  # payload 版本；reducer 据此分支


@dataclass
class EventFilter:
    """订阅过滤器。"""

    session_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    types: list[str] | None = None  # None=全部


# ── V1 冻结的事件类型清单（详见 §9.3）────────────────────────────────────────────


class EventType(StrEnum):
    """V1 冻结的事件类型枚举。

    继承 StrEnum：成员即字符串（`EventType.TASK_CREATED == "TaskCreated"`、
    `json.dumps` / `str()` 都得到 `"TaskCreated"`），因此对所有按字符串比较 /
    序列化的消费端完全向后兼容。业务代码应优先引用枚举成员而非字面量。
    """

    # ── Session / Run / Step 域 ──
    SESSION_CREATED = "SessionCreated"
    SESSION_RESUMED = "SessionResumed"     # recover_session 续跑被打断的 session 时发
    SESSION_STATUS_CHANGED = "SessionStatusChanged"
    SESSION_FINISHED = "SessionFinished"   # TaskManager 确定 session 真正结束时发（含 final_status）
    SESSION_PAUSED_HITL = "SessionPausedHitl"
    RUN_STARTED = "RunStarted"
    RUN_PAUSED = "RunPaused"
    RUN_RESUMED = "RunResumed"
    RUN_CANCELED = "RunCanceled"
    RUN_FINISHED = "RunFinished"
    STEP_STARTED = "StepStarted"
    STEP_COMPLETED = "StepCompleted"
    STEP_FAILED = "StepFailed"
    # ── Task 域 ──
    TASK_CREATED = "TaskCreated"
    TASK_STARTED = "TaskStarted"
    TASK_SUSPENDED = "TaskSuspended"
    TASK_RESUMED = "TaskResumed"
    TASK_FINISHED = "TaskFinished"
    TASK_FAILED = "TaskFailed"
    TASK_CANCELED = "TaskCanceled"
    TASK_FINALIZED = "TaskFinalized"
    TASK_REQUEUED = "TaskRequeued"
    BLACKBOARD_PUBLISHED = "BlackboardPublished"
    # ── Agent 域 ──
    AGENT_INSTANTIATED = "AgentInstantiated"
    AGENT_SPAWNED = "AgentSpawned"
    AGENT_STATUS_CHANGED = "AgentStatusChanged"
    AGENT_WAITING = "AgentWaiting"
    AGENT_FINALIZED = "AgentFinalized"
    SPAWN_REJECTED = "SpawnRejected"
    # ── Context 域 ──
    PREPARE_COMPLETED = "PrepareCompleted"
    CONTEXT_TOKENS_ESTIMATED = "ContextTokensEstimated"
    CONTEXT_TOKENS_MEASURED = "ContextTokensMeasured"
    CONTEXT_ASSEMBLED = "ContextAssembled"
    CONTEXT_OVERFLOWED = "ContextOverflowed"
    # ── LLM 域 ──
    LLM_REQUEST_STARTED = "LLMRequestStarted"
    LLM_PROMPT_SENT = "LLMPromptSent"           # 完整 prompt（system + messages + tools），供调试
    LLM_TOKEN_STREAMED = "LLMTokenStreamed"
    LLM_REASONING_STREAMED = "LLMReasoningStreamed"    # extended thinking delta
    LLM_RESPONSE_FINISHED = "LLMResponseFinished"
    LLM_RETRY_TRIGGERED = "LLMRetryTriggered"
    # ── Capability 域 ──
    CAPABILITY_INVOKED = "CapabilityInvoked"
    CAPABILITY_PROGRESS = "CapabilityProgress"
    CAPABILITY_FINISHED = "CapabilityFinished"
    CAPABILITY_FAILED = "CapabilityFailed"
    CAPABILITY_CANCELED = "CapabilityCanceled"
    # ── ActStep / ObserveStep 子事件 ──
    ACT_TURN_STARTED = "ActTurnStarted"
    ACT_TURN_COMPLETED = "ActTurnCompleted"
    MAX_TURNS_REACHED = "MaxTurnsReached"
    OBSERVE_COMPLETED = "ObserveCompleted"
    # ── Memory 域 ──
    MEMORY_INGESTED = "MemoryIngested"
    COMPACT_TRIGGERED = "CompactTriggered"
    COMPACT_DISPATCHED = "CompactDispatched"
    MEMORY_COMPACT_STARTED = "MemoryCompactStarted"
    MEMORY_COMPACTED = "MemoryCompacted"
    MEMORY_COMPACT_FINISHED = "MemoryCompactFinished"   # 一轮压缩收尾聚合（总折叠数/省 token/各级），供前端落一条持久标记
    MEMORY_COMPACT_FAILED_FALLBACK = "MemoryCompactFailedFallback"
    BLACKBOARD_SUBSCRIBED = "BlackboardSubscribed"
    # ── HITL 域 ──
    HITL_REQUIRED = "HitlRequired"
    HITL_APPROVED = "HitlApproved"       # approval kind 放行（无改参）
    HITL_ANSWERED = "HitlAnswered"       # input kind 取得人类文字答复
    HITL_REJECTED = "HitlRejected"
    HITL_MODIFIED = "HitlModified"       # approval kind 放行（带改参）
    HITL_TIMEOUT = "HitlTimeout"
    HITL_CANCELLED = "HitlCancelled"   # session 关闭 / interrupt / GC：收口悬挂 pending，不 requeue
    # ── HITL v2（2026-09-01 重设计）──
    # outcome 是事实本身，不再由事件类型编码结局：approved vs modified 由
    # payload 有无 modified_arguments 推出，其余由 outcome 推出。host 自定义
    # outcome 因此无需新增事件类型。上方 6 个 legacy HITL 事件在段 3 才退役。
    HITL_OPENED = "HitlOpened"
    HITL_RESOLVED = "HitlResolved"
    # ── Guard 域 ──
    TOKEN_BUDGET_WARNING = "TokenBudgetWarning"
    TOKEN_BUDGET_EXCEEDED = "TokenBudgetExceeded"
    FAILURE_THRESHOLD_HIT = "FailureThresholdHit"
    MAX_CONCURRENT_AGENTS_EXCEEDED = "MaxConcurrentAgentsExceeded"
    # ── Provider 域 ──
    MCP_SERVER_DISCONNECTED = "MCPServerDisconnected"
    MCP_SERVER_RECONNECTED = "MCPServerReconnected"
    # ── RecognizeIntent 域 ──
    RECOGNIZE_INTENT_STARTED = "RecognizeIntentStarted"
    RECOGNIZE_INTENT_LLM_PROMPT = "RecognizeIntentLLMPrompt"
    RECOGNIZE_INTENT_COMPLETED = "RecognizeIntentCompleted"
    RECOGNIZE_INTENT_TOOL_CALL = "RecognizeIntentToolCall"
    RECOGNIZE_INTENT_SKIPPED = "RecognizeIntentSkipped"
    # ── BackgroundObserve 域（root 后台异步 observe 的 LLM 交互；与 LLM_* 同形但独立类型，
    #     host 据此区分前端是否渲染——core 不感知前端可见性，只发不同类型）──
    BACKGROUND_OBSERVE_REQUEST_STARTED = "BackgroundObserveRequestStarted"
    BACKGROUND_OBSERVE_PROMPT_SENT = "BackgroundObservePromptSent"
    BACKGROUND_OBSERVE_TOKEN_STREAMED = "BackgroundObserveTokenStreamed"
    BACKGROUND_OBSERVE_RESPONSE_FINISHED = "BackgroundObserveResponseFinished"
    # ── TaskRecap 域（background observe 的持久化生命周期标记；崩溃恢复据此重跑，
    #     与逐轮 BACKGROUND_OBSERVE_* 流式事件不同——这两条是"整段 recap 起/止"的记账）──
    TASK_RECAP_STARTED = "TaskRecapStarted"   # payload: {task_id, boundary, agent_id}
    TASK_RECAP_DONE = "TaskRecapDone"         # payload: {task_id}
    # ── System / 元事件 ──
    EVENTS_DROPPED = "EventsDropped"
    SNAPSHOT_CREATED = "SnapshotCreated"


# 向后兼容：保持 `EVENT_TYPES` 为字符串 frozenset，供 `type not in EVENT_TYPES` 校验。
# StrEnum 成员即字符串，故对原有 `"TaskCreated" in EVENT_TYPES` 用法等价。
EVENT_TYPES: frozenset[str] = frozenset(EventType)


# 瞬态事件：高频流式 delta，仅供实时订阅（SSE）消费，**不进任何持久化 / 投影 / 快照路径**。
# 单一真相由 LLMResponseFinished（含完整文本）承载，reducer 也不消费这些 delta，
# 故跳过它们不影响回放、投影与崩溃恢复，只是不再把每个 token 写进事件存储与 DB。
# 持久化/投影/快照各订阅者统一引用此集合（此前 host 以字符串字面量各维护一份）。
TRANSIENT_EVENT_TYPES: frozenset[str] = frozenset({
    EventType.LLM_TOKEN_STREAMED,
    EventType.LLM_REASONING_STREAMED,
    EventType.LLM_RETRY_TRIGGERED,
    EventType.BACKGROUND_OBSERVE_TOKEN_STREAMED,
})


# ── Subscription handle ───────────────────────────────────────────────────────


@dataclass
class SubscriptionHandle:
    """订阅句柄，用于 unsubscribe。"""

    subscriber_id: str
    _bus: "EventBus"

    async def unsubscribe(self) -> None:
        await self._bus._unsubscribe(self.subscriber_id)


# ── Protocol ──────────────────────────────────────────────────────────────────


@runtime_checkable
class EventBus(Protocol):
    """事件总线。"""

    @abstractmethod
    async def emit(self, event: Event) -> None: ...

    @abstractmethod
    def subscribe(
        self,
        event_type: str | None,
        handler: Callable[[Event], Awaitable[None]],
    ) -> SubscriptionHandle: ...

    @abstractmethod
    def stream(
        self,
        filter: EventFilter,
    ) -> AsyncIterator[Event]: ...

    @abstractmethod
    async def _unsubscribe(self, subscriber_id: str) -> None: ...


# ── RunSnapshot ───────────────────────────────────────────────────────────────


@dataclass
class RunSnapshot:
    """事件流的某一时刻快照（供 host 实现 snapshot/restore 优化用）。"""

    id: str
    run_id: str
    session_id: str
    last_event_id: str
    last_event_sequence: int
    state_blob: dict[str, Any]
    snapshot_reason: str = ""
    snapshot_at: datetime | None = None


# ── EventStore Protocol ───────────────────────────────────────────────────────


@runtime_checkable
class EventStore(Protocol):
    """事件流持久化抽象。host 提供具体实现（Postgres / SQLite / in-memory）。"""

    @abstractmethod
    async def append(self, event: Event) -> None:
        """持久化单条事件。"""
        ...

    @abstractmethod
    async def read_by_session(self, session_id: str) -> list[Event]:
        """按 session_id 加载全部事件，按 id（ULID 字典序）升序排序。

        **排序键是 id，不是 sequence。** `sequence` 只在同一 `run_id` 内单调递增
        （见 `Event.sequence`）；一个 session 可以跨多个 run，按 sequence 排会把
        不同 run 的事件交错在一起（run A 的 1,2,3 与 run B 的 1,2,3 排成
        A1,B1,A2,B2,A3,B3）。`id` 是 ULID，全局单调，与 `read_after` 的排序口径
        一致，也是 `SqlEventStore` 的排序键（`ORDER BY id`）。生产里三者（append 顺序 /
        sequence 顺序 / id 顺序）通常一致，只有乱序 append 或跨 run 会话才会分叉。
        """
        ...

    # ── 可选快照扩展 ──────────────────────────────────────────────────────────
    # 未实现时抛 NotImplementedError；core 捕获后降级为全量 replay。

    async def list_active_session_ids(self) -> list[str]:
        """返回有 SessionCreated 但无终态事件的 session ID 列表（用于启动时 crash recovery）。"""
        raise NotImplementedError

    async def read_after(self, session_id: str, after_event_id: str) -> list[Event]:
        """加载 session 中 id > after_event_id 的增量事件（ULID 字典序）。

        `after_event_id` 不存在于本 session 时，字面语义已蕴含：返回 id 大于它的
        **全部**事件，不是空列表——这是纯过滤，不是"从标记处扫描、找不到就返回空"。
        调用方（如 `rebuild_view` 用快照的 `last_event_id` 调本方法）据此在标记失配
        时仍能拿到完整增量，而不是静默丢失整段 delta。
        """
        raise NotImplementedError

    async def read_session_events_of_types(
        self, session_id: str, types: "tuple[str, ...]",
    ) -> list[Event]:
        """只加载 session 中指定类型的事件（按 id / ULID 字典序升序排序）。

        排序键与 `read_by_session` 同理是 id 而非 sequence——sequence 只在同一
        `run_id` 内单调，跨 run 的 session 按它排会交错两个 run 的事件。

        轻查询——供恢复决策按事件折叠（如 HITL 待解决判定）而**不必全量回放**。
        未实现时抛 NotImplementedError；调用方降级为 read_by_session + 内存过滤。
        """
        raise NotImplementedError

    async def save_snapshot(self, snapshot: RunSnapshot) -> None:
        """持久化一个状态快照。"""
        raise NotImplementedError

    async def load_latest_snapshot(self, session_id: str) -> RunSnapshot | None:
        """加载 session 最新快照，无快照时返回 None。

        **「最新」的定义（跨实现必须一致）：按 `snapshot.snapshot_at` 取最大；
        `snapshot_at` 相同时按 `snapshot.id` 取最大。** 这条口径选 `snapshot_at`
        而不是「最后一次 `save_snapshot` 调用」，是因为写入顺序不保证与时间顺序
        一致（并发写、重试补写都可能乱序），而 `created_at` / `id` 是可以跨实现
        定义的稳定排序键，「哪次调用最后执行」不是。`SnapshotWriter` 目前只按
        `snapshot_at` 升序写，所以这条口径暂不影响现有行为，但实现方不得依赖
        「最后写入即最新」这个更强、不受协议保证的假设。
        """
        raise NotImplementedError


# ── Blob 存储（事件流的字节侧）─────────────────────────────────────────────────


class EventBlobStore(ABC):
    """事件流侧的「二进制 sink」：存取图片等二进制内容，事件库里只留 ref。

    与 `protocols.memory.MemoryBlobStore` **同形但类型无关**（spec §3）。不做成子类型、
    也不共用一个 ABC，理由是两侧语义会各自演进——最明显的是**回收锚点不同**：memory 侧
    是记录 `is_superseded`，event 侧是事件保留策略。今天同形不代表明天同形。

    host 要共用就一个类同时实现两者，注册两次：

        class MyBlobStore(MemoryBlobStore, EventBlobStore): ...

    **ref 前缀取自 `protocols.context.BLOB_REF_PREFIX`**，与 memory 侧同一个常量——
    但**仅此而已**：两侧的 ref 是**两个独立的命名空间**，core 从不比较、也从不拿
    一侧的 ref 去另一侧解。host 用同一实例时两个 ref 恰好相同，那是实现层的巧合，
    不是任何代码可以依赖的前提。

    ⚠️ **回收策略由 host 定，core 不规定。** 事件流里的 ref 能否取回字节，完全取决于
    host 让 event blob 活多久：想让事件流永远可重建，就让回收与事件保留策略对齐
    （例如永不回收，或按事件 TTL）。**共用一个实例时尤其当心**——该实现要同时看两侧的
    引用才能安全回收，仅把 `MemoryProvider.live_blob_refs()`（memory 侧的活引用集合）
    喂给 `FsBlobStore.collect` 之类的 sweep，会把事件流仍需要的字节当孤儿删掉
    （spec §9）。
    """

    @property
    def can_externalize(self) -> bool:
        """本 store 是否真的能存——`NullEventBlobStore` 返回 False。

        调用方据此**先探询、再决定**，而不是调用 put 并捕获 NotImplementedError：
        后者会把「响亮失败」降级成控制流，让真正的接线错误也被静默吞掉。
        基类默认 True，既有实现无需改动。
        """
        return True

    @abstractmethod
    async def put(self, data: bytes, media_type: str, ctx: "ProviderContext") -> str:
        """存字节，返回 ref。必须**内容寻址且幂等**：同样的 data 返回同样的 ref。

        这同时给到三件事：写入端去重、重放安全、以及 rehydrate 字节稳定——同一 ref
        每次还原出的 base64 完全一致，prompt cache 前缀不会被打碎。
        """

    @abstractmethod
    async def get(self, ref: str, ctx: "ProviderContext") -> "tuple[bytes, str] | None":
        """取字节。对不存在 / 已回收的 ref 返回 `None`，**不得 raise**。

        blob 过期、宿主换机、GC 误删都会发生，调用方据此降级为文本占位，
        绝不因取图失败中断 loop。
        """


class NullEventBlobStore(EventBlobStore):
    """未注册 `EventBlobStore` 时的默认实现。

    `put` 刻意抛错而不是静默产出假 ref：调用方（`core.content`）先探询
    `can_externalize` 决定是否外部化，**不**捕获这里的 NotImplementedError——
    它仍是接线错误的响亮信号。
    """

    @property
    def can_externalize(self) -> bool:
        return False

    async def put(self, data: bytes, media_type: str, ctx: "ProviderContext") -> str:
        raise NotImplementedError(
            "No EventBlobStore registered; register one via "
            "ProviderRegistry.register_event_blob_store() before externalizing content."
        )

    async def get(self, ref: str, ctx: "ProviderContext") -> "tuple[bytes, str] | None":
        return None
