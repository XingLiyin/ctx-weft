"""EventType 的每个成员都必须有发射点，或明确登记为 L 档（只读存量）。

死枚举的代价不是运行期开销，是**读代码的人被误导** —— 他会以为那条事件会发生，
去写消费分支、去等一个永远不来的信号。本守卫把「定义即必须发射」变成红灯。
"""

from __future__ import annotations

import pathlib

from ctx_weft.protocols.events import EventType

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src" / "ctx_weft"

#: L 档：已停止发射，但 reducer 仍读它们以重放存量日志。删除须过退役闸门。
_LEGACY_READ_ONLY = frozenset({
    "SessionStatusChanged", "SessionPausedHitl",
    "HitlRequired", "HitlApproved", "HitlAnswered", "HitlRejected",
    "HitlModified", "HitlCancelled",
    # Task 5（2026-09-03-agent-centric-interaction）：observe.py 的 ReactEventTypes 间接层
    # 删除后，run_observe_react 统一发 LLM_*（靠 state.origin 区分前台/后台），这 4 个
    # BackgroundObserve* 类型停止发射。枚举成员本身按控制方裁定暂不删除（退役闸门是
    # 后续任务的事）——就地登记 L 档，让「EventType 全集 ≡ 实际发射 ∪ L 档」这条不变式
    # 在本 commit 就恢复成立，不把红灯留给下一个任务。
    "BackgroundObserveRequestStarted", "BackgroundObservePromptSent",
    "BackgroundObserveTokenStreamed", "BackgroundObserveResponseFinished",
})


def _referenced_outside_definition(member: str, value: str) -> bool:
    for p in _SRC.rglob("*.py"):
        if p.name == "events.py":
            continue
        text = p.read_text(encoding="utf-8")
        if f"EventType.{member}" in text or f'"{value}"' in text:
            return True
    return False


def test_guard_scans_a_real_tree_not_an_empty_one():
    """零扫描也会报绿——把「扫到了东西」本身变成断言。"""
    assert len(list(_SRC.rglob("*.py"))) > 50
    assert (_SRC / "protocols" / "events.py").exists()


def test_every_event_type_is_emitted_or_registered_legacy():
    dead = [
        m.name for m in EventType
        if m.value not in _LEGACY_READ_ONLY
        and not _referenced_outside_definition(m.name, m.value)
    ]
    assert dead == [], (
        f"这些 EventType 成员零发射、零引用，且未登记为 L 档: {dead}。"
        "要么给它发射点，要么删掉，要么登记进 _LEGACY_READ_ONLY 并说明理由。"
    )


def test_legacy_read_only_members_still_exist():
    """L 档成员不许被顺手删掉——reducer 还要用它们读存量日志。"""
    values = {m.value for m in EventType}
    missing = _LEGACY_READ_ONLY - values
    assert missing == set(), f"L 档成员被删了: {sorted(missing)}"
