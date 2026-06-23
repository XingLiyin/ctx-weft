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
from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols import MemoryEvent, MemoryEventType, MemoryScope
from ctx_weft.protocols.capability import qualify

logger = logging.getLogger(__name__)

# few-turns 阈值：root task 的 assistant 轮次（LLM_RESPONSE）≤ 此值 → 保全整段对话；
# 超过 → 折叠为单个合成 delegate_task↔result 派发对。
ROOT_SELF_EXPERIENCE_TURN_LIMIT = 3

# root task 对话保全时拉取的 task 层类型（与 RecentMemorySource 一致）
_ROOT_CONVERSATION_TYPES = [
    MemoryEventType.USER_PROMPT,
    MemoryEventType.LLM_RESPONSE,
    MemoryEventType.TOOL_RESULT,
    MemoryEventType.TASK_COMPACT_SUMMARY,
]


def _is_agent_root_task(task, agent_id: str | None) -> bool:
    """task 是否为 agent_id 的 root task。

    判据(无标签、无 parent 链上溯,只看 task 自身的 creator/assigned):
      - parent_task_id is None → session 根;或
      - assigned_agent_id == agent_id 且 creator != assigned → 被别的 agent 派进来交给它的根。
    同 agent 内派生(use_subagent=False:creator==assigned)、派给别的 agent 的子任务
    (use_subagent=True:assigned!=agent_id)都不满足 → 视为 sub-task。
    """
    if task.parent_task_id is None:
        return True
    return (
        task.assigned_agent_id == agent_id
        and task.creator_agent_id != task.assigned_agent_id
    )


async def fold_root_subtree(memory, agent_scope, root_task, get_task, provider_ctx) -> int:
    """root finalize 时,把本次 root 子树的 dispatch 记录在 agent 层折掉(supersede)。

    保留判据:一条 TASK_DISPATCH/RESULT 指向的 child task 若本身是该 agent 的 root
    (_is_agent_root_task)则保留——这正是 root 自身的 self-experience pair,以及顺序
    跑过的更早 root 的经验;否则(两类 sub-task)折掉。child task 经 child_task_id 用
    get_task 查得;DISPATCH 无 child_task_id,按 tool_call_id 配到 RESULT 取。
    返回 superseded 的条数。get_task 缺失/查不到的记录一律不动(降级安全)。
    """
    agent_id = root_task.assigned_agent_id
    records = await memory.recall_recent(
        agent_scope,
        [MemoryEventType.TASK_DISPATCH, MemoryEventType.TASK_DISPATCH_RESULT],
        2000, provider_ctx,
    )
    # tool_call_id → child_task_id（来自 RESULT；DISPATCH 据此回查）
    child_by_tcid: dict[str, str] = {}
    for r in records:
        if r.type == MemoryEventType.TASK_DISPATCH_RESULT:
            tcid = r.metadata.get("tool_call_id")
            cid = r.metadata.get("child_task_id")
            if tcid and cid:
                child_by_tcid[tcid] = cid

    to_supersede: list[str] = []
    for r in records:
        cid = r.metadata.get("child_task_id") or child_by_tcid.get(r.metadata.get("tool_call_id"))
        if not cid:
            continue  # 未配对 / 无法定位 child → 不动
        child = get_task(cid) if get_task else None
        if child is None:
            continue
        if not _is_agent_root_task(child, agent_id):
            to_supersede.append(r.id)

    if to_supersede:
        await memory.supersede(to_supersede, provider_ctx)
    return len(to_supersede)


async def record_root_self_experience(memory, scope, task, mem_content, outcome, provider_ctx) -> dict:
    """Write a finished root task's experience into the root agent's agent layer.

    ≤ ROOT_SELF_EXPERIENCE_TURN_LIMIT assistant turns → preserve the whole
    conversation as AGENT_CONVERSATION_TURN records; otherwise → one synthesized
    delegate_task↔result pair. Returns {"mode": ..., "count": ...} for eventing.
    """
    n_assistant = await memory.count_recent(
        scope, [MemoryEventType.LLM_RESPONSE], provider_ctx,
    )
    if n_assistant <= ROOT_SELF_EXPERIENCE_TURN_LIMIT:
        count = await _preserve_conversation(memory, scope, task, provider_ctx)
        return {"mode": "conversation", "count": count}
    await _synthesize_dispatch_pair(memory, scope, task, mem_content, outcome, provider_ctx)
    return {"mode": "dispatch", "count": 1}


async def _preserve_conversation(memory, scope, task, provider_ctx) -> int:
    """Copy this task's task-layer conversation into the agent layer (faithful, ordered)."""
    records = await memory.recall_recent(
        scope, _ROOT_CONVERSATION_TYPES, 2000, provider_ctx,
    )
    count = 0
    for r in reversed(records):  # recall is newest-first → re-ingest chronologically
        md = {"origin_task_id": task.id}
        if r.role == "assistant" and r.metadata.get("tool_calls"):
            md["tool_calls"] = r.metadata["tool_calls"]
        if r.role == "tool" and r.metadata.get("tool_call_id"):
            md["tool_call_id"] = r.metadata["tool_call_id"]
        await memory.ingest(
            MemoryEvent(
                type=MemoryEventType.AGENT_CONVERSATION_TURN,
                scope=scope,
                content=r.content,
                timestamp=r.timestamp,   # preserve original order/anchor
                role=r.role,
                metadata=md,
            ),
            provider_ctx,
        )
        count += 1
    return count


async def _synthesize_dispatch_pair(memory, scope, task, mem_content, outcome, provider_ctx) -> None:
    """Write a synthesized delegate_task↔result pair representing a long root task."""
    tool_call_id = generate_id("tcall")
    ts = now_utc()
    await memory.ingest(
        MemoryEvent(
            type=MemoryEventType.TASK_DISPATCH,
            scope=scope,
            content="",
            timestamp=ts,
            role="assistant",
            metadata={
                "tool_call_id": tool_call_id,
                "tool_name": qualify("control:delegate_task"),
                "arguments": {
                    "title": task.title,
                    "task_prompt": task.user_prompt,
                    "description": task.description,
                },
            },
        ),
        provider_ctx,
    )
    await memory.ingest(
        MemoryEvent(
            type=MemoryEventType.TASK_DISPATCH_RESULT,
            scope=scope,
            content=mem_content,
            timestamp=ts,
            role="tool",
            metadata={
                "tool_call_id": tool_call_id,
                "child_task_id": task.id,
                "title": task.title,
                "outcome": outcome,
                "parent_task_id": None,
            },
        ),
        provider_ctx,
    )


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

        # 1) 经验写入：success/fail 终结才写（spec/06 §5/§6）。两类写入对「子 agent 的 root
        # task」是并存的（不再互斥）：
        #   (a) 委派回填：child 的 output+report 作为 TASK_DISPATCH_RESULT 写进 parent 的 agent 层
        #       ——父级把整棵子树看成一条结果。
        #   (b) self-experience：本 task 若是其自身 agent 的 root（session 根，或被别的 agent 派进来
        #       的根），把自身对话/结果折进自己的 agent 层，并把本次 root 子树的 dispatch 记录折掉
        #       （fold_root_subtree）——日后作为 experience 召回时只见 root 经验、不见逐个 sub-task。
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

        # (b) self-experience：本 task 是其自身 agent 的 root 时触发（不再仅限 session 根）。
        if terminal and mem_content and _is_agent_root_task(task, task.assigned_agent_id):
            # 先折子树（supersede 本次 root 期间派生的 sub-task dispatch 记录），再写 root 经验，
            # 使新写的经验记录不被本次 fold 命中。
            get_task = ctx.task_manager.get_task if ctx.task_manager else None
            folded = await fold_root_subtree(
                ctx.memory, state.scope, task, get_task, ctx.provider_ctx,
            )
            if folded:
                events.append(make_event(
                    state, EventType.MEMORY_COMPACTED,
                    payload={"source": "root_subtree_fold", "superseded_count": folded,
                             "layer": "agent"},
                ))
            info = await record_root_self_experience(
                ctx.memory, state.scope, task, mem_content, outcome, ctx.provider_ctx,
            )
            events.append(make_event(
                state, EventType.MEMORY_INGESTED,
                payload={
                    "memory_event_type": MemoryEventType.AGENT_CONVERSATION_TURN.value
                    if info["mode"] == "conversation"
                    else MemoryEventType.TASK_DISPATCH_RESULT.value,
                    "source": "root_conversation" if info["mode"] == "conversation" else "root_dispatch",
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
