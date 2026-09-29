"""ControlCapabilityProvider：内置控制能力。

工具定义用 @control_tool 装饰器 + Annotated 类型注解声明，
input_schema 由 extract_schema() 从函数签名自动提取（自动跳过 ctx 参数）。

工具函数体通过注入的 ControlContext 直接操作 Task / TaskManager / Session，
不再通过 metadata 把控制信号传递给 ActStep。
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any

from ctx_weft.core.capabilities.schema import extract_schema
from ctx_weft.core.utils.clock import now_utc
from ctx_weft.core.utils.headings import SUBTASKS_HEADING
from ctx_weft.core.utils.ids import generate_id
from ctx_weft.core.utils.verdict import (
    VERDICT_CONTINUE,
    VERDICT_FAIL,
    VERDICT_SUCCESS,
    normalize_verdict,
)
from ctx_weft.core.utils.task_ref import task_ref
from ctx_weft.core.models.task import NormalTaskSettings
from ctx_weft.protocols.capability import (
    CapabilityEvent,
    RecoveryPolicy,
    CapabilityProviderInfo,
    Purpose,
    SessionScopedCapabilityProvider,
    ToolCapability,
    ToolCapabilityProvider,
    qualify,
)
from ctx_weft.protocols.context import ProviderContext

if TYPE_CHECKING:
    from ctx_weft.core.orchestrator.task.manager import TaskManager
    from ctx_weft.core.models.session import Session
    from ctx_weft.core.models.task import Task

logger = logging.getLogger(__name__)

PROVIDER_NAME = "control"

# runtime-injected context parameter — excluded from LLM schema and from arguments filtering
_SKIP: frozenset[str] = frozenset({"ctx"})

# Qualified (LLM-facing) names for the built-in control tools. Use these anywhere
# a control tool is named to the LLM (prompts, docstrings shown as descriptions).
FINISH_TASK_NAME = qualify(f"{PROVIDER_NAME}:finish_task")
DELEGATE_TASK_NAME = qualify(f"{PROVIDER_NAME}:delegate_task")
DELEGATE_PLAN_NAME = qualify(f"{PROVIDER_NAME}:delegate_plan")
ASK_USER_NAME = qualify(f"{PROVIDER_NAME}:ask_user")
REPORT_TASK_OUTCOME_NAME = qualify(f"{PROVIDER_NAME}:report_task_outcome")
#: 只摘要那一档后台 observe 的 terminal tool（2026-09-28 恢复为**真工具**）。
#:
#: 它 2026-09-22 曾被合并进 `report_task_outcome`（当时的理由：签名是真子集，而它存在的唯一
#: 理由「Zero state write」已由 `ControlContext.readonly` 取代）。那次合并留下一个活的缺陷：
#: 不判决的那几个边界，cue 让模型调一个**已经不存在**的 `collect_process_report`，同一段话又
#: 说「不要判 success/retry/fail」，而桌面上唯一的工具把 `task_status` 列为**必填**——指令自
#: 相矛盾，模型只能违背其一（不调工具 → 段保 raw；或硬填一个没人看的 status）。
#:
#: 拆回两个工具之后，「这个边界不判」成为**工具面的事实**：摘要档的能力面里压根没有判决工具，
#: 不需要任何叮嘱。两档由 purpose 区分（`background_recap` / `background_observe`）。
COLLECT_PROCESS_REPORT_NAME = qualify(f"{PROVIDER_NAME}:collect_process_report")
UPDATE_TASK_METADATA_NAME = qualify(f"{PROVIDER_NAME}:update_task_metadata")

#: `act_recap` 的字段契约，两个 observe terminal tool **逐字共用**一份。
#:
#: 这段文字是「字段是什么」的唯一真相源——cue 不再复述它（2026-09-28）。scope 规则放在这里而
#: 不放 cue，是因为它**恒定**、不随边界变，而且模型读字段时它就在眼前。
_ACT_RECAP_DESC = (
    "An honest recap of what this segment's act did: what you changed or produced, which tools "
    "you called, and whether anything failed. First person, faithful to what actually ran. "
    "Scope = the work the actor newly did after the last `## Progress So Far` or the last user "
    "message in the conversation, whichever is later (on a first observation, start from the "
    "beginning of the task); do not restate anything before that point. Written to memory, and "
    "reused as the next round's `## Progress So Far` when the task continues."
)

# delegate_plan 的 actor-visible ack 及 gateway 配对 tool result 内容。
_PLAN_DISPATCH_ACK = ("Plan created. Its sub-tasks will now be started one by one "
                      "via start_task.")

# `ask_user` 撞上无人值守（`UnattendedHitl`）时回灌给 actor 的工具结果。
#
# 文本住在这里而不是 gateway：这是**给 ask_user 的调用者看的**上下文化答复，与该工具
# 自身的语义（「我需要一个人的决定」）配套；gateway 只负责在唯一的登记入口被守卫挡下时
# 把它取出来。绝不能让 `UnattendedHitl` 逸出到 agent loop——那会把「没人可问」变成一次
# run 失败，而 actor 该收到的是一个说得清楚的结果。
ASK_USER_UNATTENDED_RESULT = (
    "[No human available: this task runs unattended in the background, so nobody can "
    "answer your question. Decide for yourself based on the information you already "
    "have, or call control__finish_task and state clearly what blocked you.]"
)


# ── ControlMetaKey ────────────────────────────────────────────────────────────


class ControlMetaKey:
    """Step 间传递的 metadata key。仅保留仍需通过 metadata 传递的信号。"""
    CONTROL_ACTION = "control_action"
    HITL_REQUESTED = "hitl_requested"

    #: `report_task_outcome` 的结构化回传（2026-09-22）。`ControlResult.content` 是给
    #: LLM 看的确认话术，形状不稳定、不该被解析；拿不到 task 字段的调用方（readonly
    #: 的后台 observe）从这几个 key 取干净的值。
    OBSERVER_OUTCOME = "observer_outcome"
    OBSERVER_ACT_RECAP = "observer_act_recap"
    OBSERVER_TASK_SUMMARY = "observer_task_summary"
    OBSERVER_NEXT_STEP_HINT = "observer_next_step_hint"
    OBSERVER_FAILURE_REASON = "observer_failure_reason"


# ── ControlContext ─────────────────────────────────────────────────────────────


@dataclass
class ControlContext:
    """运行时注入到 control tool 函数体的执行上下文（不暴露给 LLM）。"""
    session_id: str
    task_id: str
    agent_id: str
    task: "Task | None"
    task_manager: "TaskManager | None"
    session: "Session | None"
    tool_call_id: str = ""  # 发起本次调用的 LLM tool_call id（spec/06 §5，委派回填用）
    #: 只读模式：工具可以**读** `task`，但一个字段都不许写（2026-09-22）。
    #:
    #: 为什么需要它：`task` 是从 `TaskManager._tasks` 取的**活对象**，不是快照。后台
    #: observe 是 fire-and-forget、常在主 run 收尾之后才跑到，它若调用写 task 的工具
    #: 就会隔着时间改主线程状态——而它的判决还要先过带外入口的仲裁（人可能已经开口
    #: 重排了这个 task）。判决结果改走 `ControlResult.metadata` 回传，由 TaskManager
    #: 在仲裁通过后统一写，「task 状态的唯一改写者」因此不破。
    #:
    #: 由 `ProviderContext.extra["control_readonly"]` 传入，见 `_handle`。
    readonly: bool = False


# ── ControlResult ─────────────────────────────────────────────────────────────


@dataclass
class ControlResult:
    """control tool 函数的返回值。

    content  → 给 LLM 看的确认文本（拼入 tool result message）
    metadata → 传递给 Step 的附加信号（可选）
    """
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


# ── @control_tool 装饰器 ──────────────────────────────────────────────────────

# name → (ToolCapability, handler_fn)
_CONTROL_TOOLS: dict[str, tuple[ToolCapability, Callable[..., ControlResult]]] = {}


def control_tool(*, purposes: list[Purpose], input_schema: dict[str, Any] | None = None):
    """装饰器：声明控制工具的 purposes + 自动提取 input_schema，注册到 _CONTROL_TOOLS。

    input_schema 给定时用它覆盖自动提取——extract_schema 只能生成扁平 schema
    （list→array 无 items、dict→object 无 properties），需要嵌套对象/数组的工具
    （如 ask_user 的 questions）须手写 schema 覆盖。
    """
    def decorator(fn: Callable[..., ControlResult]) -> Callable[..., ControlResult]:
        first_line = (fn.__doc__ or "").strip().split("\n")[0].strip()
        cap = ToolCapability(
            id=f"{PROVIDER_NAME}:{fn.__name__}",
            name=fn.__name__,
            kind="tool",
            purposes=list(purposes),
            description=first_line,
            input_schema=input_schema or extract_schema(fn),  # extract_schema 默认排除 "ctx"
            side_effects=False,
            # spec: tool-operations「控制工具单独核验」——控制工具的副作用**全在 core
            # 自己手里**，逐个核过都是同身份幂等的状态迁移，所以崩溃后按 idempotent
            # 直接重跑，不必问重跑授权（问也没人答得了：外部系统里根本没有对应物）：
            #   finish_task / report_task_outcome  同身份幂等的 task 状态迁移
            #   ask_user                           复用既有 HITL 请求（决定缓存门控）
            #   delegate_task / delegate_plan      经 gateway 的 completed 短路防双建
            # 这是**声明**而不是 gateway 里的一条按名字前缀的特判：判据本来就是「这个
            # 工具重跑安不安全」，那正是本字段的含义；写成特判则两处都要记得同步。
            recovery_policy=RecoveryPolicy.IDEMPOTENT,
        )
        _CONTROL_TOOLS[fn.__name__] = (cap, fn)
        return fn
    return decorator


# ── 工具定义 ──────────────────────────────────────────────────────────────────


@control_tool(purposes=["act"])
def delegate_task(
    title: Annotated[str, "Short imperative title for the sub-task (≤20 chars)"],
    description: Annotated[
        str,
        "WHAT the sub-task must achieve — its goal/content only. No HOW, no tool names, and do NOT "
        "restate dispatch/orchestration choices such as 'use subagent' / 'inherit memory' / which "
        "skill (those go in the use_subagent / inherit_memory / skill_name params). This text becomes "
        "the sub-task's own `## Current Task`, so anything off-goal will mislead it when it runs.",
    ] = "",
    task_prompt: Annotated[str, "Detailed prompt extracted from the user request for this task"] = "",
    skill_name: Annotated[str, "Skill to assign to the task, or empty if none"] = "",
    use_subagent: Annotated[bool, "True if the task should run in a dedicated sub-agent"] = False,
    subagent_template: Annotated[str, "Template id for the sub-agent; empty = system default"] = "",
    inherit_memory: Annotated[bool, "True if the sub-agent should inherit session memory"] = True,
    *,
    ctx: ControlContext = None,
) -> ControlResult:
    """Delegate ONE new sub-task to run separately; the current task suspends until its sub-tasks finish. To finish your OWN task instead, use control__finish_task."""
    from ctx_weft.core.models.task import Task as TaskModel

    if ctx is None or ctx.task_manager is None or ctx.task is None:
        return ControlResult(content=f"Sub-task '{title}' scheduled.")

    child = TaskModel(
        id=generate_id("tsk"),
        session_id=ctx.session_id,
        status="PENDING",
        tenant_id=ctx.task.tenant_id,
        parent_task_id=ctx.task_id,
        creator_agent_id=ctx.agent_id,
        title=title,
        description=description,
        user_prompt=task_prompt or description,
        origin_tool_call_id=ctx.tool_call_id or None,
        origin_tool_name=DELEGATE_TASK_NAME,  # 保真：actor 确实调了 delegate_task → finalize 铸框用真名
        # 无人值守**继承**，不给 LLM 旋钮（schema 里没有这个参数）：「有没有人在」是
        # 作业被怎么起起来的事实，不是 actor 可以自行宣布的。它同时决定子任务的纯文本
        # 回合要不要 park——父任务有人在，子任务的话也有人听。
        unattended=ctx.task.unattended,
        # 交互口同样继承：子任务的产出流向同一个对端。
        port_key=ctx.task.port_key,
        settings=NormalTaskSettings(
            skill_name=skill_name,
            use_subagent=bool(use_subagent),
            subagent_template=subagent_template,
            inherit_memory=bool(inherit_memory),
        ),
        created_at=now_utc(),
    )
    ctx.task_manager.stage_task(child, parent_task_id=ctx.task_id)

    if isinstance(ctx.task.settings, NormalTaskSettings):
        ctx.task.settings.spawn_titles.append(title)

    ctx.task.suspend_requested = True     # 路由意图；task 落 SUSPENDED 归 TaskManager
    ctx.task.actor_done = True
    # spec: task-handoff——回执带标题 + id：id 是模型后续审核/引用的稳定句柄，标题让
    # 它对得上自己刚派的是哪一个。
    return ControlResult(content=f"Sub-task {task_ref(child)} scheduled.")


@control_tool(purposes=["act"])
def delegate_plan(
    tasks: Annotated[
        list,
        (
            "Ordered list of task specs. Each item: "
            "title (str, the sub-task's goal only — no dispatch flags), "
            "description (str, WHAT the sub-task must achieve — its goal/content only; no HOW, and do NOT "
            "restate use_subagent/inherit_memory/skill in the text: it becomes the sub-task's own "
            "`## Current Task` and off-goal words mislead it), "
            "task_prompt (str, detailed prompt — goal/content only, same rule as description), "
            "skill_name (str, skill to assign or empty), "
            "use_subagent (bool), subagent_template (str), "
            "inherit_memory (bool, default true), "
            "Tasks run strictly in order and each one starts ONLY if its predecessor succeeded — "
            "if a step fails, the rest are canceled. Put independent work in separate "
            "control__delegate_task calls instead, so one failure does not cancel the others."
        ),
    ],
    *,
    ctx: ControlContext = None,
) -> ControlResult:
    """Delegate SEVERAL ordered sub-tasks in one call (each runs after the previous); the current task suspends until they all finish. For a single sub-task use control__delegate_task; to finish your OWN task, use control__finish_task."""
    from ctx_weft.core.models.task import Task as TaskModel

    if not isinstance(tasks, list):
        tasks = []

    n = len(tasks)
    if ctx is None or ctx.task_manager is None or ctx.task is None:
        return ControlResult(content=_PLAN_DISPATCH_ACK)

    prepared: list[dict] = [spec for spec in tasks if isinstance(spec, dict)]

    titles: list[str] = []
    child_refs: list[str] = []
    prev_ids: list[str] = []
    for spec in prepared:
        title = spec.get("title", "subtask")
        child = TaskModel(
            id=generate_id("tsk"),
            session_id=ctx.session_id,
            status="PENDING",
            tenant_id=ctx.task.tenant_id,
            parent_task_id=ctx.task_id,
            creator_agent_id=ctx.agent_id,
            title=title,
            description=spec.get("description", ""),
            user_prompt=spec.get("task_prompt") or spec.get("description", ""),
            origin_tool_call_id=generate_id("tcall"),
            # 同 delegate_task：继承而非声明——「有没有人在」「对端是谁」都是作业被
            # 怎么起起来的事实。
            unattended=ctx.task.unattended,
            port_key=ctx.task.port_key,
            settings=NormalTaskSettings(
                skill_name=spec.get("skill_name", ""),
                use_subagent=bool(spec.get("use_subagent", False)),
                subagent_template=spec.get("subagent_template", ""),
                inherit_memory=bool(spec.get("inherit_memory", True)),
            ),
            created_at=now_utc(),
        )
        ctx.task_manager.stage_task(
            child,
            parent_task_id=ctx.task_id,
            blocked_by=[prev_ids[-1]] if prev_ids else None,
        )
        prev_ids.append(child.id)
        child_refs.append(task_ref(child))
        titles.append(title)

    if isinstance(ctx.task.settings, NormalTaskSettings):
        ctx.task.settings.spawn_titles = titles
    ctx.task.suspend_requested = True     # 路由意图；task 落 SUSPENDED 归 TaskManager
    ctx.task.actor_done = True
    # spec: task-handoff——回执逐条给「标题 + id」，而不是一串裸 id：只给 id 列表的话
    # 模型得靠位置去对应自己刚传的 spec 顺序，同名任务更是无从分辨。
    # 句子本体从 `_PLAN_DISPATCH_ACK` 拼，不重打字面量（重打两处必然漂）。
    listing = "; ".join(f"{i}. {ref}" for i, ref in enumerate(child_refs, start=1))
    return ControlResult(content=(
        f"{_PLAN_DISPATCH_ACK.rstrip('.')}, in this order: {listing}."
    ))


@control_tool(purposes=["act"])
def finish_task(
    deliverables_summary: Annotated[
        str,
        "OPTIONAL. A brief recap of the concrete deliverables of this task (e.g. the key files "
        "changed / artifacts produced), for the observer and whoever delegated this task. This "
        "is NOT your reply to the user — write your final reply as your normal message text in "
        "this same turn; that message is what the user sees AND the deliverable handed off. "
        "Leave this empty when there is nothing concrete to itemize.",
    ] = "",
    *,
    ctx: ControlContext = None,
) -> ControlResult:
    """Finish the CURRENT task and hand off to review — call ONLY when the task goal is fully achieved; if work remains, keep working instead. Write your final reply to the user as your normal message text in this same turn — that message IS the reply shown to the user and the deliverable handed off; this tool just ends the task. The optional `deliverables_summary` is a brief recap of concrete artifacts for the reviewer, NOT your answer. Use when YOUR work is done — NOT to create new work (use control__delegate_task / control__delegate_plan for that)."""
    if ctx is not None and ctx.task is not None:
        # task.outputs 由 ActStep 收尾时合成（收尾回合正文 + deliverables_summary，spec 2026-07-01）；
        # 此处不写 outputs。actor_done 让 act 循环退出；不置 SUSPENDED → next_step=observe。
        ctx.task.actor_done = True
    return ControlResult(content="Task finished.")


@control_tool(purposes=["observe", "background_observe"])
def report_task_outcome(
    task_status: Annotated[
        str,
        "Outcome of the current task — one of 'success' | 'continue' | 'fail'. "
        "'success': the task's goal is achieved and the task ends here. "
        "'continue': the task is NOT over — it needs another actor turn, or it is waiting on the user. "
        "IF THE ACTOR'S MESSAGE ASKS THE USER FOR ANYTHING — a question, missing information, a choice "
        "between options, a confirmation, or an action only the user can take — THE ANSWER IS ALWAYS "
        "'continue', never 'success' and never 'fail'. That holds even when the message is polished and "
        "everything the actor could do alone is done: a turn that ends by handing the floor back is not a "
        "delivered task. 'continue' carries no criticism of the actor; it only says the task has not ended. "
        # 「别想太多、尽快调」不写在这里（2026-09-28 删）：那是关于**这次调用**的指令，归尾部
        # cue（`composer._JUDGMENT_ASK` 有一句），不是字段语义。此前两处各一句，同一个 prompt
        # 里出现两遍。
        "'fail': the goal cannot be achieved as stated and should NOT be attempted again.",
    ],
    act_recap: Annotated[str, _ACT_RECAP_DESC],
    task_summary: Annotated[
        str,
        "Required when task_status is 'success' or 'fail': a CONCISE process report of the WHOLE task — "
        "the important steps taken and lessons/experience, incorporating any sub-task results. "
        "Keep it high-signal, NOT a verbose blow-by-blow. This is NOT the final output: the final "
        "deliverable shown to the user is the actor's closing message text (the same turn that calls "
        "finish_task), not any tool argument. Leave empty for 'continue'.",
    ] = "",
    task_failure_reason: Annotated[
        str,
        "Required when task_status is 'fail'. For 'continue', fill it ONLY when something actually blocked "
        "or fell short this round — LEAVE IT EMPTY when the actor is simply waiting on the user, or the work "
        "is merely unfinished. Do not invent a failure in order to fill this field. "
        "For 'fail': explain specifically what went wrong — which step failed, what error or unexpected "
        "result was encountered, and the root cause. "
        "For a genuinely blocked 'continue': state what concretely blocked this attempt; if the attempt limit "
        "is later hit, this text is what the user sees as the failure reason. Empty for 'success'.",
    ] = "",
    next_step_hint: Annotated[
        str,
        "Optional. If there are obvious risks, blockers, or important concerns the next actor turn should be "
        "aware of, describe them here. Leave empty if nothing notable. "
        # 这里只说「这个字段也承载这件事」。**「你不决定下一步」那条职权规则不写在这里**
        # （2026-09-28 删）：它是 ROLE 的地盘，而 ROLE 已有一份近乎逐字的同款文本——连
        # `## Your sub-tasks` 这个标题名都两边各存一份。字段描述越界写职权规则，与「ROLE 越界
        # 写字段契约」是同一个病的两个方向。
        "This is also where you flag a sub-task whose result does not actually achieve its goal: "
        f"name it (title + id, as listed under '{SUBTASKS_HEADING}') and say what is wrong and "
        "what must be different.",
    ] = "",
    *,
    ctx: ControlContext = None,
) -> ControlResult:
    """Record the review verdict for the current task (success / retry / fail)."""
    task = ctx.task if ctx else None
    # observe 裁决三态。机械退出（max_turns/context_limit）由系统在 ObserveStep 归为 retry。
    # 归一到三态（`retry` 是永久别名，认不出的归 continue——决不归 fail）。见
    # `core.utils.verdict` 的模块 docstring。
    task_status = normalize_verdict(task_status)
    # 一次性转向（只对下一次 attempt 有效）与永久记录（act_recap）分开累积：act_recap 会经
    # process_report → 段摘要 / finish 对进永久记忆，把 hint 拌进去会让它在任务完成后仍留在
    # 历史里（过期的 Next Step Hint）。hint 走 task.next_step_hint → guidance，不入 memory。
    hint = f"Next Step Hint: {next_step_hint}" if next_step_hint else ""

    metadata: dict[str, Any] = {}
    if task is not None:
        # 护栏：没有最终产出就不允许判成功，改判 retry（提示下一轮调 finish_task 收尾）。
        # **只读也跑**：它读的是 `task.outputs`，改的是本地的 task_status/hint，不碰 task。
        if task_status == VERDICT_SUCCESS and not task.outputs:
            task_status = VERDICT_CONTINUE
            _hint = ("The previous round ended without a final output. Review the recap above "
                     "and judge whether this task still needs more work. If it does, continue with the "
                     "necessary tool calls. Once everything required is done, write your final reply to "
                     "the user as your normal message text and then call the `control__finish_task` tool "
                     "to complete the task — your message text is the reply and the deliverable.")
            hint = f"{hint}\n\n{_hint}" if hint else _hint

    # `readonly` 的调用方（后台 observe）一个字段都不写——判决改走下面的 metadata 回传，
    # 由 TaskManager 在带外仲裁通过后统一写。见 `ControlContext.readonly`。
    if task is not None and not ctx.readonly:
        task.process_report = act_recap
        task.next_step_hint = hint or None
        task.task_summary = task_summary
        task.process_report_at = now_utc()
        # **只写判决，不写状态**（Task 4）：三态 verdict 经 FinalizeStep 的 RunOutcome
        # 交给 TaskManager，由处置表决定 task 落 FINISHED / FAILED / PENDING。
        task.observer_outcome = task_status
        task.actor_done = True
        if task_status == VERDICT_SUCCESS:
            task.error = None  # 清掉上一轮暂存的受阻原因，FINISHED 任务不携带 error
        elif task_status == VERDICT_FAIL:
            task.error = task_failure_reason
        else:  # continue
            # 本轮受阻原因暂存 task.error：retry 耗尽降级 fail 时它就是真死因
            # （TaskFailed 的 TASK_FAILED_RETRY_EXHAUSTED 携带）；下一轮判决必然覆盖或清空。
            task.error = task_failure_reason or None

    # 结构化回传（2026-09-22）：`content` 是给 LLM 看的确认话术，形状不稳定也不该被解析。
    # 调用方——尤其是 readonly 的后台路径，它拿不到任何 task 字段——从这里取干净的值。
    # 前台不读 metadata（它读 task 字段），所以这几行对既有路径无影响。
    metadata.update({
        ControlMetaKey.OBSERVER_OUTCOME: task_status,
        ControlMetaKey.OBSERVER_ACT_RECAP: act_recap,
        ControlMetaKey.OBSERVER_TASK_SUMMARY: task_summary,
        ControlMetaKey.OBSERVER_NEXT_STEP_HINT: hint,
        ControlMetaKey.OBSERVER_FAILURE_REASON: task_failure_reason,
    })

    failure_part = f" Failure reason: {task_failure_reason}" if task_status == VERDICT_FAIL and task_failure_reason else ""
    return ControlResult(
        content=f"Assessment recorded: outcome={task_status}.{failure_part} {act_recap}",
        metadata=metadata,
    )


@control_tool(purposes=["background_recap"])
def collect_process_report(
    act_recap: Annotated[str, _ACT_RECAP_DESC],
    task_summary: Annotated[
        str,
        "Fill this only when the trailing prompt says the task has ended (a close-out segment): a "
        "CONCISE process report of the WHOLE task — the important steps taken and lessons/experience, "
        "incorporating any sub-task results. Keep it high-signal, NOT a verbose blow-by-blow. It is "
        "NOT the final output: the deliverable shown to the user is the actor's closing message text. "
        "Leave empty otherwise.",
    ] = "",
    *,
    ctx: ControlContext = None,
) -> ControlResult:
    """Record what this segment did. No verdict — this segment is not being adjudicated."""
    # **不写 task，一个字段都不写**（与 `report_task_outcome` 的分野不止于少一个参数）：这一档
    # 的存在意义就是「这个边界不产判决」，而 `observer_outcome` / `actor_done` / `process_report`
    # 那几个字段全是判决的产物。调用方（`loop.background.recap`）从
    # metadata 取报告，自己决定写进段摘要还是 close report 槽。
    #
    # 也因此不需要 `ctx.readonly` 那道闸——它保护的是「后台隔着时间改主线程 task 状态」，而这里
    # 压根没有写。`ctx` 仍收下：签名与其余控制工具一致，gateway 无条件传它。
    return ControlResult(
        content=f"Process report recorded. {act_recap}",
        metadata={
            ControlMetaKey.OBSERVER_ACT_RECAP: act_recap,
            ControlMetaKey.OBSERVER_TASK_SUMMARY: task_summary,
        },
    )


@control_tool(purposes=["recognize_intent"])
def update_task_metadata(
    title: Annotated[str, "Task title (REQUIRED, must be non-empty): ≤20 chars, start with a verb, "
                          "summarize the core goal. Never pass an empty string — make a best effort even "
                          "if the instruction is short or vague."],
    description: Annotated[str, "Task description (REQUIRED, must be non-empty): ≤80 chars, state the "
                               "outcome to achieve. Never pass an empty string."],
    session_goal: Annotated[
        str,
        "Overall session goal: ≤60 chars. The only optional field. Set it on the first fill or when "
        "the direction changes; leave empty ONLY to keep the existing goal.",
    ] = "",
    *,
    ctx: ControlContext = None,
) -> ControlResult:
    """Set the current task's title and description (and optionally the session goal). Call exactly once, then stop."""
    if ctx is not None and ctx.task is not None:
        # recognize_intent 现直跑在 root task 上：直接写当前 task 的 title/description。
        # 不置 actor_done（会让 root 主 act 循环误判完成；单发步骤本就不读它）。
        if title:
            ctx.task.title = title
        if description:
            ctx.task.description = description
    if ctx is not None and ctx.session is not None and session_goal:
        ctx.session.goal = session_goal
    return ControlResult(content=f"Metadata updated: title={title!r}")


# ask_user 的嵌套结构超出 extract_schema 的扁平表达能力,手写覆盖（见 control_tool）。
_ASK_USER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "description": (
                "One or more questions to ask the human. Each is answered independently; "
                "batch related questions in one call instead of asking one at a time."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question or decision you need from the human.",
                    },
                    "options": {
                        "type": "array",
                        "description": (
                            "Predefined choices the human can pick. Omit or leave empty for a "
                            "free-text-only question; a free-text 'Other' is always offered regardless."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string", "description": "Short choice text shown on the button."},
                                "description": {"type": "string", "description": "What this choice means or its trade-off."},
                                "recommended": {"type": "boolean", "description": "Set on the option you recommend."},
                            },
                            "required": ["label"],
                        },
                    },
                    "multi_select": {
                        "type": "boolean",
                        "description": "Allow the human to select multiple options.",
                        "default": False,
                    },
                },
                "required": ["question"],
            },
        },
    },
    "required": ["questions"],
}


@control_tool(purposes=["act", "observe"], input_schema=_ASK_USER_SCHEMA)
def ask_user(
    questions: Annotated[list, "List of questions to ask the human (see schema)"],
    *,
    ctx: ControlContext = None,
) -> ControlResult:
    """Ask the user one or more questions and wait for their reply — call this whenever you need information, a decision, a clarification, or a choice that only the user can provide; prefer asking over guessing or assuming. Execution pauses until they answer, then resumes from their reply."""
    # 暂停态由 pending HITL 集合推导（spec §7.1），不再由这里直接写 session.status。
    # needs_human 的产出与 park 均在 ControlCapabilityProvider._handle() 完成。
    n = len(questions) if questions else 0
    return ControlResult(
        content=f"Human input requested ({n} question(s))",
        metadata={
            ControlMetaKey.HITL_REQUESTED: True,
            "questions": questions or [],
        },
    )


# ── ControlCapabilityProvider ─────────────────────────────────────────────────


class ControlCapabilityProvider(ToolCapabilityProvider, SessionScopedCapabilityProvider):
    """Exposes task-orchestration control tools to the LLM.

    工具通过 @control_tool 装饰器注册到 _CONTROL_TOOLS；
    _handle() 构建 ControlContext 并注入，工具函数体直接完成实际变动。
    """

    name = PROVIDER_NAME

    def __init__(self) -> None:
        self._sessions: dict[str, tuple["TaskManager", "Session"]] = {}

    def register_session(
        self,
        session_id: str,
        task_manager: "TaskManager",
        session: "Session",
    ) -> None:
        """注册 session 的 TaskManager + Session，供工具函数体使用。"""
        self._sessions[session_id] = (task_manager, session)

    def deregister_session(self, session_id: str) -> None:
        """会话被**逐出内存**时注销（`forget_session` / `purge_session`），防止内存泄漏。

        不是「会话跑完」时（2026-09-08 生命周期改造前是那样）。重新登记由
        `_register_and_drain` 承担：逐出会连 TaskManager 一起摘掉，下一条执行入口重建
        TM 时那里会把本 provider 一并接回来。
        """
        self._sessions.pop(session_id, None)

    async def list(self, ctx: ProviderContext) -> list[ToolCapability]:
        return [cap for cap, _ in _CONTROL_TOOLS.values()]

    def invoke(
        self,
        capability_id: str,
        arguments: dict[str, Any],
        ctx: ProviderContext,
    ) -> AsyncIterator[CapabilityEvent]:
        return self._handle(capability_id, arguments, ctx)

    async def _handle(
        self,
        capability_id: str,
        arguments: dict[str, Any],
        ctx: ProviderContext,
    ) -> AsyncIterator[CapabilityEvent]:
        name = capability_id.split(":")[-1]
        entry = _CONTROL_TOOLS.get(name)
        if entry is None:
            yield CapabilityEvent(
                kind="error",
                payload={
                    "code": "UNKNOWN_CONTROL",
                    "message": f"Unknown control capability: {capability_id}",
                },
            )
            return

        _, fn = entry
        tm = None
        try:
            sig = inspect.signature(fn)
            # 过滤掉 LLM arguments 中 schema 未声明的 key
            valid_keys = set(sig.parameters.keys()) - _SKIP
            filtered = {k: v for k, v in arguments.items() if k in valid_keys}

            # 构建并注入 ControlContext
            if "ctx" in sig.parameters:
                tm, session = self._sessions.get(ctx.session_id, (None, None))
                task = tm.get_task(ctx.task_id or "") if tm and ctx.task_id else None
                filtered["ctx"] = ControlContext(
                    session_id=ctx.session_id,
                    task_id=ctx.task_id or "",
                    agent_id=ctx.agent_id or "",
                    task=task,
                    task_manager=tm,
                    session=session,
                    tool_call_id=ctx.extra.get("tool_call_id", ""),
                    readonly=bool(ctx.extra.get("control_readonly")),
                )

            result: ControlResult = fn(**filtered)
        except Exception as e:
            logger.exception("Control tool %s raised: %s", name, e)
            yield CapabilityEvent(
                kind="error",
                payload={"code": "CONTROL_EXEC_ERROR", "message": str(e)},
            )
            return

        K = ControlMetaKey

        # ask_user：声明「我需要一个人的决定」并立即停——不在此 park、不自己等人。
        # 「答复即结果」：gateway 收到 needs_human 后开等待、拿到答复直接回灌为本次工具结果，
        # 因此本 provider **不需要**实现 HumanResumable（spec §2.3）。
        if result.metadata.get(K.HITL_REQUESTED):
            from ctx_weft.protocols.hitl import HITL_FORM_QUESTION, HitlAsk, ToolResultDelivery
            tool_call_id = (ctx.extra or {}).get("tool_call_id", "")
            yield CapabilityEvent(kind="needs_human", payload={"ask": HitlAsk(
                form=HITL_FORM_QUESTION,
                delivery=ToolResultDelivery(tool_call_id=tool_call_id),
                prompt=result.content,
                fields=list(result.metadata.get("questions") or []),
                subject_id=capability_id,
                reply_as_result=True,
            )})
            return                                     # needs_human 是流的终点

        yield CapabilityEvent(
            kind="result",
            payload={"content": result.content, "metadata": result.metadata},
        )

    async def cancel(self, invocation_id: str, ctx: ProviderContext) -> None:
        pass

    async def describe(self, ctx: ProviderContext) -> CapabilityProviderInfo:
        return CapabilityProviderInfo(
            name=self.name,
            capability_count=len(_CONTROL_TOOLS),
            supports_streaming=False,
            supports_cancel=False,
            description=self.description,
        )
