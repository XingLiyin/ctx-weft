"""HITL 契约：host-facing 的请求形态与状态。

``HitlRequest`` 是 core 与 host 之间的交换类型——host 读 ``form`` 决定 UI、经
``HitlManager.approve/answer/reject`` 回话。故它属于 protocols 而非 core 内部状态。

``form`` 是**开放扩展点**（``str`` 而非闭 ``Literal``）：host 可定义自己的等待形态，
core 只负责原样透传、不做白名单校验。``outcome`` 与之对称，同样开放——core 只认
``accepted``/``rejected``/``cancelled`` 三个内建值，其余原样透传、不校验。真正封闭的
是**事件类型**——`HitlManager._emit` 对 ``EVENT_TYPES`` 做运行期校验并抛 `ValueError`，
而 5 个 resolve 事件（Approved/Modified/Answered/Rejected/Cancelled）映到 3 个内建
outcome，``outcome`` 是它的**有损投影**。reducer 不消费 outcome，reducer **生产**它。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ctx_weft.protocols.context import ContentPart


def _now_utc() -> datetime:
    """UTC current time。

    刻意不复用 ``core.utils.now_utc``：protocols 是比 core 低的层，模块级反向 import
    会把依赖做反（同 ``protocols/context.py`` 顶部那处说明）。这一行重复的代价
    小于把 ``now_utc`` 下沉所牵动的连锁改动。
    """
    return datetime.now(UTC)


# 三种内建等待形态（spec 2026-07-05，替代旧 kind + capability_id sentinel 拼判）：
#   approval — 审批门控：放行/拒绝一次工具调用（HumanConfirmationAuthorizer 触发）
#   question — ask_user 结构化提问，答复回灌 LLM
#   wait     — act 纯文本暂停 / 软打断（wait_for_user 冷 park）
# host 可自定义其它值；core 不校验。
HITL_FORM_APPROVAL = "approval"
HITL_FORM_QUESTION = "question"
HITL_FORM_WAIT = "wait"

HitlForm = str

#: 终局结果。**开放值域**，与 `HitlForm` 对称——host 定义了自己的 form，就该能定义自己的
#: 结局。core 只认下面三个内建值，其余原样透传、不校验。
#: 空串 `""` 是「未决」哨兵，host 自定义 outcome 不得使用它。
HitlOutcome = str

HITL_OUTCOME_ACCEPTED = "accepted"
HITL_OUTCOME_REJECTED = "rejected"
HITL_OUTCOME_CANCELLED = "cancelled"


@dataclass
class HitlRequest:
    """一次 HITL 请求（含其解析结果）。内存态与事件回放投影共用的单一实体。

    form 决定语义与应答形态：approval 用 approve/reject；question/wait 用 answer/reject。
    host 据 form 决定 UI（批准/拒绝按钮 vs 答题输入框 vs 普通输入框），并可自定义 form。
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
    outcome: HitlOutcome = ""     # "" = 未决；非空 = 终局（开放值域，见 HitlOutcome）
    # 解析载荷
    # 人类附带的内容：答复 / 拒绝理由 / 备注。多模态回复（含图片）走同一字段。
    message: "str | list[ContentPart]" = ""
    # approval form：人类改写后的工具参数。**生效**——经 `human.py` 透进
    # `AuthorizationDecision.modified_arguments`，`capability_gateway` 的
    # `effective_args` 用它替换原参，再过 `_coerce_args` 交给 provider。
    # （旧注释写的「暂仅记录，不生效」是错的，会让人以为人工改参是安全的空操作。）
    modified_arguments: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=_now_utc)
    resolved_at: datetime | None = None
    # resume-time LLM 覆盖：冷应答触发 session resume 时用的当前所选模型（host 据 entry 传入），
    # 仅供本次 cold-resolve 转发给 recover_session，不入事件、不持久化。
    resume_llm_account: str | None = None
    resume_llm_model: str | None = None

    @property
    def resolved(self) -> bool:
        """是否已有终局。**推导而非存储**——存两份就有一条要维护的不变量
        （`resolved is False` ⟺ `outcome == ""`），而漏维护是静默的：
        `HitlManager.find_resolved_for_tool_call` 会误判成「还没答」，把已答过的问题
        重新问一遍、丢掉用户已给的回复。推导掉之后这种失败不可能发生。
        """
        return bool(self.outcome)

    @property
    def accepted(self) -> bool:
        """approval 语义的便利属性。唯一消费者是 `providers/authorizer/human.py`。"""
        return self.outcome == HITL_OUTCOME_ACCEPTED

    def resolve(self, outcome: HitlOutcome) -> None:
        """终局的唯一写入点。`HitlManager._resolve` 与 `reducers.fold_cold_hitl_decision`
        都经由它，不各写各的赋值。"""
        self.outcome = outcome


# ══════════════════════════════════════════════════════════════════════════════
# 新契约（2026-09-01 重设计 · 段 1）。与上方 legacy 类型并存，段 2 删除 legacy。
# 设计：docs/superpowers/specs/2026-09-01-hitl-redesign-design.md §4 / §5
# ══════════════════════════════════════════════════════════════════════════════

#: `UserTurnDelivery.preface`：注入用户回合时的续接修饰。取代 legacy 的
#: `context` 字符串 sniffing（"plain_text" / "interrupt" / "interrupt:edit"）。
PREFACE_NORMAL = "normal"
PREFACE_AFTER_INTERRUPT = "interrupt"
PREFACE_AFTER_INTERRUPT_EDIT = "interrupt_edit"


@dataclass(frozen=True)
class ToolResultDelivery:
    """决定作为该 tool_call 的结果送达 → 热路径就地重入 / 冷路径 reconcile 精确重入。"""

    tool_call_id: str


@dataclass(frozen=True)
class UserTurnDelivery:
    """决定作为一条 user 消息注入任务对话 → 置 PENDING 重排。"""

    task_id: str
    preface: str = PREFACE_NORMAL


@dataclass(frozen=True)
class NoResumeDelivery:
    """纯通知 / 取消，不续跑。"""


#: **封闭值域**——与开放的 `form` 正交（spec §5）。host 可以定义新的等待形态，
#: 但不能定义新的回灌方式；因此续跑路由的每个取值 core 都认识、都有确定行为。
Delivery = ToolResultDelivery | UserTurnDelivery | NoResumeDelivery


@dataclass
class HitlAsk:
    """「我需要一个人的决定」——provider 产出的纯意图。provider 唯一需要构造的类型。

    展示槽位（prompt / detail / fields / proposal）是**通用**的：因为 form 是开放值域，
    不可能做穷举的 tagged union，host 自定义 form 复用同一组槽位。私有语义不得混进来。
    """

    form: str
    delivery: Delivery
    prompt: str = ""                                  # 给人看的主问题
    detail: str = ""                                  # 展示用补充说明
    fields: list[dict[str, Any]] = field(default_factory=list)   # 结构化提问
    proposal: dict[str, Any] | None = None            # 被门控的参数（approval 用）
    subject_id: str = ""                              # 被门控的能力 id（展示与审计）
    #: 不透明续跑载荷：core 原样保存、重入时原样回传，**永不解读**。必须可序列化。
    resume_state: dict[str, Any] | None = None
    #: True = 人的答复直接作工具结果，重入不发生（`ask_user` 走这条）。
    reply_as_result: bool = False


@dataclass
class HitlDecision:
    """「人给了什么」——core 喂给发起方的结果。无 id、无时间、无 session。"""

    outcome: HitlOutcome
    message: "str | list[ContentPart]" = ""
    modified_arguments: dict[str, Any] | None = None


@dataclass(frozen=True)
class ResumeHint:
    """应答时携带的当前所选模型。属于**这一次应答**，不属于这个请求——故不入事件、不入状态。"""

    llm_account: str | None = None
    llm_model: str | None = None


@dataclass
class HitlReply:
    """host → core 的一次应答命令。"""

    hitl_id: str
    outcome: HitlOutcome
    message: "str | list[ContentPart]" = ""
    modified_arguments: dict[str, Any] | None = None
    resume_hint: ResumeHint = field(default_factory=ResumeHint)


@dataclass
class HitlRequestView:
    """core → host 的只读视图：渲染 UI 与 pending 列表用。

    刻意**不含** `tool_call_id`——那是 core 的幂等键，host 不需要（spec §4）。
    """

    id: str
    form: HitlForm
    session_id: str
    task_id: str
    created_at: datetime
    agent_id: str = ""
    subject_id: str = ""
    prompt: str = ""
    detail: str = ""
    fields: list[dict[str, Any]] = field(default_factory=list)
    proposal: dict[str, Any] | None = None
    outcome: HitlOutcome = ""
    resolved_at: datetime | None = None

    @property
    def resolved(self) -> bool:
        """推导而非存储——存两份就有一条要维护的不变量，而漏维护是静默的。"""
        return bool(self.outcome)
