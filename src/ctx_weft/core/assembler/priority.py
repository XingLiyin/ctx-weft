"""槽位 → 裁剪 priority 的静态基线（tier 表的代码化，见 spec §4.2）。

各 Source 造 block 时调用本函数取 priority，取代散落各处的硬编码整数。
「当前 vs 已完成」是动态轴，不在此——由 budget 提级（当前→4）+ pin（当前 user_prompt→0）。
故此处 raw 一律按"已完成"给 5/6；slot_priority 永不返回 4。

═══ 保护阶梯（0 最受保护、永不裁；超限时丢序 7→1）═══

  0  identity / task_spec                     身份与当前任务锚，地板
     （+ budget pin：当前 task 的 user_prompt 回合 → 0）
  1  capabilities / directive / background    能力清单、skill 指令、项目背景、
     / guidance                               act 运行时态势 guidance
  2  agent_compact_summary                    agent 层跨 task 折叠摘要
  3  blackboard                               相关任务 topic / project_log
  4 （动态档，本函数不返回）                  当前 task 内容：budget 把
     task_id / origin_task_id 命中当前 task 的 history 从 5/6 提到 4
  5  agent_conversation_turn                  已完成 task 的 finish/dispatch 对
  6  其余 history                             已完成 task 的 task 层胶囊
                                              （含 task_compact_summary）
  7  reference / summary                      知识检索、语义召回

同档内的丢弃顺序（最老先丢、再按体积）与 tool_call↔tool_result 配对原子丢弃
见 budget.PriorityBudgetStrategy。
"""

from __future__ import annotations


def slot_priority(kind: str, mem_type: str | None = None) -> int:
    """kind: BlockKind；mem_type: history 类 block 的 MemoryEventType 字符串。
    数字越小越受保护；0 永不裁。丢序 7→1。"""
    if kind in ("identity", "task_spec"):
        return 0
    if kind in ("capabilities", "directive", "background", "guidance"):
        return 1  # 项目背景=系统提示内容，与能力同档（原 blackboard.py priority 1）
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
