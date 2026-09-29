"""跨层的 prompt 标题契约字符串。

`SUBTASKS_HEADING` 必须住在这里而不是任何一个包里：它的两个消费者是
`assembler/composer.py`（渲染那一段）与 `capabilities/control_tools.py`
（`report_task_outcome` 的 `next_step_hint` 描述里指路「按这一段列出的标题+id 指名」），
而 `assembler -> capabilities` 已经存在（composer 引控制工具的限定名），放进 assembler
会让 capabilities 反向引它、成环。

`FINAL_OUTPUT_HEADING`（2026-09-28 加）在本模块里的理由不是环，是**跨仓**：它的第二个
消费者是 observer 的 ROLE.md，而那份文件住在宿主仓、由业务方维护。

它们的兄弟 `PROGRESS_SO_FAR_HEADING` 不在这里——那个只有 `assembler/sources/_history.py`
一个消费者（同时也是唯一的渲染者），已内联到那边。
"""

from __future__ import annotations

__all__ = ["FINAL_OUTPUT_HEADING", "SUBTASKS_HEADING"]

# observe prompt 里「自己派生的子任务清单」段的标题前缀。跨层字符串契约，勿散写字面量。
#
# 2026-09-19 之前它叫 SUBTASKS_REVIEW_HEADING，配套的是 `report_task_outcome` 的
# `task_reviews` 参数（可 confirm/reopen 子任务）。那套连同 reopen 一并删除——这一段
# 现在纯是**信息**：observer 据它指名哪个子任务的产出不合格，写进 next_step_hint，
# 由下一轮 actor 自己决定重派还是自己做。
SUBTASKS_HEADING = "## Your sub-tasks"

# `finish_task` 收尾时注入 actor 产出那一段的标题。**这个名字是跨仓契约**：observer 的
# ROLE.md（住在宿主仓）里有一句「产出由 trailing prompt 注入在这个标题下」，用来告诉观察者
# 去哪儿找证据——那一段产出不在重建的对话里（`finish_task` 是 SILENT 工具）。
#
# 改了这里而不改那边，ROLE 当场开始撒谎，且不会有任何报错。改造前 ROLE 里那个指向**不存在
# 工具**的名字就是这样烂掉的（2026-09-22 到 09-28 之间一直在撒谎）。导出成常量，好让宿主侧的
# `tests/test_default_observer_role.py` 拿它对 ROLE 文本做断言——文字契约只能靠这种方式钉。
FINAL_OUTPUT_HEADING = "## Actor's Final Output"
