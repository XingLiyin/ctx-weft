"""判别值必须来自集中定义，不许散落字面量（总账 C1/C2）。"""

from __future__ import annotations

import pathlib
import re

from ctx_weft.core.discriminators import CancelReason, InterruptReason, TaskErrorCode


def test_enum_values_are_the_wire_strings():
    """StrEnum 的值就是上线上的字面量——改值等于改对外契约。"""
    assert InterruptReason.LLM_OUTAGE == "llm_outage"
    assert InterruptReason.RUN_CRASH == "run_crash"
    assert InterruptReason.ASSEMBLY_FAILURE == "assembly_failure"
    assert CancelReason.USER_CANCEL == "user_cancel"
    assert CancelReason.FAILURE_THRESHOLD == "failure_threshold"
    assert CancelReason.PAUSE_ABANDON == "pause_abandon"
    assert TaskErrorCode.BY_OBSERVER == "TASK_FAILED_BY_OBSERVER"
    assert TaskErrorCode.RETRY_EXHAUSTED == "TASK_FAILED_RETRY_EXHAUSTED"
    assert TaskErrorCode.BY_THRESHOLD == "TASK_FAILED_BY_THRESHOLD"


_SRC = pathlib.Path("src/ctx_weft")
_ALLOWED = {"src/ctx_weft/core/discriminators.py"}


def _literal_sites(literal: str) -> list[str]:
    hits: list[str] = []
    pat = re.compile(rf'"{re.escape(literal)}"')
    for p in _SRC.rglob("*.py"):
        rel = p.as_posix()
        if rel in _ALLOWED:
            continue
        for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if pat.search(code):
                hits.append(f"{rel}:{lineno}")
    return hits


def test_no_stray_reason_literals():
    """这些判别值只许从 discriminators 引用（注释里出现不算）。"""
    offenders: list[str] = []
    for lit in ("llm_outage", "run_crash", "assembly_failure",
                "user_cancel", "failure_threshold", "pause_abandon"):
        offenders += _literal_sites(lit)
    assert offenders == [], f"散落的判别值字面量: {offenders}"


def test_no_stray_task_error_code_literals():
    offenders: list[str] = []
    for lit in ("TASK_FAILED_BY_OBSERVER", "TASK_FAILED_RETRY_EXHAUSTED",
                "TASK_FAILED_BY_THRESHOLD"):
        offenders += _literal_sites(lit)
    assert offenders == [], f"散落的 error_code 字面量: {offenders}"
