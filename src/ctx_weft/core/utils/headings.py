"""跨层的 prompt 标题契约字符串。

`SUBTASKS_REVIEW_HEADING` 只有一个，但它必须住在这里而不是任何一个包里：它的两个
消费者是 `assembler/composer.py`（渲染）与 `capabilities/control_tools.py`
（`report_task_outcome` 的 `task_reviews` schema 引用），而
`assembler -> capabilities` 已经存在（composer 引控制工具的限定名），放进 assembler
会让 capabilities 反向引它、成环。

它的兄弟 `PROGRESS_SO_FAR_HEADING` 不在这里——那个只有 `assembler/sources/_history.py`
一个消费者（同时也是唯一的渲染者），已内联到那边。
"""

from __future__ import annotations

__all__ = ["SUBTASKS_REVIEW_HEADING"]

# observe prompt 里「可 review 子任务清单」段的标题前缀。跨层字符串契约，勿散写字面量。
SUBTASKS_REVIEW_HEADING = "## Your sub-tasks"
