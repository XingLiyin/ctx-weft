"""Core state dataclasses：TaskSettings / Session / Task / Agent / LoopGuard。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from ctx_weft.protocols import LoopConfig, MemoryConfig

if TYPE_CHECKING:
    from ctx_weft.protocols import ContentPart


# ── TaskSettings ──────────────────────────────────────────────────────────────


@dataclass
class NormalTaskSettings:
    """Settings for a regular reasoning task.

    Created by delegate_task / delegate_plan control tools, or supplied
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
    "RUNNING",          # 有 task 在跑
    "WAITING",          # 停着，但正常——都在等人 / 等外部输入
    "INTERRUPTED",      # 停着，异常——系统故障，等 /resume（非终态）
    "SUCCEEDED",
    "FAILED",
    "CANCELED",
]
# 值域 == `core.orchestrator.session_state` 状态机的可达状态。
# 已删除：`QUEUED` / `TIMEOUT`（core 从未赋值）；`PAUSED` / `PAUSED_HITL`
# （两者的差别是「前端要不要出面板」，那是 `HitlOpened.delivery` 的性质，不是会话状态，
# 已合并成 `WAITING`）。存量日志里的旧值由 `core.control.reducers` 折叠，见
# `docs/upgrade/2026-09-02-session-status-ownership.md`。

TaskStatus = Literal[
    "PENDING",
    "ACTIVE",
    "SUSPENDED",         # 等子任务完成——**只剩这一个语义**
    "AWAITING_HUMAN",    # 被 HITL 挂起，需要人来解决
    "INTERRUPTED",       # 被外部打断（LLM outage / run 崩溃），等 /resume
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
    reserved_output_tokens: int = 8192


# ── Session ───────────────────────────────────────────────────────────────────


@dataclass
class Session:
    """会话容器。"""

    id: str
    # 多模态：保 ref 形态（裁定 2026-08-27）。不拍扁——否则事件流重放不出
    # 「曾有一张图」；也不内联字节——见 content_to_event_jsonable（dual-blob-store §6）。
    user_prompt: "str | list[ContentPart]"
    status: SessionStatus
    goal: str = ""
    tenant_id: str = "default"
    root_agent_id: str | None = None

    token_budget: int = 200_000
    token_used: int = 0
    context_limit: int = 180_000
    reserved_output_tokens: int = 8192
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
    user_prompt: "str | list[ContentPart] | None" = None
    # 首次执行时的原始 user_prompt 快照；reopen 重写 user_prompt 时以此为 base，
    # 避免多轮 reopen 把"上轮产出/修订提示"反复累加进 prompt。None = 尚未快照。
    original_user_prompt: "str | list[ContentPart] | None" = None
    # 事件侧的 prompt 形态（event ref，`content_to_event_jsonable` 的产物）。
    # **纯瞬态**：不进 TaskProjection / 快照——恢复时由 Runtime._restore_task_prompts
    # 从事件 payload 直接重填，那本来就是这份数据的原样形态，没必要再持久化第二遍。
    # reopen_task 据此发 TASK_REQUEUED，零 blob IO：reopen 只追加文本，不可能引入新图。
    user_prompt_event_jsonable: "str | list[dict] | None" = None
    original_user_prompt_event_jsonable: "str | list[dict] | None" = None
    user_prompt_in_memory: bool = False
    settings: TaskSettings = field(default_factory=NormalTaskSettings)
    # 纯文本(无 tool call)turn 的处理方式：interactive=暂停等用户 / auto=自治需调 finish_task。
    # root task 由 session_manager 设为 interactive；委派子任务默认 auto（delegate_task/plan 可显式置 interactive）。
    interaction_mode: TaskInteractionMode = "auto"
    outputs: Any | None = None
    process_report: str | None = None
    # 何时设置 process_report（= 上一轮 observe 产出反馈的时刻，落在该 attempt 之后、下一 attempt 之前）。
    # process_report/process_report_at 现仅服务终态 finish 对 / 子任务 bubble；retry 进度已改由 task 层 TASK_COMPACT_SUMMARY 段摘要承载（spec 2026-07-01 §3.7）。
    process_report_at: datetime | None = None
    # observer 给「下一次 act attempt」的一次性转向：next_step_hint + success-without-outputs
    # 护栏文案（见 control_capability.report_task_outcome）。
    # **刻意不并进 process_report/act_recap**：后者是永久记录（→ TASK_COMPACT_SUMMARY 段摘要、
    # finish 对 assistant 槽），一次性指令混进去会在任务完成后仍留在历史里，跨 task 召回时表现为
    # 「一句已经过期的 Next Step Hint 夹在已完成任务的对话中间」。本字段改由 act_guidance 渲染进
    # 每轮 guidance（只发不入 memory、priority 1 受保护），随任务终结自然消失。
    next_step_hint: str | None = None

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
    # 发起本任务的派发工具的**限定名**，finalize 铸派发框时用作 tool_calls[].name（保真）。
    # delegate_task → 真名 control__delegate_task（actor 确实调过）；delegate_plan 子 → None
    # （actor 只调过一次 delegate_plan、无 per-child 调用）→ finalize 回退 start_task 叙事名。
    # 与 origin_tool_call_id 同走 TASK_CREATED payload → TaskView → 恢复链（2026-08-19 补，
    # 此前是纯瞬态、重放后为 None）；存量事件仍无此字段 → 重建后为 None → 回退 start_task。
    origin_tool_name: str | None = None

    created_at: datetime | None = None
    updated_at: datetime | None = None
    # task manager 真正启动本 task（本 attempt）的时刻。派发框（start_task/dispatch 对）以此为锚，
    # 排在子 body 之前、反映真实启动顺序（避免 created_at 的兄弟碰撞/乱序）。每次 run 重置。
    started_at: datetime | None = None
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
