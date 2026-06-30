"""Core state dataclasses：TaskSettings / Session / Task / Agent / LoopGuard。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from ctx_weft.protocols import LoopConfig, MemoryConfig


# ── TaskSettings ──────────────────────────────────────────────────────────────


@dataclass
class NormalTaskSettings:
    """Settings for a regular reasoning task.

    Created by delegate_task / delegate_plan / replan control tools, or supplied
    as initial_task_settings to start_session() / create_session().
    """

    skill_name: str = ""
    use_subagent: bool = False
    subagent_template: str = ""
    inherit_memory: bool = True
    purpose: str = "act"
    # Transient accumulator: written by submit_* control tools, cleared by SuspendStep.
    spawn_titles: list[str] = field(default_factory=list)


@dataclass
class CompactTaskSettings:
    """Settings for a legacy memory-compact sub-task (initial_step='compact').

    No longer created at runtime (compact now runs as an inline step). Retained for
    deserializing old persisted event streams.
    compact_scope_task_id is NOT stored here — Task.parent_task_id carries it.
    """

    compact_scope_agent_id: str = ""
    keep_last: int = 6
    subagent_template: str = "memory-compactor"
    layer: str = "agent"  # spec/06 §7：'agent'=折叠派发日志 / 'task'=折叠 task 对话


@dataclass
class MetadataFillerTaskSettings:
    """Settings for a legacy metadata-filler daemon task.

    No longer created at runtime (recognize_intent now runs as a background
    coroutine). Retained for deserializing old persisted event streams.
    """

    target_task_id: str = ""
    is_daemon: bool = True


TaskSettings = NormalTaskSettings | CompactTaskSettings | MetadataFillerTaskSettings


def deserialize_settings(d: dict | None) -> TaskSettings:
    """Reconstruct a typed TaskSettings from a raw dict (as stored in events)."""
    if not d:
        return NormalTaskSettings()
    d = dict(d)
    t = d.pop("_type", "NormalTaskSettings")
    if t == "CompactTaskSettings":
        known = {k: v for k, v in d.items() if k in CompactTaskSettings.__dataclass_fields__}
        return CompactTaskSettings(**known)
    if t == "MetadataFillerTaskSettings":
        known = {k: v for k, v in d.items() if k in MetadataFillerTaskSettings.__dataclass_fields__}
        return MetadataFillerTaskSettings(**known)
    known = {k: v for k, v in d.items() if k in NormalTaskSettings.__dataclass_fields__}
    return NormalTaskSettings(**known)


# ── Status types ──────────────────────────────────────────────────────────────


SessionStatus = Literal[
    "QUEUED",
    "RUNNING",
    "INTERRUPTED",
    "SUCCEEDED",
    "FAILED",
    "TIMEOUT",
    "CANCELED",
    "PAUSED_HITL",
    "PAUSED",
]

TaskStatus = Literal[
    "PENDING",
    "ACTIVE",
    "SUSPENDED",
    "TO_BE_OBSERVED",
    "FINISHED",
    "FAILED",
    "CANCELED",
]

AgentStatus = Literal[
    "IDLE",
    "RUNNING",
    "WAITING",
    "FINISHED",
    "FAILED",
]

# How the actor's plain-text (no tool call) turn is handled:
#   "interactive" → pause and wait for a user message (HITL input cold park)
#   "auto"        → autonomous; the actor must call finish_task to finish
TaskInteractionMode = Literal["interactive", "auto"]


# ── LoopGuard ─────────────────────────────────────────────────────────────────


@dataclass
class LoopGuard:
    """Agent 运行时计数与测量值（mutable，每轮可能更新）。"""

    turns_used: int = 0
    context_tokens: int = 0
    context_message_count: int = 0
    context_limit: int = 180_000


# ── Session ───────────────────────────────────────────────────────────────────


@dataclass
class Session:
    """会话容器。"""

    id: str
    user_prompt: str
    status: SessionStatus
    goal: str = ""
    tenant_id: str = "default"
    root_agent_id: str | None = None

    token_budget: int = 200_000
    token_used: int = 0
    context_limit: int = 180_000
    max_concurrent_tasks: int = 8
    max_concurrent_agents: int = 4
    failure_counter: int = 0
    failure_threshold: int = 3

    llm_provider: str | None = None
    llm_model: str | None = None

    config: dict[str, Any] = field(default_factory=dict)
    runtime_summary: dict[str, Any] = field(default_factory=dict)

    created_at: datetime | None = None
    updated_at: datetime | None = None
    finished_at: datetime | None = None


# ── Task ──────────────────────────────────────────────────────────────────────


@dataclass
class Task:
    """会话内的最小工作单元。"""

    id: str
    session_id: str
    status: TaskStatus
    tenant_id: str = "default"

    assigned_agent_id: str | None = None
    creator_agent_id: str | None = None
    parent_task_id: str | None = None

    dag_deps: list[str] = field(default_factory=list)
    tracking_task_ids: list[str] = field(default_factory=list)

    title: str = ""
    description: str = ""
    user_prompt: str | None = None
    # 首次执行时的原始 user_prompt 快照；reopen 重写 user_prompt 时以此为 base，
    # 避免多轮 reopen 把"上轮产出/修订提示"反复累加进 prompt。None = 尚未快照。
    original_user_prompt: str | None = None
    user_prompt_in_memory: bool = False
    settings: TaskSettings = field(default_factory=NormalTaskSettings)
    # 纯文本(无 tool call)turn 的处理方式：interactive=暂停等用户 / auto=自治需调 finish_task。
    # root task 由 session_manager 设为 interactive；委派子任务默认 auto（delegate_task/plan 可显式置 interactive）。
    interaction_mode: TaskInteractionMode = "auto"
    outputs: Any | None = None
    process_report: str | None = None
    # 何时设置 process_report（= 上一轮 observe 产出反馈的时刻，落在该 attempt 之后、下一 attempt 之前）。
    # 装配时据此把 "## Current Progress" 按时间戳归并到正确位置（见 composer._progress_history_block）。
    process_report_at: datetime | None = None

    retry_count: int = 0
    max_retries: int = 3
    timeout_ms: int = 60_000

    compensation: dict[str, Any] | None = None
    priority: int = 5
    error: str | None = None
    error_code: str | None = None
    actor_done: bool = False
    # observe 裁决（三态）：success|retry|fail。retry 置 status=PENDING 重排（机械退出也归 retry）。
    observer_outcome: str | None = None
    # observer 产出的整段综合总结（执行历程+结果）→ finish 对 tool 槽（spec 2026-06-30）。
    task_summary: str | None = None
    # 发起本任务的 parent delegate_task/delegate_plan 的 tool_call_id（spec/06 §5）。
    # finalize 据此把 output+report 作为 TASK_DISPATCH_RESULT 回填 parent agent 层、按它配对。
    origin_tool_call_id: str | None = None

    created_at: datetime | None = None
    updated_at: datetime | None = None
    finished_at: datetime | None = None


# ── Agent ─────────────────────────────────────────────────────────────────────


@dataclass
class Agent:
    """Agent 实例。"""

    id: str
    session_id: str
    template_id: str
    template_version: str
    status: AgentStatus
    tenant_id: str = "default"

    parent_agent_id: str | None = None
    spawn_depth: int = 0

    bound_capability_ids: list[str] = field(default_factory=list)
    active_task_id: str | None = None

    loop_guard: LoopGuard = field(default_factory=LoopGuard)

    memory_config: MemoryConfig = field(default_factory=MemoryConfig)
    loop_config: LoopConfig = field(default_factory=LoopConfig)

    tracking_task_ids: list[str] = field(default_factory=list)
    fetched_tracking_ids: set[str] = field(default_factory=set)

    runtime: dict[str, Any] = field(default_factory=dict)

    created_at: datetime | None = None
    updated_at: datetime | None = None
