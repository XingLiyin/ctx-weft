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


# ── Guard 相关 ─────────────────────────────────────────────────────────────────


class BudgetExceededError(CtxWeftError):
    code = "TOKEN_BUDGET_EXCEEDED"


class ContextOverflowError(CtxWeftError):
    code = "CONTEXT_OVERFLOW"


class MaxTurnsExceeded(CtxWeftError):
    code = "MAX_TURNS_EXCEEDED"


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
