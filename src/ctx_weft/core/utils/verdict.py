"""Task verdicts shared by tools, observation and scheduling.

success/fail close the task; continue always schedules another actor turn;
waiting parks until the user replies. Historical continue values are not
reinterpreted as waiting. The retry alias and unknown-value normalization
continue to resolve to continue, without consuming exception retry budgets.
"""
from __future__ import annotations

import logging

__all__ = [
    "VERDICTS",
    "VERDICT_CONTINUE",
    "VERDICT_WAITING",
    "VERDICT_FAIL",
    "VERDICT_SUCCESS",
    "normalize_verdict",
]

logger = logging.getLogger(__name__)

VERDICT_SUCCESS = "success"
VERDICT_CONTINUE = "continue"
VERDICT_WAITING = "waiting"
VERDICT_FAIL = "fail"

#: 规范四态，顺序即 prompt 里的呈现顺序。
VERDICTS = (VERDICT_SUCCESS, VERDICT_WAITING, VERDICT_CONTINUE, VERDICT_FAIL)

#: 历史值 → 规范值。见模块 docstring 的「值域与归一」。
_ALIASES = {"retry": VERDICT_CONTINUE}


def normalize_verdict(raw: object) -> str:
    """把任意来源的裁决值归一成四态之一；认不出的归 `continue` 并记一笔。

    来源有三：LLM 的工具入参、重放的存量事件、机械判决自己填的常量。前两者都可能给出
    `retry`（别名）或彻底不认识的东西。
    """
    value = (raw or "").strip().lower() if isinstance(raw, str) else ""
    if value in VERDICTS:
        return value
    if value in _ALIASES:
        return _ALIASES[value]
    if value:
        logger.warning(
            "unrecognized task verdict %r; treating it as %r (never as a failure)",
            raw, VERDICT_CONTINUE,
        )
    return VERDICT_CONTINUE
