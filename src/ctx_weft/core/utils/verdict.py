"""复核裁决的三态值域，全仓一份。

## 为什么第二态叫 `continue` 而不是 `retry`（2026-09-28 改名）

这套裁决的第二态要表达的事实是「**这个 task 还没结束**」，它有三个来源：机械退出
（max_turns / context_limit 把一段截断了）、复核认为本段产出不合格、以及——交互产品里最
常见的那个——**actor 在问用户话，正等着回答**。

`retry` 这个词只贴合中间那一种，对第三种是明确的误导，而它恰好是 root task 每天都在发生的
形态。后果不是文字上的不美观，是**观察者被推着去判 success**：

- 词义上，「重试」暗示这次尝试失败了。观察者看着一段礼貌得体的澄清提问，被要求打上"重试"，
  会觉得这标签不对，转手去找 `success`；
- 字段契约上，`task_failure_reason` 对 `retry` 是必填（"what concretely blocked or fell
  short"）。一个正常的提问没有任何 blocker——观察者要么编一个，要么换个 status。

而判 success 的代价是实打实的：task 就此终结，用户正要说的那一轮被丢掉，park 气泡被收口。
纯文本 park 这条路上**没有任何机械护栏**能挡住它——`report_task_outcome` 那道
success-without-outputs 护栏读的是 `task.outputs`，而 park 前合成的 outputs 正是那段提问本身，
非空，护栏原地失效。所以只剩词面与字段契约这两个抓手，两个都得对。

`continue` 是中性的：它只说「循环继续」，不含对 actor 的评价，三个来源都贴合。

## 值域与归一

对外（工具 schema、prompt）与对内（`disposition_for`、`Task.observer_outcome`、机械判决）
**同一套词**，不做「外部 continue / 内部 retry」的双词表——那种错位会让日志与事件里的值跟
模型说的话对不上。

`retry` 作为**别名**永久保留：prompt 缓存、重放的老 transcript、以及模型自身的先验都会继续
吐它。归一在 `normalize_verdict` 一处完成。

认不出的值 → `continue`，**决不是 `fail`**。改名前 `disposition_for` 的末尾是个 catch-all，
`fail` 与任何拼错的值共用它——一个错别字就让 task 无声判死。方向反了：拿不准时该继续，不该
判死（`max_retries` 自会收口）。
"""

from __future__ import annotations

import logging

__all__ = [
    "VERDICTS",
    "VERDICT_CONTINUE",
    "VERDICT_FAIL",
    "VERDICT_SUCCESS",
    "normalize_verdict",
]

logger = logging.getLogger(__name__)

VERDICT_SUCCESS = "success"
VERDICT_CONTINUE = "continue"
VERDICT_FAIL = "fail"

#: 规范三态，顺序即 prompt 里的呈现顺序。
VERDICTS = (VERDICT_SUCCESS, VERDICT_CONTINUE, VERDICT_FAIL)

#: 历史值 → 规范值。见模块 docstring 的「值域与归一」。
_ALIASES = {"retry": VERDICT_CONTINUE}


def normalize_verdict(raw: object) -> str:
    """把任意来源的裁决值归一成三态之一；认不出的归 `continue` 并记一笔。

    来源有三：LLM 的工具入参、重放的存量事件、机械判决自己填的常量。前两者都可能给出
    `retry`（别名）或彻底不认识的东西。
    """
    value = (raw or "").strip().lower() if isinstance(raw, str) else ""
    if value in VERDICTS:
        return value
    if value in _ALIASES:
        return _ALIASES[value]
    if value:
        logger.warning(
            "unrecognized task verdict %r; treating it as %r (never as a failure)",
            raw, VERDICT_CONTINUE,
        )
    return VERDICT_CONTINUE
