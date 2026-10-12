"""两张边界名单：哪些边界要产判决、哪些是 close。

这里是**唯一真相源**。装配层不再镜像判定名单（它按 `request.purpose` 走，而 purpose 正是
`recap` 依本模块选出来的）；`CLOSE_BOUNDARIES` 仍在 `assembler.composer` 有一份同名镜像，
那一份由 `tests/unit/test_background_observe_prompt.py` 钉住不漂。
"""

from __future__ import annotations

CLOSE_BOUNDARIES = {"finish", "normal"}

# 段边界折叠会 supersede 的 raw 类型（= apply_compact 的非保护类型；与 finalize._FINAL_RAW_TYPES
# 同构，本地定义避免与 finalize 交叉 import）
#: 要产 verdict 的边界 = 「让位给人」的那两个。**这里是唯一真相源**：装配层不再镜像这张
#: 名单，它按 `request.purpose`（判定档 `background_observe` / 只摘要档 `background_recap`）
#: 走，而 purpose 正是本文件按 `judges()` 选出来的（2026-09-28）。
JUDGING_BOUNDARIES = {"plain_text", "finish_park"}


def judges(boundary: str) -> bool:
    """这个边界的后台 observe 要不要产 verdict（2026-09-22，2026-09-27 加 `finish_park`）。

    **让位给人的那两个**：人在旁边等着，而「这段话/这次收尾到底成没成」得有人判。

    - `plain_text`（S5）：「这段话是想问人还是交付完了」只有判定能区分。
    - `finish_park`（S-b）：root 的 `finish_task` 从此也让位。它此前在 root 上是**零复核**
      的——`_should_use_llm` 对 `parent_task_id is None` 降级走机械判决，而
      `_mechanical_verdict` 把 `actor_done` 无条件映射成 success，那道 success-without-outputs
      护栏又长在 `report_task_outcome` 里、不在机械判决的路上。于是「纯文本被判、明确宣布
      完成反而不被判」这个不对称，等于给 LLM 留了一个能绕开复核的开关。

    其余边界的判定已由别处给出——`mechanical` 是机械判决刚判过、close 边界（`finish` /
    `normal`，即**不让位**的那条 finish_task：unattended 或子任务）是前台 observe 判的、
    `interrupt` / `dispatch` 压根不是一个结局——后台再判一次只会把那份判决覆盖掉。

    **这只是判定档的一半条件**：另一半是这个 agent 得真有 observer（`has_observe_role`）。
    两者相与，见 `_run_recap` 里那个 `judging`。
    """
    return boundary in JUDGING_BOUNDARIES


