"""Core state dataclasses：TaskSettings / Session / Task / Agent / LoopGuard。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from ctx_weft.protocols import LoopConfig, MemoryConfig

from ctx_weft.core.utils import now_utc

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


# ── HITL ──────────────────────────────────────────────────────────────────────

# 三种等待形态（spec 2026-07-05，替代旧 kind + capability_id sentinel 拼判）：
#   approval — 审批门控：放行/拒绝一次工具调用（HumanConfirmationAuthorizer 触发）
#   question — ask_user 结构化提问，答复回灌 LLM
#   wait     — act 纯文本暂停 / 软打断（wait_for_user 冷 park）
HitlForm = Literal["approval", "question", "wait"]
HitlStatus = Literal["pending", "accepted", "rejected", "cancelled"]


@dataclass
class HitlRequest:
    """一次 HITL 请求（含其解析结果）。内存态与事件回放投影共用的单一实体。

    form 决定语义与应答形态：approval 用 approve/reject；question/wait 用 answer/reject。
    host 据 form 决定 UI（批准/拒绝按钮 vs 答题输入框 vs 普通输入框）。
    """

    id: str                                       # 全局唯一，即 hitl_id
    form: HitlForm
    session_id: str
    task_id: str
    agent_id: str = ""
    capability_id: str = ""                       # approval: 被门控的工具；question: 触发提问的工具；wait: 保留 sentinel 值仅作信息
    tool_call_id: str = ""                        # 发起本次调用的 LLM tool_call id（短路门控的键）
    arguments: dict[str, Any] = field(default_factory=dict)
    question: str = ""                            # 展示给人类的问题（approval / wait 用）
    context: str = ""                             # wait 形态的来源（plain_text / interrupt / interrupt:edit）
    questions: list[dict[str, Any]] = field(default_factory=list)  # ask_user 的结构化批量问题（含 options/multi_select）
    status: HitlStatus = "pending"
    # 解析载荷
    # 人类附带的内容：答复 / 拒绝理由 / 备注。多模态回复（含图片）走同一字段。
    message: "str | list[ContentPart]" = ""
    modified_arguments: dict[str, Any] | None = None  # approval form：改写后的工具参数（暂仅记录，不生效）
    created_at: datetime = field(default_factory=now_utc)
    resolved_at: datetime | None = None
    # resume-time LLM 覆盖：冷应答触发 session resume 时用的当前所选模型（host 据 entry 传入），
    # 仅供本次 cold-resolve 转发给 recover_session，不入事件、不持久化。
    resume_llm_account: str | None = None
    resume_llm_model: str | None = None

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"
