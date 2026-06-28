"""FinalizeStep：task 收尾——写 memory + blackboard publish + 更新 task 状态。

miniAgents 对齐版：
- memory 内容 = task.outputs + "\\n\\nProcess Report: " + verdict.summary（合并写入）
- 新增 BLACKBOARD_PUBLISH：让父 agent 通过 recall_topic(task.id) 读到子任务结果
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from ctx_weft.core.loop.driver import LoopContext, LoopState, Step, StepOutcome, make_event
from ctx_weft.core.events import EventType
from ctx_weft.core.utils import content_to_text, estimate_tokens, generate_id, now_utc
from ctx_weft.protocols import MemoryEvent, MemoryEventType, MemoryScope
from ctx_weft.protocols.capability import qualify

logger = logging.getLogger(__name__)

# close 时软删/折叠的 task 层类型（OPEN task 对话的全部）
_OWN_CONV_TYPES = [
    MemoryEventType.USER_PROMPT,
    MemoryEventType.LLM_RESPONSE,
    MemoryEventType.TOOL_INVOCATION,
    MemoryEventType.TOOL_RESULT,
    MemoryEventType.TASK_COMPACT_SUMMARY,
]


def _descendant_task_ids(root_id: str, task_manager) -> set[str]:
    """BFS over children_of → 该 task 名下所有后代 task_id（不含自身）。"""
    if task_manager is None:
        return set()
    out: set[str] = set()
    stack = [root_id]
    while stack:
        cur = stack.pop()
        for cid in task_manager.children_of(cur):
            if cid not in out:
                out.add(cid)
                stack.append(cid)
    return out


async def _is_short_leaf(memory, scope, task, loop_config, ctx, has_descendants: bool) -> bool:
    """叶子(无后代) 且 对话 token ≤ threshold 且 LLM_RESPONSE 轮次 ≤ turn_cap → short。"""
    if has_descendants:
        return False  # 非叶（委派过子任务）永不 short
    n_assistant = await memory.count_recent(
        scope, [MemoryEventType.LLM_RESPONSE], ctx.provider_ctx,
    )
    if n_assistant > loop_config.short_task_turn_cap:
        return False
    records = await memory.recall_recent(scope, _OWN_CONV_TYPES, 2000, ctx.provider_ctx)
    text = " ".join(
        content_to_text(r.content) if not isinstance(r.content, str) else r.content
        for r in records
    )
    return estimate_tokens(text) <= loop_config.short_task_token_threshold


async def _supersede_own_conversation(memory, scope, ctx) -> int:
    """软删本 task 自身的 task 层对话（close 的一步）。"""
    records = await memory.recall_recent(scope, _OWN_CONV_TYPES, 2000, ctx.provider_ctx)
    ids = [r.id for r in records]
    return await memory.supersede(ids, ctx.provider_ctx) if ids else 0


async def _gc_subtree(memory, agent_scope, descendants: set[str], ctx) -> int:
    """软删后代 task 的自身 task 层 raw 残留（派发对/嵌入子胶囊保留，由 parent 胶囊管理）。"""
    if not descendants:
        return 0
    ids: list[str] = []
    task_recs = await memory.recall_recent_by_agent(
        agent_scope, _OWN_CONV_TYPES, 2000, ctx.provider_ctx,
    )
    for r in task_recs:
        if r.metadata.get("task_id") in descendants:
            ids.append(r.id)
    return await memory.supersede(ids, ctx.provider_ctx) if ids else 0


async def finalize_task_memory(memory, state, task, mem_content: str, outcome: str, ctx) -> list:
    """finish 时调用：算 short / descendants 后委派 _close_one。返回事件列表。

    见 spec 2026-06-23 §压缩原语 与本计划 Task 4 的决策矩阵。
    """
    descendants = _descendant_task_ids(task.id, ctx.task_manager)
    short = await _is_short_leaf(
        memory, state.scope, task, state.agent.loop_config, ctx, bool(descendants),
    )
    return await _close_one(
        memory, state, task, mem_content, outcome, ctx, short=short, descendants=descendants,
    )


async def _close_one(memory, state, task, mem_content: str, outcome: str, ctx,
                     *, short: bool, descendants: set[str]) -> list:
    """close 主体：bubble / 自身残留 / 软删自身对话 / GC 子树。

    short / descendants 由调用方给定——finish 时由 finalize_task_memory 算得；压力下强制
    回收短 task 时由 close_finished_short_tasks 传 short=False（Task 5）。返回事件列表。
    """
    # Terminal finalize is single-entry by construction (retry is non-terminal; restore reschedules
    # only non-terminal tasks; re-dispatch uses a new task id), so the residue/bubble writes here
    # need no idempotency guard. [2026-06-23]
    events: list[Any] = []
    same_agent = task.creator_agent_id == task.assigned_agent_id
    cross_agent = bool(task.parent_task_id) and not same_agent
    is_own_root = (task.parent_task_id is None) or cross_agent

    # 1) bubble 到 parent scope（dispatch marker 所在 scope）
    if task.parent_task_id and task.origin_tool_call_id and mem_content:
        if cross_agent:
            do_bubble = True
            result_content = mem_content
        elif same_agent and not short:
            do_bubble = True
            result_content = f"Sub-task '{task.title}' scheduled."
        else:
            do_bubble = False
            result_content = ""
        parent_scope = MemoryScope(
            session_id=state.scope.session_id,
            task_id=task.parent_task_id,
            agent_id=task.creator_agent_id,
        )
        if do_bubble and result_content:
            await memory.ingest(
                MemoryEvent(
                    type=MemoryEventType.TASK_DISPATCH_RESULT,
                    scope=parent_scope,
                    content=result_content,
                    timestamp=now_utc(),
                    role="tool",
                    metadata={"tool_call_id": task.origin_tool_call_id, "child_task_id": task.id,
                              "title": task.title, "outcome": outcome,
                              "parent_task_id": task.parent_task_id},
                ),
                ctx.provider_ctx,
            )
            events.append(make_event(
                state, EventType.MEMORY_INGESTED,
                payload={"memory_event_type": MemoryEventType.TASK_DISPATCH_RESULT.value,
                         "source": "dispatch_result", "content_length": len(result_content)},
            ))
        # 同 agent non-short：额外把 child 交互胶囊写进共享 agent scope（嵌套）
        if same_agent and not short:
            await _synthesize_dispatch_pair(
                memory, parent_scope, task, mem_content, outcome, ctx.provider_ctx)

    # 2) 自身残留：own root（session 根或跨 agent 根）且 close 时，在 own scope 合成派发对
    if is_own_root and mem_content and not short:
        await _synthesize_dispatch_pair(memory, state.scope, task, mem_content, outcome, ctx.provider_ctx)
        events.append(make_event(
            state, EventType.MEMORY_INGESTED,
            payload={"memory_event_type": MemoryEventType.TASK_DISPATCH_RESULT.value,
                     "source": "root_dispatch", "content_length": len(mem_content)},
        ))

    # 3) 软删自身对话（close 时）
    if not short:
        n = await _supersede_own_conversation(memory, state.scope, ctx)
        if n:
            events.append(make_event(
                state, EventType.MEMORY_COMPACTED,
                payload={"source": "close_own_conversation", "superseded_count": n, "layer": "task"},
            ))

    # 4) GC 子树（无条件；叶子时为空）
    g = await _gc_subtree(memory, state.scope, descendants, ctx)
    if g:
        events.append(make_event(
            state, EventType.MEMORY_COMPACTED,
            payload={"source": "subtree_gc", "superseded_count": g, "layer": "agent"},
        ))

    return events


async def close_finished_short_tasks(memory, state, ctx) -> list:
    """压力下回收：对本 agent 名下所有 status==FINISHED 的短 task 强制 close（坍缩成残留）。

    短 task finish 时维持 OPEN；压缩触发时调用本函数把它们 close 掉腾空间。挂起祖先 task
    （status != FINISHED）的对话绝不触碰（spec 2026-06-23 §3）。返回事件列表。
    """
    if ctx.task_manager is None:
        return []
    records = await memory.recall_recent_by_agent(
        state.scope, _OWN_CONV_TYPES, 2000, ctx.provider_ctx,
    )
    task_ids = {
        tid for r in records
        if (tid := r.metadata.get("task_id")) and tid != state.task.id
    }
    events: list[Any] = []
    for tid in task_ids:
        t = ctx.task_manager.get_task(tid)
        if t is None or t.status != "FINISHED":
            continue  # 挂起祖先 / 未知 → 保留
        mem_content = _build_memory_content(t.outputs, t.process_report or "")
        if not mem_content:
            continue
        sub_scope = MemoryScope(
            session_id=state.scope.session_id, task_id=tid, agent_id=state.scope.agent_id,
        )
        sub_state = dataclasses.replace(state, scope=sub_scope, task=t)
        descendants = _descendant_task_ids(tid, ctx.task_manager)  # 短=叶 → 空
        events += await _close_one(
            memory, sub_state, t, mem_content, t.observer_outcome or "success", ctx,
            short=False, descendants=descendants,
        )
    return events


async def _synthesize_dispatch_pair(memory, scope, task, mem_content, outcome, provider_ctx) -> None:
    """close 合成交错时间线胶囊（spec §3.3）：快照幸存 task 对话 → AGENT_CONVERSATION_TURN
    （保留原始 timestamp）+ 末尾合成 finish 对。

    step1: 非阻塞——close 边界 bg observe 已不写 memory（结果落 _close_report 槽），
           survivors 快照不依赖它；A1 设计下不再 await await_pending_background_observe。
    step2: 镜像幸存 task 层事件到 agent 层 AGENT_CONVERSATION_TURN，保留原始 timestamp/role/tool 元数据。
           角色映射：USER_PROMPT→user, TASK_COMPACT_SUMMARY→assistant（继承存储 role）,
           LLM_RESPONSE→assistant（携带 tool_calls），TOOL_RESULT→tool（携带 tool_call_id）。
    step3: 追加合成 finish 对（assistant finish_task tool_call + tool Process Report）。
           A1：机会性取 _close_report 槽；槽空则用薄占位 + 登记 _close_synth 待 bg 异步替换。
    """

    # step2：召回幸存 task 层对话，逐条镜像成 agent 层 AGENT_CONVERSATION_TURN
    task_scope = MemoryScope(session_id=scope.session_id, task_id=task.id, agent_id=scope.agent_id)
    survivors = await memory.recall_recent(
        task_scope,
        [MemoryEventType.USER_PROMPT, MemoryEventType.TASK_COMPACT_SUMMARY,
         MemoryEventType.LLM_RESPONSE, MemoryEventType.TOOL_RESULT],
        2000, provider_ctx,
    )
    survivors = list(reversed(survivors))  # newest-first → 时间序（oldest first）

    for r in survivors:
        if r.type == MemoryEventType.USER_PROMPT:
            role = "user"
        else:
            # TASK_COMPACT_SUMMARY 存储即 assistant；LLM_RESPONSE→assistant、TOOL_RESULT→tool
            # 均已在记录 role 上，直接继承。
            role = r.role or "user"

        md: dict = {"origin_task_id": task.id, "parent_task_id": task.parent_task_id}
        if role == "assistant":
            md["tool_calls"] = r.metadata.get("tool_calls", [])
        elif role == "tool":
            md["tool_call_id"] = r.metadata.get("tool_call_id", "")

        await memory.ingest(
            MemoryEvent(
                type=MemoryEventType.AGENT_CONVERSATION_TURN,
                scope=scope,
                content=r.content,
                timestamp=r.timestamp,
                role=role,
                metadata=md,
            ),
            provider_ctx,
        )

    # step3：合成 finish 对（末尾承载，spec §3.3 step3 + §3.5）
    # A1: 机会性取 _close_report 槽；槽空则用薄占位
    from ctx_weft.core.loop.steps.background_observe import (
        pop_close_report, register_close_synth, _replace_finish_report,
    )
    base = now_utc()
    tool_call_id = generate_id("tcall")
    outputs_text = _output_text(task.outputs) or ("(无最终产出)" if outcome == "fail" else "")

    report_prefix = "[outcome=fail] " if outcome == "fail" else ""
    # mem_content 格式为 "{outputs}\n\nProcess Report: {summary}" 或仅 "{summary}"
    _SEP = "\n\nProcess Report: "
    report_only = (
        mem_content.rsplit(_SEP, 1)[-1]
        if _SEP in mem_content
        else mem_content
    )

    await memory.ingest(
        MemoryEvent(
            type=MemoryEventType.AGENT_CONVERSATION_TURN,
            scope=scope,
            content="",
            timestamp=base,
            role="assistant",
            metadata={"origin_task_id": task.id, "parent_task_id": task.parent_task_id,
                      "tool_calls": [{
                          "id": tool_call_id,
                          "name": qualify("control:finish_task"),
                          "input": {"result": outputs_text},
                      }]},
        ),
        provider_ctx,
    )
    await memory.ingest(
        MemoryEvent(
            type=MemoryEventType.AGENT_CONVERSATION_TURN,
            scope=scope,
            content=f"{report_prefix}Process Report: {report_only}",
            timestamp=base,
            role="tool",
            metadata={"origin_task_id": task.id, "parent_task_id": task.parent_task_id,
                      "tool_call_id": tool_call_id},
        ),
        provider_ctx,
    )

    # A1: 机会性替换或登记异步替换（pop + register 之间无 await，关闭竞态窗口）
    bg_report = pop_close_report(task.id)
    if bg_report is not None:
        # background 已先完成（少见）→ 立即替换占位
        await _replace_finish_report(
            memory, provider_ctx, scope, task.id, tool_call_id, bg_report, outcome,
        )
    else:
        # background 尚未完成 → 登记待异步替换（sync，无 await）
        register_close_synth(task.id, tool_call_id, scope, outcome)


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

        # 1) 统一 close：bubble / 自身残留 / 软删自身对话 / GC 子树（spec 2026-06-23）。
        if terminal and mem_content:
            events.extend(await finalize_task_memory(
                ctx.memory, state, task, mem_content, outcome, ctx,
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
            task.process_report_at = now_utc()  # 落在本 attempt 之后、下一 attempt 之前 → 装配按时间戳归位
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


def _output_text(outputs: Any) -> str:
    """Extract the plain answer text from task.outputs (str or [{type:text,text:...}])."""
    if isinstance(outputs, list):
        return next(
            (p.get("text", "") for p in outputs if isinstance(p, dict) and p.get("type") == "text"),
            "",
        )
    if isinstance(outputs, str):
        return outputs
    return ""


def _build_memory_content(outputs: Any, summary: str) -> str:
    """合并 task outputs 和 observer summary，对齐 miniAgents _write_execution_memory。

    格式："{output_text}\\n\\nProcess Report: {summary}"
    只有 summary 时："{summary}"
    """
    parts = [p for p in [_output_text(outputs), summary] if p]
    return "\n\nProcess Report: ".join(parts)
