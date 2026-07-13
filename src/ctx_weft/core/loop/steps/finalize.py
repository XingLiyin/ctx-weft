"""FinalizeStep：task 收尾——写 memory + blackboard publish + 更新 task 状态。

miniAgents 对齐版：
- memory 内容 = task.outputs + "\\n\\nProcess Report: " + verdict.act_recap（合并写入）
- 新增 BLACKBOARD_PUBLISH：让父 agent 通过 recall_topic(task.id) 读到子任务结果
"""

from __future__ import annotations

import logging
from typing import Any

from ctx_weft.core.loop.driver import LoopContext, LoopState, Step, StepOutcome, make_event
from ctx_weft.core.events import EventType
from ctx_weft.core.utils import as_utc, content_to_text, estimate_tokens, generate_id, now_utc
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

# 长任务 close 时 supersede 的 task 层「末 raw 段」类型（保留 USER_PROMPT / TASK_COMPACT_SUMMARY 锚点）
_FINAL_RAW_TYPES = [
    MemoryEventType.LLM_RESPONSE,
    MemoryEventType.TOOL_INVOCATION,
    MemoryEventType.TOOL_RESULT,
]

# 同 agent 派发：派发对 tool 结果的静态文案（不含任何子任务结果，永不回填，spec 2026-06-30 §2.5）。
# 只写「任务已开始」的套话——子真实产出由内联胶囊 body + 嵌套 finish 对承载。
def _dispatch_ack(title: str) -> str:
    return f"Task '{title}' started."

# 派发框的叙事工具名（仅出现在重建历史的 tool_calls 里，非可调用能力）。
START_TASK_NAME = qualify("control:start_task")


# 归一可能 naive 的 datetime 为 aware(UTC)——事件重放 / DB 反序列化可能丢 tz，
# 比较前统一，避免 naive 与 aware 直接比较报 TypeError。统一实现见 utils.as_utc。
_as_utc = as_utc


async def _ensure_dispatch_frame(memory, parent_scope, task, ctx):
    """在 parent scope 铸一条 tool_call id==task.origin_tool_call_id 的 assistant 派发框，
    返回它 == 配对 tool result 应锚定的时间戳 = task 真正开始执行的时刻（started_at）。

    **框与 result 同锚 started_at**：二者共用同一时间戳 → 按 (timestamp, seq_no) 排序时严格相邻
    （框先写 seq 小、result 后写 seq 大），且落在「任务开始执行」这条时间线上（而非派发时刻）。
    started_at 晚于 actor 派发那一轮、早于子 body（body 由 driver 在启动后才 ingest USER_PROMPT），
    故「派发框 → Task started/result → 子 body → finish 对」顺序天然成立、胶囊连续。回退 created_at
    （历史/无 started_at）再回退 now，统一归一为 aware(UTC)。

    delegate_task 与 delegate_plan 子统一走此铸框路径（gateway 不再为 delegate_task eager 写框——
    eager 框只能带派发时刻，无法落在 started_at 时间线上，见 capability_gateway._record_invocation）。
    框的 tool name 取 task.origin_tool_name（保真）：delegate_task 子 = 真名 control__delegate_task
    （actor 确实调过）；delegate_plan 子 = None → 回退 START_TASK_NAME 叙事名（无 per-child 真实调用）。
    幂等：若同 id 的框已存在（终态 finalize 单入本不会重入，此为防御），直接返回 ts、不重复铸。
    origin_task_id 留父（delegate_task 的父 = 派发 task；delegate_plan 子 = 留 plan task）。
    """
    # 框与 result 的公共锚点：task 真正启动执行的时刻。归一为 aware(UTC)：started_at/created_at
    # 可能来自事件重放而为 naive（历史无 started_at 时回退 created_at 再回退 now）。
    ts = _as_utc(task.started_at or task.created_at or now_utc())
    existing = await memory.recall_recent(
        parent_scope, [MemoryEventType.AGENT_CONVERSATION_TURN], 2000, ctx.provider_ctx)
    frame = next(
        (r for r in existing
         if r.role == "assistant"
         and any(tc.get("id") == task.origin_tool_call_id
                 for tc in (r.metadata.get("tool_calls") or []))),
        None,
    )
    if frame is not None:
        return ts  # 幂等：框已铸（同锚 ts）→ 直接复用锚点，不重复铸框
    await memory.ingest(
        MemoryEvent(
            type=MemoryEventType.AGENT_CONVERSATION_TURN, scope=parent_scope,
            content="", timestamp=ts, role="assistant",
            metadata={"origin_task_id": task.parent_task_id,
                      "parent_task_id": task.parent_task_id,
                      "tool_calls": [{"id": task.origin_tool_call_id,
                                      "name": task.origin_tool_name or START_TASK_NAME,
                                      "input": {"title": task.title,
                                                "description": task.description or ""}}]},
        ),
        ctx.provider_ctx,
    )
    return ts


def _finish_tool_text(task_summary: str, act_recap: str, outcome: str) -> str:
    """finish 对 tool 槽内容 = task_summary（process report）。R2 兜底：空则退 act_recap，
    再空给占位。**不掺 outputs**——最终输出在 finish_task 的 result 入参，tool 槽不重复它。
    绝不返回空串（避免空 tool 回合 / 400）。"""
    for cand in (task_summary, act_recap):
        if cand and cand.strip():
            return cand
    return "(无最终产出)" if outcome == "fail" else "(本段无更多总结)"


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


async def finalize_task_memory(memory, state, task, mem_content: str, outcome: str, ctx,
                               *, act_recap: str, task_summary: str) -> list:
    """finish 时调用：算 short / descendants 后委派 _close_one。返回事件列表。

    task-resident（spec 2026-06-28）：short 不再 gate 合成/supersede——每个结束 task 都写
    finish 对、body 留 task 层。`short` 仅算出后透传给 _close_one（Task 2 用于 body raw-vs-压末段）。
    """
    descendants = _descendant_task_ids(task.id, ctx.task_manager)
    short = await _is_short_leaf(
        memory, state.scope, task, state.agent.loop_config, ctx, bool(descendants),
    )
    return await _close_one(
        memory, state, task, mem_content, outcome, ctx,
        short=short, act_recap=act_recap, task_summary=task_summary,
    )


async def _supersede_final_raw_segment(memory, scope, ctx) -> None:
    """长任务 close：supersede task 层末 raw 段（active LLM_RESPONSE/TOOL_INVOCATION/TOOL_RESULT），
    保留 USER_PROMPT + TASK_COMPACT_SUMMARY 锚点（spec 2026-06-28 §3.2）。

    中间段已在各自边界由后台 observe 折成 TASK_COMPACT_SUMMARY（折时 supersede 了对应 raw），
    故此刻 active 的 raw 即「末段」。末段已由 finish 对的 Process Report 承载（不变量 4）→
    直接 supersede、**不另产新 TASK_COMPACT_SUMMARY**（避免与 finish 对重复）。
    """
    records = await memory.recall_recent(scope, _FINAL_RAW_TYPES, 2000, ctx.provider_ctx)
    ids = [r.id for r in records]
    if ids:
        await memory.supersede(ids, ctx.provider_ctx)


async def _close_one(memory, state, task, mem_content: str, outcome: str, ctx,
                     *, short: bool, act_recap: str, task_summary: str) -> list:
    """close 主体（task-resident，spec 2026-06-28）：bubble / 写 finish 对（不镜像 body）。

    每个结束 task 无条件写 finish 对、body 留 task 层（不 GC 子树）。长任务额外 supersede 末 raw
    段（短任务留全 raw）——`short` 决定 body raw-vs-压末段（spec §3.2）。返回事件列表。
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
        parent_scope = MemoryScope(
            session_id=state.scope.session_id,
            task_id=task.parent_task_id,
            agent_id=task.creator_agent_id,
        )
        if cross_agent:
            # 跨 agent（spec 2026-06-28 §2.3）：dispatch result 写成 agent 层普通 conversation turn
            # （tool 回合），与 start_task / delegate 框靠 tool_call_id 配对、时间戳对齐保证相邻。
            report_prefix = "[outcome=fail] " if outcome == "fail" else ""
            frame_ts = await _ensure_dispatch_frame(memory, parent_scope, task, ctx)
            await memory.ingest(
                MemoryEvent(
                    type=MemoryEventType.AGENT_CONVERSATION_TURN,
                    scope=parent_scope,
                    content=f"{report_prefix}{mem_content}",
                    timestamp=frame_ts,
                    role="tool",
                    metadata={"origin_task_id": task.parent_task_id,
                              "tool_call_id": task.origin_tool_call_id},
                ),
                ctx.provider_ctx,
            )
            events.append(make_event(
                state, EventType.MEMORY_INGESTED,
                payload={"memory_event_type": MemoryEventType.AGENT_CONVERSATION_TURN.value,
                         "source": "dispatch_result", "content_length": len(mem_content)},
            ))
        elif same_agent:
            # 同 agent（spec 2026-06-30 §2.5）：确保/补铸派发框，写一条配对静态 tool result，
            # 时间戳对齐框 → 严格相邻、排在子 body 之前。子真实产出由内联胶囊 body + 嵌套 finish 对承载。
            frame_ts = await _ensure_dispatch_frame(memory, parent_scope, task, ctx)
            await memory.ingest(
                MemoryEvent(
                    type=MemoryEventType.AGENT_CONVERSATION_TURN, scope=parent_scope,
                    content=_dispatch_ack(task.title), timestamp=frame_ts, role="tool",
                    metadata={"origin_task_id": task.parent_task_id,
                              "tool_call_id": task.origin_tool_call_id},
                ),
                ctx.provider_ctx,
            )
            # 嵌套合成子自己的 finish 对（写进共享 agent scope，@close 时刻）
            await _synthesize_dispatch_pair(
                memory, parent_scope, task, act_recap, task_summary, outcome, ctx.provider_ctx)

    # 2) 自身 finish 对：own root（session 根或跨 agent 根）close 时无条件在 own scope 合成
    if is_own_root and mem_content:
        await _synthesize_dispatch_pair(memory, state.scope, task, act_recap, task_summary, outcome, ctx.provider_ctx)
        events.append(make_event(
            state, EventType.MEMORY_INGESTED,
            payload={"memory_event_type": MemoryEventType.AGENT_CONVERSATION_TURN.value,
                     "source": "root_finish_pair", "content_length": len(mem_content)},
        ))

    # task-resident（spec 2026-06-28 §3.2）：body 留 task 层、不 GC 子树。
    # 长任务额外 supersede 末 raw 段（保留 USER_PROMPT/TASK_COMPACT_SUMMARY 锚点）；短任务留全 raw。
    if not short:
        await _supersede_final_raw_segment(memory, state.scope, ctx)
    return events


async def _synthesize_dispatch_pair(memory, scope, task, act_recap: str, task_summary: str,
                                    outcome: str, provider_ctx) -> None:
    """close 合成 agent 层 finish 对（spec 2026-06-30 两段化）：
    assistant{content=act_recap + finish_task 调用} / tool{content=task_summary 综合总结}。
    own-root：占位先写，bg close observe 产新两段后经 _replace_finish_report 替换（A1）。"""
    from ctx_weft.core.loop.steps.background_observe import (
        pop_close_report, register_close_synth, _replace_finish_report,
    )
    base = now_utc()
    tool_call_id = generate_id("tcall")
    # 反转契约（spec 2026-07-01）：答复正文由「内联的 task 层 body / blackboard mem_content」承载，
    # 故 finish 对的 assistant 槽用 act_recap（过程复述，≠ 答复），避免与内联 body 的答复重复；
    # finish_task 退化为无参收尾标记（不再把答复塞进 input.result）。tool 槽 = task_summary（process report）。
    report_prefix = "[outcome=fail] " if outcome == "fail" else ""
    summary_text = _finish_tool_text(task_summary, act_recap, outcome)

    await memory.ingest(
        MemoryEvent(
            type=MemoryEventType.AGENT_CONVERSATION_TURN, scope=scope,
            content=act_recap, timestamp=base, role="assistant",
            metadata={"origin_task_id": task.id, "parent_task_id": task.parent_task_id,
                      "tool_calls": [{"id": tool_call_id,
                                      "name": qualify("control:finish_task"),
                                      "input": {}}]},
        ),
        provider_ctx,
    )
    await memory.ingest(
        MemoryEvent(
            type=MemoryEventType.AGENT_CONVERSATION_TURN, scope=scope,
            content=f"{report_prefix}{summary_text}", timestamp=base, role="tool",
            metadata={"origin_task_id": task.id, "parent_task_id": task.parent_task_id,
                      "tool_call_id": tool_call_id},
        ),
        provider_ctx,
    )

    bg = pop_close_report(task.id)
    if bg is not None:
        bg_recap, bg_summary = bg
        await _replace_finish_report(memory, provider_ctx, scope, task.id, tool_call_id,
                                     bg_recap, bg_summary, outcome)
    else:
        register_close_synth(task.id, tool_call_id, scope, outcome)


class FinalizeStep(Step):
    name = "finalize"

    async def execute(self, state: LoopState, ctx: LoopContext) -> StepOutcome:
        task = state.task
        verdict = state.verdict
        outcome = verdict.task_outcome if verdict else "fail"
        summary = verdict.act_recap if verdict else ""            # → task.process_report（success/fail finish 对；retry 不再写，进度由段摘要承载）
        task_summary = verdict.task_summary if verdict else ""    # → 汇报给 parent 的 process report
        events: list[Any] = []

        # retry 超过上限 → 降级 fail（不再重试）
        if outcome == "retry" and task.retry_count >= task.max_retries:
            outcome = "fail"
            task.status = "FAILED"
            task.observer_outcome = "fail"

        terminal = outcome in ("success", "fail")
        # 汇报给 parent（blackboard + cross_agent bubble）= 最终输出 + task_summary（process report 作用）；
        # task_summary 空时回退 act_recap。
        mem_content = _build_memory_content(task.outputs, task_summary or summary)

        # 1) 统一 close：bubble / 自身残留 / 软删自身对话 / GC 子树（spec 2026-06-23）。
        if terminal and mem_content:
            events.extend(await finalize_task_memory(
                ctx.memory, state, task, mem_content, outcome, ctx,
                act_recap=summary, task_summary=task_summary,
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
            # retry 反馈由 observe 前台段折写的 TASK_COMPACT_SUMMARY 承载（spec 2026-07-01 §3.1）；
            # 不再写 process_report/process_report_at（旧 Progress So Far 字段路径已废）。
            # 机械退出（max_turns/context_limit）也归到这里：重排再跑，受 max_retries 兜底。
            task.outputs = None
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
