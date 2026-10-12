"""判别值必须来自集中定义，不许散落字面量（总账 C1/C2）。"""

from __future__ import annotations

import pathlib
import re

from ctx_weft.core.models.discriminators import CancelReason, InterruptReason, TaskErrorCode


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


#: 锚定本文件位置，**不吃调用 cwd**：写成相对路径时，从 tests/ 下跑 rglob 会命中
#: 0 个文件 → offenders 恒空 → 守卫报绿。零扫描即通过，正是本批次在治的那类假绿灯
#: （与 golden `_GOLDEN_DIR` 那次同源）。
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src" / "ctx_weft"
#: `discriminators.py` 本身整份豁免：那是判别值的定义处，枚举成员的值字面量
#: 天然就长在这个文件里，不是「散落」。
_DISCRIMINATORS_FILE = "src/ctx_weft/core/models/discriminators.py"

#: 守卫抓的是散落的**判别值**。`_loader.py` 里的 `failure_threshold` 是**配置
#: schema 的字段名**（模板作者面向的 YAML key），与判别值 `CancelReason.
#: FAILURE_THRESHOLD` 是两份独立演进的契约，恰好同名而已——把 key 绑上枚举会让
#: 日后改判别值名字时静默读错 YAML key。这是假阳性，正确处理是精确豁免这一个
#: 字面量，而不是整份文件放行——**按 (仓根相对路径, 字面量) 精确放行**，
#: 不是整份文件豁免：粒度粗了，该文件将来真出现别的散落判别值也抓不到
#: （批次二终评 M2）。
_ALLOWED: frozenset[tuple[str, str]] = frozenset({
    ("src/ctx_weft/providers/agent_template_local/_loader.py", "failure_threshold"),
})


def _literal_sites(literal: str) -> list[str]:
    hits: list[str] = []
    pat = re.compile(rf'"{re.escape(literal)}"')
    for p in _SRC.rglob("*.py"):
        # 相对仓根算，与 _ALLOWED 里存的口径一致——_SRC 现在是绝对路径（防 cwd 假绿灯），
        # 直接 as_posix() 会让白名单永远失配。
        rel = p.relative_to(_REPO_ROOT).as_posix()
        if rel == _DISCRIMINATORS_FILE or (rel, literal) in _ALLOWED:
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


def test_guard_scans_a_real_tree_not_an_empty_one():
    """守卫必须真的扫到文件——零扫描也会报绿，那是假绿灯。

    `_SRC` 曾写成相对路径 `pathlib.Path("src/ctx_weft")`，从 `tests/` 下跑时
    rglob 命中 0 个文件、offenders 恒空、守卫报绿。本仓在同一种病上栽过两次
    （golden 的 `_GOLDEN_DIR` 解析到仓根之上，导致 golden 测试从未真正跑过）。
    这条把「扫到了东西」本身变成断言。
    """
    scanned = list(_SRC.rglob("*.py"))
    assert len(scanned) > 50, f"守卫只扫到 {len(scanned)} 个文件，疑似路径解析错误"
    assert (_SRC / "core" / "models" / "discriminators.py").exists(), "_SRC 没指向真的源码树"
