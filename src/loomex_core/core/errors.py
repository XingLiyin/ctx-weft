"""LoomeX-00 错误类型。"""

from __future__ import annotations


class LoomeXError(Exception):
    """所有 LoomeX 异常的基类。"""

    code: str = "LoomeX_ERROR"

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


# ── 状态相关 ───────────────────────────────────────────────────────────────────


class AgentNotFound(LoomeXError):
    code = "AGENT_NOT_FOUND"


class TaskNotFound(LoomeXError):
    code = "TASK_NOT_FOUND"


class SessionNotFound(LoomeXError):
    code = "SESSION_NOT_FOUND"


# ── Capability 相关 ────────────────────────────────────────────────────────────


class UnknownCapability(LoomeXError):
    code = "UNKNOWN_CAPABILITY"


class CapabilityNotAuthorized(LoomeXError):
    code = "CAPABILITY_NOT_AUTHORIZED"


class DuplicateCapabilityName(LoomeXError):
    code = "DUPLICATE_CAPABILITY_NAME"


# ── Guard 相关 ─────────────────────────────────────────────────────────────────


class BudgetExceededError(LoomeXError):
    code = "TOKEN_BUDGET_EXCEEDED"


class ContextOverflowError(LoomeXError):
    code = "CONTEXT_OVERFLOW"


class MaxTurnsExceeded(LoomeXError):
    code = "MAX_TURNS_EXCEEDED"


# ── Loop 控制流 ────────────────────────────────────────────────────────────────


class TaskFailedByObserver(LoomeXError):
    code = "TASK_FAILED_BY_OBSERVER"


class RunCanceledError(LoomeXError):
    code = "RUN_CANCELED"
