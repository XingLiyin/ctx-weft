"""Event 基类 + V1 冻结的事件类型常量。

详见设计文档 §9.2-§9.7。

V1 冻结的事件类型常量在 EVENT_TYPES 中——业务代码不允许发未在此列表的类型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ctx_weft.core.state.models import TaskStatus


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


# 事件类型 → 它蕴含的 task 状态。投影 / 崩溃恢复的唯一真相，
# 由 core reducer、Postgres 投影、前端 SSE 翻译三处共用（此前各维护一份）。
# 键是 EventType（StrEnum），故用裸字符串 `ev.type` 查表同样命中。
TASK_STATUS_BY_EVENT: dict[EventType, "TaskStatus"] = {
    EventType.TASK_STARTED: "ACTIVE",
    EventType.TASK_SUSPENDED: "SUSPENDED",
    EventType.TASK_FINISHED: "FINISHED",
    EventType.TASK_FAILED: "FAILED",
    EventType.TASK_CANCELED: "CANCELED",
    EventType.TASK_RESUMED: "ACTIVE",
    EventType.TASK_REQUEUED: "PENDING",
}
