"""BlackboardSource：MemoryProvider.recall_topic → blackboard / background blocks。

两个用途：
1. 相关任务通信：pull 当前 task 订阅的 topic → kind=blackboard
   - intent=subtask     → 自己派生的子任务结果（observe 渲染为可 review 段）
   - intent=predecessor → 同 plan 前序结果（observe 渲染为只读段）
2. 跨 session 长期上下文：pull session 订阅的 topic
   - intent=long_term_background → kind=background
   - intent=long_term_project_log → kind=blackboard

intent 透传到 block.metadata，由 purpose template 决定渲染分段。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from ctx_weft.core.utils.ids import generate_id

if TYPE_CHECKING:
    from ctx_weft.core.assembler.assembler import AssemblerDeps, ContextBlock, ContextRequest


class BlackboardSource:
    """按 session 订阅 + 当前 task 的子任务 / 前序关系拉取 topic 记录。"""

    name = "blackboard"

    async def fetch(
        self,
        request: "ContextRequest",
        deps: "AssemblerDeps",
    ) -> AsyncIterator["ContextBlock"]:
        from ctx_weft.core.assembler.assembler import ContextBlock

        # 1) 当前 task 订阅的相关任务结果（子任务 + 同 plan 前序）；只取本 task 的订阅 + session 级
        subscriptions = await deps.memory.list_subscriptions(
            session_id=request.session.id,
            ctx=deps.provider_ctx,
            task_id=request.task.id,
        )

        for sub in subscriptions:
            # 按 intent 决定 block 落位
            if sub.intent == "long_term_background":
                kind = "background"
            elif sub.intent == "long_term_project_log":
                kind = "blackboard"
            else:  # subtask / predecessor — 相关任务结果，intent 透传给 composer 分段渲染
                kind = "blackboard"

            records, _new_cursor = await deps.memory.recall_topic(
                topic=sub.topic,
                since=sub.cursor,
                ctx=deps.provider_ctx,
            )
            # 注意：cursor 更新由 PrepareStep 完成后批量提交；这里只读

            for record in records:
                yield ContextBlock(
                    id=generate_id("blk"),
                    source=f"blackboard:{sub.topic}",
                    kind=kind,  # type: ignore[arg-type]
                    metadata={
                        "channel": "topic",
                        "topic": sub.topic,
                        "intent": sub.intent,
                        "memory_event_id": record.id,
                        "title": record.metadata.get("title", ""),
                        "outcome": record.metadata.get("outcome", ""),
                    },
                    value=record,
                )
