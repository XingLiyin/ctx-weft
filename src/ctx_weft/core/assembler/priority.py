"""槽位 → 裁剪 priority 的静态基线（tier 表的代码化，见 spec §4.2）。

各 Source 造 block 时调用本函数取 priority，取代散落各处的硬编码整数。
「当前 vs 已完成」是动态轴，不在此——由 budget 提级（当前→4）+ pin（当前 user_prompt→0）。
故此处 raw 一律按"已完成"给 5/6；slot_priority 永不返回 4。
"""

from __future__ import annotations


def slot_priority(kind: str, mem_type: str | None = None) -> int:
    """kind: BlockKind；mem_type: history 类 block 的 MemoryEventType 字符串。
    数字越小越受保护；0 永不裁。丢序 7→1。"""
    if kind in ("identity", "task_spec"):
        return 0
    if kind in ("capabilities", "directive"):
        return 1
    if mem_type == "agent_compact_summary":
        return 2  # agent 层跨 task 折叠（受保护）
    if kind == "blackboard":
        return 3
    # 4 = 当前 task 内容：budget 动态提级，此处不返回
    if mem_type == "agent_conversation_turn":
        return 5  # 已完成 task 的 agent 层回合（finish/dispatch 对）
    if kind == "history":
        return 6  # 已完成 task 的 task 层胶囊（含 task_compact_summary）
    return 7  # knowledge(reference) / long_memory(语义召回 summary)
