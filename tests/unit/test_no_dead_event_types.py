"""EventType 的每个成员都必须有发射点，或明确登记为 L 档（只读存量）。

死枚举的代价不是运行期开销，是**读代码的人被误导** —— 他会以为那条事件会发生，
去写消费分支、去等一个永远不来的信号。本守卫把「定义即必须发射」变成红灯。
"""

from __future__ import annotations

import pathlib

from ctx_weft.protocols.events import L_TIER_EVENT_TYPES as _LEGACY_READ_ONLY
from ctx_weft.protocols.events import EventType

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src" / "ctx_weft"


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


# `_referenced_outside_definition` 上面那条弱守卫故意不区分「发射」与「读取」——它只
# 证明 L 档类型在源码里*出现过*，出现在 reducer 的重放分支里也算数，所以没法验证
# 「L 档 ∩ 实际发射集合 = ∅」这条更强的不变式。
#
# 直接用语法规则分辨「这处引用是不是一次真正的发射」在本仓库里不可靠：发射点经常是
# 间接的（例如 `session_state.py::Transition(...)` 只构造一个裸字符串字面量，真正
# 调 `Event(...)` 的地方在另一个文件的 `session_manager.py::_emit_session_event`，
# 靠 `transition.event_type` 传递，两处文本上毫无关联），若按「必须紧邻
# emit(/make_event(/Event( 调用」来判定，会对这类合法发射产生漏报。
#
# 所以这里改用范围更小、但可验证的办法：已经确认（见 task-7 报告）这 13 个 L 档类型
# 在 src/ 下的唯一文本出现处，要么是 events.py 的自身定义，要么落在下列两个「重放/
# 投影历史事件」的模块里——两者都不构造新事件，只读已落盘的 `event.type` 做折叠。
# 白名单显式维护而非用「reducers.py 之外」这种否定式排除：新增一个合法的只读重放
# 模块但忘记登记，这里会转红（安全的失败方向——需要人工确认后显式加白名单，而不是
# 被静默放行）；若某天真的有人在白名单之外的地方写了一行发射 L 档类型的代码，这里
# 同样会转红，抓到的正是这条测试要抓的问题。
_KNOWN_READ_ONLY_MODULES = frozenset({
    "protocols/events.py",              # EventType 定义 + TRANSIENT_EVENT_TYPES 等只读集合
    "core/control/reducers.py",         # 主 reducer：折叠存量日志，读这 13 个类型的分支都在这
    "providers/events/_lifecycle.py",   # 会话活跃性重放（apply_lifecycle/replay_lifecycle）
    # Task 16：SnapshotWriter.on_event 只**消费**、不**发射**——它是挂在活总线上的旁路
    # 订阅者，靠 `event.type == "SessionFinished"` 字符串匹配决定要不要在会话终态落一张
    # 快照，本身从不 `Event(...)` 构造这个类型。SessionManager 状态机退役后，没有任何
    # 组件还会把这个类型送上总线，这个分支变成永久不可达的死分支（无害：判据落空，
    # 直接掉进下面的 periodic 计数分支，不抛错、不误写）。把它换成会话真正的终态信号
    # （比如 TM 的 `TaskQueueDrained` 聚合信号）需要同时改写 6 个既有快照测试对「触发
    # 事件是什么」的契约断言（`test_event_persistence_wiring.py`），超出本任务范围
    # （4 个 SESSION_* 停发 + session_state.py 清理 + 19 处红测试），留给后续任务，
    # 见 task-16-report.md。
    "providers/events/snapshot.py",
})


def test_l_tier_types_have_no_emission_point():
    """L 档类型必须「零发射」，不只是「弱守卫扫到了引用」。

    比 `test_every_event_type_is_emitted_or_registered_legacy` 更严：那条只证明
    L 档类型不是「零引用」（哪怕引用只出现在 reducer 的重放分支里也算过），这条则
    要求它们在已知只读重放模块之外**完全不出现**——既不以 `EventType.<member>`
    形式，也不以裸字符串字面量形式，从而验证「L 档 ∩ 实际发射集合 = ∅」。
    """
    hits: dict[str, list[str]] = {}
    for value in sorted(_LEGACY_READ_ONLY):
        member = next(m.name for m in EventType if m.value == value)
        found = []
        for p in _SRC.rglob("*.py"):
            rel = p.relative_to(_SRC).as_posix().replace("\\", "/")
            if rel in _KNOWN_READ_ONLY_MODULES:
                continue
            text = p.read_text(encoding="utf-8")
            if f"EventType.{member}" in text or f'"{value}"' in text:
                found.append(rel)
        if found:
            hits[value] = found
    assert hits == {}, (
        f"这些 L 档类型在已知只读重放模块之外仍被引用，可能是一个新发射点（应移除或"
        f"重新审视是否还该留在 L 档），也可能是新增了一个合法的只读重放模块（应把它"
        f"加进 _KNOWN_READ_ONLY_MODULES 并说明理由）: {hits}"
    )
