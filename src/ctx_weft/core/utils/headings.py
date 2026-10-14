"""跨层的 prompt 标题契约字符串。

`SUBTASKS_HEADING` 必须住在这里而不是任何一个包里：它的两个消费者是
`assembler/composer.py`（渲染那一段）与 `capabilities/control_tools.py`
（`report_observation` 的 `next_step_hint` 描述里指路「按这一段列出的标题+id 指名」），
而 `assembler -> capabilities` 已经存在（composer 引控制工具的限定名），放进 assembler
会让 capabilities 反向引它、成环。

`FINAL_OUTPUT_HEADING`（2026-09-28 加）在本模块里的理由不是环，是**跨仓**：它的第二个
消费者是 observer 的 ROLE.md，而那份文件住在宿主仓、由业务方维护。

`PROGRESS_SO_FAR_HEADING`（2026-10-08 移入）当初留在 `assembler/sources/_history.py`，
理由是「只有一个消费者，同时也是唯一的渲染者」。后来段作用域指令也要引用它，若各处抄裸
字面量，改标题时就会静默失准，因此同样收在这里作为跨层契约。
"""

from __future__ import annotations

__all__ = ["FINAL_OUTPUT_HEADING", "PROGRESS_SO_FAR_HEADING", "SUBTASKS_HEADING"]

# observe prompt 里「自己派生的子任务清单」段的标题前缀。跨层字符串契约，勿散写字面量。
#
# 2026-09-19 之前它叫 SUBTASKS_REVIEW_HEADING，配套的是 `report_observation` 的
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

# 当前 task 的上一段复述在重建对话里冠的标题。渲染由 `assembler/sources/_history.py`
# 完成；Composer asset 的 `review.context` 通过 prompts 自动占位符引用它来界定下一段起点。
PROGRESS_SO_FAR_HEADING = "## Progress So Far"
