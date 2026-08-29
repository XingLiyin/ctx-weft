"""HITL 契约：host-facing 的请求形态与状态。

``HitlRequest`` 是 core 与 host 之间的交换类型——host 读 ``form`` 决定 UI、经
``HitlManager.approve/answer/reject`` 回话。故它属于 protocols 而非 core 内部状态。

``form`` 是**开放扩展点**（``str`` 而非闭 ``Literal``）：host 可定义自己的等待形态，
core 只负责原样透传、不做白名单校验。``status`` 相反是**闭集**——状态机是 core 的
不变式，新增状态会破坏 reducer 投影。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

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
HitlStatus = Literal["pending", "accepted", "rejected", "cancelled"]


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
    status: HitlStatus = "pending"
    # 解析载荷
    # 人类附带的内容：答复 / 拒绝理由 / 备注。多模态回复（含图片）走同一字段。
    message: "str | list[ContentPart]" = ""
    modified_arguments: dict[str, Any] | None = None  # approval form：改写后的工具参数（暂仅记录，不生效）
    created_at: datetime = field(default_factory=_now_utc)
    resolved_at: datetime | None = None
    # resume-time LLM 覆盖：冷应答触发 session resume 时用的当前所选模型（host 据 entry 传入），
    # 仅供本次 cold-resolve 转发给 recover_session，不入事件、不持久化。
    resume_llm_account: str | None = None
    resume_llm_model: str | None = None

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"
