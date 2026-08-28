"""ctx-weft 错误类型。"""

from __future__ import annotations


class CtxWeftError(Exception):
    """所有 ctx-weft 异常的基类。"""

    code: str = "CTX_WEFT_ERROR"

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


# ── 状态相关 ───────────────────────────────────────────────────────────────────


class AgentNotFound(CtxWeftError):
    code = "AGENT_NOT_FOUND"


class TaskNotFound(CtxWeftError):
    code = "TASK_NOT_FOUND"


class SessionNotFound(CtxWeftError):
    code = "SESSION_NOT_FOUND"


# ── Capability 相关 ────────────────────────────────────────────────────────────


class UnknownCapability(CtxWeftError):
    code = "UNKNOWN_CAPABILITY"


class CapabilityNotAuthorized(CtxWeftError):
    code = "CAPABILITY_NOT_AUTHORIZED"


class DuplicateCapabilityName(CtxWeftError):
    code = "DUPLICATE_CAPABILITY_NAME"


class TemplateNotFoundError(CtxWeftError):
    """模板查找失败：裸 id 无前缀 / 前缀路由不到 provider / provider 不认识局部名。

    模板引用必须是规范形式 'provider:name'（如 'agent:planner'）——边界强制前缀
    （spec 2026-07-22），core 不做扫描回落。"""

    code = "TEMPLATE_NOT_FOUND"

    def __init__(self, template_ref: str, *, providers: list[str] | None = None) -> None:
        self.template_ref = template_ref
        self.providers = list(providers or [])
        hint = (
            f"registered agent providers: {', '.join(self.providers)}"
            if self.providers else "no AgentCapabilityProvider registered"
        )
        super().__init__(
            f"Template {template_ref!r} not found ({hint}); template refs must be "
            f"canonical 'provider:name', e.g. 'agent:planner'"
        )


# ── Guard 相关 ─────────────────────────────────────────────────────────────────


class BudgetExceededError(CtxWeftError):
    code = "TOKEN_BUDGET_EXCEEDED"


class ContextOverflowError(CtxWeftError):
    code = "CONTEXT_OVERFLOW"
    retriable = False  # 非瞬时：同批 block 重装配必再溢出，不可 resume（spec §4.5）

    def __init__(
        self,
        message: str = "",
        *,
        code: str | None = None,
        required: int = 0,
        effective_limit: int = 0,
        context_limit: int = 0,
        reserved_output_tokens: int = 0,
        image_count: int = 0,
    ) -> None:
        self.required = required
        self.effective_limit = effective_limit
        self.context_limit = context_limit
        self.reserved_output_tokens = reserved_output_tokens
        self.image_count = image_count
        if not message and context_limit:
            message = (
                f"上下文超出模型可用窗口：保护槽位（角色设定 + 当前任务/消息）约 {required} tokens，"
                f"已超过为输出预留后的可用窗口 effective_limit={effective_limit}"
                f"（= 模型窗口 {context_limit} − 输出预留 {reserved_output_tokens}）。"
            )
            if image_count:
                # 函数内 import：errors.py 是被广泛引入的底层模块，故意保持依赖轻量，
                # 不在模块顶层拉 core.utils（已确认 core/utils.py 只导入 stdlib + ulid，
                # 模块级 import 也不会成环——此处仍就地导入是刻意的，不是遗留待清理项）。
                from ctx_weft.core.utils import _IMAGE_PART_TOKENS
                message += (
                    f"其中不可裁的当前消息含 {image_count} 张图片，"
                    f"约占 {image_count * _IMAGE_PART_TOKENS} tokens。"
                    "请先减少图片数量，或改用更大上下文窗口的模型。"
                )
            else:
                message += "请改用更大上下文窗口的模型，或缩短当前消息 / 任务描述。"
        super().__init__(message, code=code)


class MaxTurnsExceeded(CtxWeftError):
    code = "MAX_TURNS_EXCEEDED"


# ── 入口内容校验 ───────────────────────────────────────────────────────────────


class InvalidContentError(CtxWeftError):
    """入口内容格式非法：未知 media_type / base64 畸形 / 单图超限。

    入口即拒，不落库——比让畸形内容流到 provider 侧再 400 更早、更可诊断。
    """

    code = "INVALID_CONTENT"


class BlobStoreRequiredError(CtxWeftError):
    """携图内容要求宿主注册 EventBlobStore（spec 2026-08-27 双 blob store §7）。

    事件库的口径是**恒不含字节、恒可回读**，没有例外分支——没有 event blob store 就
    无处放字节，只能在入口拒绝。纯文本会话不受影响。
    """

    code = "BLOB_STORE_REQUIRED"


# ── Loop 控制流 ────────────────────────────────────────────────────────────────


class TaskFailedByObserver(CtxWeftError):
    code = "TASK_FAILED_BY_OBSERVER"


class RunCanceledError(CtxWeftError):
    code = "RUN_CANCELED"


# ── Session 状态 ───────────────────────────────────────────────────────────────


class SessionBusyError(CtxWeftError):
    """Raised when an operation needs an idle session but the session is currently
    running (draining / compacting). The caller should retry once it is idle."""

    code = "SESSION_BUSY"

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"Session {session_id!r} is busy (currently running); try again when idle")


class UnfinishedTasksError(CtxWeftError):
    """开新一轮（resume_session）被拒：事件里仍有未终结任务。

    弃轮（不恢复、直接开新轮）会把滞留的非终态任务永久遗弃在事件库,之后任何
    recover_session 全量重建又会把它们复活重跑（僵尸重跑）。调用方应引导用户走
    恢复路径（/resume → recover_session）续跑或收尾这些任务。"""

    code = "SESSION_HAS_UNFINISHED_TASKS"

    def __init__(self, session_id: str, task_ids: list[str]) -> None:
        self.session_id = session_id
        self.task_ids = list(task_ids)
        shown = ", ".join(self.task_ids[:5]) + ("…" if len(self.task_ids) > 5 else "")
        super().__init__(
            f"Session {session_id!r} still has {len(self.task_ids)} unfinished task(s) "
            f"({shown}); resume the session instead of starting a new turn"
        )
