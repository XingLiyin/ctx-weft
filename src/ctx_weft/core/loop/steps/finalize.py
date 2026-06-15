"""FinalizeStep：task 收尾——写 memory + blackboard publish + 更新 task 状态。

miniAgents 对齐版：
- memory 内容 = task.outputs + "\\n\\nProcess Report: " + verdict.summary（合并写入）
- 新增 BLACKBOARD_PUBLISH：让父 agent 通过 recall_topic(task.id) 读到子任务结果
"""

from __future__ import annotations

import logging
from typing import Any

from ctx_weft.core.loop.driver import LoopContext, LoopState, Step, StepOutcome, make_event
from ctx_weft.core.events import EventType
from ctx_weft.core.utils import now_utc
from ctx_weft.protocols import MemoryEvent, MemoryEventType, MemoryScope

logger = logging.getLogger(__name__)


class FinalizeStep(Step):
    name = "finalize"

    async def execute(self, state: LoopState, ctx: LoopContext) -> StepOutcome:
        task = state.task
        verdict = state.verdict
        outcome = verdict.task_outcome if verdict else "fail"
        summary = verdict.summary if verdict else ""
        events: list[Any] = []

        # retry 超过上限 → 降级 fail（不再重试）
        if outcome == "retry" and task.retry_count >= task.max_retries:
            outcome = "fail"
            task.status = "FAILED"
            task.observer_outcome = "fail"

        terminal = outcome in ("success", "fail")
        mem_content = _build_memory_content(task.outputs, summary)

        # 1) 经验写入：仅 success/fail 终结才写（spec/06 §5/§6）。
        # 委派回填：把 output+report 作为 TASK_DISPATCH_RESULT 写进 parent 的 agent 层，
        # 按 origin_tool_call_id 与 parent 的 delegate_task/delegate_plan 配对。
        # 不写 self 经验——agent 的经验 = 它派发的子任务（spec/06 §4.2 零合成）。
        if terminal and task.parent_task_id and task.origin_tool_call_id and mem_content:
            parent_scope = MemoryScope(
                session_id=state.scope.session_id,
                task_id=task.parent_task_id,
                agent_id=task.creator_agent_id,
            )
            await ctx.memory.ingest(
                MemoryEvent(
                    type=MemoryEventType.TASK_DISPATCH_RESULT,
                    scope=parent_scope,
                    content=mem_content,
                    timestamp=now_utc(),
                    role="tool",
                    metadata={"tool_call_id": task.origin_tool_call_id, "child_task_id": task.id,
                              "title": task.title, "outcome": outcome, "parent_task_id": task.parent_task_id},
                ),
                ctx.provider_ctx,
            )
            events.append(make_event(
                state, EventType.MEMORY_INGESTED,
                payload={
                    "memory_event_type": MemoryEventType.TASK_DISPATCH_RESULT.value,
                    "source": "dispatch_result",
                    "content_length": len(mem_content),
                },
            ))

        # 2) 按 outcome 分派（task.status 已由 ObserveStep 设置）
        if outcome == "success":
            task.finished_at = now_utc()
            task.process_report = summary
            events.append(make_event(
                state, EventType.TASK_FINISHED,
                payload={"outcome": "success", "summary": summary, "outputs": task.outputs},
            ))
        elif outcome == "fail":
            task.finished_at = now_utc()
            task.process_report = summary
            events.append(make_event(
                state, EventType.TASK_FAILED,
                payload={
                    "error_code": "TASK_FAILED_BY_OBSERVER",
                    "error_message": summary,
                    "retry_count": task.retry_count,
                },
            ))
        elif outcome == "retry":
            # 不重复注入 user message——原始任务消息一开始就在 task 层。observe 的新增信息 =
            # 对本轮 process 的分析 + next step hint，作为 process_report → 下一轮 Current Progress。
            # 机械退出（max_turns/context_limit）也归到这里：重排再跑，受 max_retries 兜底。
            task.outputs = None
            task.process_report = summary
            task.retry_count += 1
            events.append(make_event(
                state, EventType.TASK_REQUEUED,
                payload={"outcome": "retry", "summary": summary, "retry_count": task.retry_count},
            ))

        # 3) success 时发布 BLACKBOARD，供任何 agent 按 task_id 精确召回
        if outcome == "success" and mem_content:
            await ctx.memory.ingest(
                MemoryEvent(
                    type=MemoryEventType.BLACKBOARD_PUBLISH,
                    scope=state.scope,
                    content=mem_content,
                    timestamp=now_utc(),
                    role="assistant",
                    topic=task.id,
                    metadata={"task_id": task.id, "title": task.title, "outcome": outcome,
                              "parent_task_id": task.parent_task_id},
                ),
                ctx.provider_ctx,
            )
            events.append(make_event(
                state, EventType.BLACKBOARD_PUBLISHED,
                payload={"topic": task.id, "content_length": len(mem_content), "parent_task_id": task.parent_task_id},
            ))

        events.append(make_event(
            state, EventType.TASK_FINALIZED,
            payload={"task_id": task.id, "outcome": outcome},
        ))

        return StepOutcome(next_step=None, state_patch={}, events=events)


def _build_memory_content(outputs: Any, summary: str) -> str:
    """合并 task outputs 和 observer summary，对齐 miniAgents _write_execution_memory。

    格式："{output_text}\\n\\nProcess Report: {summary}"
    只有 summary 时："{summary}"
    """
    output_text = ""
    if isinstance(outputs, list):
        output_text = next(
            (p.get("text", "") for p in outputs if isinstance(p, dict) and p.get("type") == "text"),
            "",
        )
    elif isinstance(outputs, str):
        output_text = outputs

    parts = [p for p in [output_text, summary] if p]
    return "\n\nProcess Report: ".join(parts)
