"""root 后台异步 observe：在交互/finish 段边界产段摘要并折 raw（spec §3.2）。

仿 recognize_intent 的 fire-and-forget：快照 state、create_task、track_background。
同一 task 至多一个在跑（_task_locks 串行），失败吞掉（降级 = 该段保 raw，spec §3.6）。

boundary 分流（Task 6）：
  - finish / normal → 结果落 _close_report 槽，不写 memory（finalize Task 8 取用）
  - 其他（interrupt、plain_text、dispatch 等）→ apply_compact 写 TASK_COMPACT_SUMMARY；
    但本段 active raw token ≤ short_segment_token_threshold 时**免折**（短段保 raw，
    不跑后台 LLM——「短 → 原文成胶囊」决策在段边界的延伸；raw 跨边界累积，超阈值再折）

崩溃恢复竞态（spec §5.1/§3.6；2026-07-16 起同进程内闭合）：`recover_session` 对一个
SUSPENDED-且-有待完成段 recap 的 task，会（a）经 TaskManager.restore 重排该 task 的新一轮
run，（b）经 `_relaunch_task_recap` 重跑被打断的段 recap。relaunch 先于 register_and_drain
发生，且 `_run_loop` 入口 await_pending_background_observe——新 run 开跑前必等 recap 完成，
两者不再并发写同一段。跨进程/其他极端时序仍是 best-effort：最坏该段保 raw，不影响正确性。
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import TYPE_CHECKING

from ctx_weft.core.events import EventType
from ctx_weft.core.loop.driver import make_event
from ctx_weft.core.loop.steps.observe import run_observe_react
from ctx_weft.core.utils import content_to_text, estimate_tokens
from ctx_weft.protocols import MemoryEventType, MemoryLayer

if TYPE_CHECKING:
    from ctx_weft.core.loop.driver import LoopContext, LoopState

logger = logging.getLogger(__name__)

_task_locks: dict[str, asyncio.Lock] = {}
_task_pending: dict[str, asyncio.Task] = {}
_orphan_tasks: set[asyncio.Task] = set()

# close 路径结果槽：task_id → (act_recap, task_summary)（finalize Task 8 通过 pop_close_report 取用）
_close_report: dict[str, tuple[str, str]] = {}

# close 路径合成槽：task_id → (tool_call_id, scope, outcome)
# finalize 先到时登记，bg 回调后替换 finish tool 记录
_close_synth: dict[str, tuple] = {}

_CLOSE_BOUNDARIES = {"finish", "normal"}

# 段边界折叠会 supersede 的 raw 类型（= apply_compact 的非保护类型；与 finalize._FINAL_RAW_TYPES
# 同构，本地定义避免与 finalize 交叉 import——finalize 已反向 import 本模块的 pop_close_report）
_SEGMENT_RAW_TYPES = [
    MemoryEventType.LLM_RESPONSE,
    MemoryEventType.TOOL_INVOCATION,
    MemoryEventType.TOOL_RESULT,
]


async def is_short_segment(state: "LoopState", ctx: "LoopContext") -> bool:
    """短段免折门：本段 active raw token ≤ short_segment_token_threshold？

    「短 → 原文成胶囊」决策（finalize._is_short_leaf）在段级的判定，
    `_run_background_observe`（interactive/interrupt 边界）与
    `observe._fold_retry_segment`（retry 段折）共用。配置缺失（手构 state /
    单测）→ False = 门关闭，照常折叠。
    """
    threshold = getattr(state.agent.loop_config, "short_segment_token_threshold", 0)
    if threshold <= 0:
        return False
    records = await ctx.memory.recall_recent(
        state.scope, _SEGMENT_RAW_TYPES, 2000, ctx.provider_ctx,
    )
    seg_text = " ".join(
        r.content if isinstance(r.content, str) else content_to_text(r.content)
        for r in records
    )
    return estimate_tokens(seg_text) <= threshold


def pop_close_report(task_id: str) -> tuple[str, str] | None:
    """取走 close 路径产出的 (act_recap, task_summary)；不存在则返回 None。"""
    return _close_report.pop(task_id, None)


def register_close_synth(task_id: str, tool_call_id: str, scope, outcome: str) -> None:
    """finalize 先到时登记：finish 对已合成，待 bg 回调替换 Process Report。"""
    _close_synth[task_id] = (tool_call_id, scope, outcome)


def pop_close_synth(task_id: str) -> tuple | None:
    """bg 回调取走合成登记；不存在则返回 None。"""
    return _close_synth.pop(task_id, None)


async def _replace_finish_report(memory, provider_ctx, scope, task_id: str,
                                 tool_call_id: str, act_recap: str, task_summary: str,
                                 outcome: str, title: str) -> None:
    """supersede finish 对的 assistant + tool 两条占位，按新 act_recap / task_summary 重写。
    按 (tool_call_id + origin_task_id) 定位，不再靠 'Process Report:' 文本（spec 2026-06-30 §2.4）。

    title：归属 task 的标题，用于重建 tool 槽的 `[task: …]` 前缀（与 finalize 合成占位时同源，
    见 finalize._finish_report_prefix）。本函数整条重写 tool 槽，不传就会把占位里的归属标记抹掉。"""
    from ctx_weft.core.loop.steps.finalize import _finish_report_prefix
    from ctx_weft.protocols import MemoryEvent, MemoryEventType
    from ctx_weft.protocols.capability import qualify

    turns = await memory.recall_recent(scope, [MemoryEventType.AGENT_CONVERSATION_TURN], 500, provider_ctx)
    asst = [r for r in turns
            if r.role == "assistant" and r.metadata.get("origin_task_id") == task_id
            and any(tc.get("id") == tool_call_id for tc in (r.metadata.get("tool_calls") or []))]
    tool = [r for r in turns
            if r.role == "tool" and r.metadata.get("origin_task_id") == task_id
            and r.metadata.get("tool_call_id") == tool_call_id]
    if not asst and not tool:
        logger.warning("A1 _replace_finish_report: no finish 对 for task=%s tcid=%s; skip (best-effort)",
                       task_id, tool_call_id)
        return

    anchor = (asst or tool)[0]
    ts = anchor.timestamp
    parent_task_id = anchor.metadata.get("parent_task_id")
    tool_calls = (asst[0].metadata.get("tool_calls") if asst
                  else [{"id": tool_call_id, "name": qualify("control:finish_task"), "input": {}}])

    await memory.supersede([r.id for r in (*asst, *tool)], provider_ctx)

    report_prefix = _finish_report_prefix(title, outcome)
    summary_text = task_summary if (task_summary and task_summary.strip()) else act_recap
    # finish 对 assistant 槽 = act_recap（过程复述，≠ 答复）：答复由内联 body / blackboard 承载，
    # 避免与之重复（spec 2026-07-01 反转契约）。
    await memory.ingest(MemoryEvent(
        type=MemoryEventType.AGENT_CONVERSATION_TURN, scope=scope,
        content=act_recap, timestamp=ts, role="assistant",
        metadata={"origin_task_id": task_id, "parent_task_id": parent_task_id, "tool_calls": tool_calls},
    ), provider_ctx)
    await memory.ingest(MemoryEvent(
        type=MemoryEventType.AGENT_CONVERSATION_TURN, scope=scope,
        content=f"{report_prefix}{summary_text}", timestamp=ts, role="tool",
        metadata={"origin_task_id": task_id, "parent_task_id": parent_task_id, "tool_call_id": tool_call_id},
    ), provider_ctx)


def _clear_pending(t: asyncio.Task, tid: str) -> None:
    """Compare-and-clear: only remove _task_pending[tid] if it still refers to this task."""
    if _task_pending.get(tid) is t:
        del _task_pending[tid]


def _lock_for(task_id: str) -> asyncio.Lock:
    lock = _task_locks.get(task_id)
    if lock is None:
        lock = asyncio.Lock()
        _task_locks[task_id] = lock
    return lock


async def _run_background_observe(state: "LoopState", ctx: "LoopContext", boundary: str) -> None:
    from ctx_weft.core.assembler import ContextRequest
    from ctx_weft.core.loop.steps.observe import BACKGROUND_OBSERVE_REACT_EVENTS
    from ctx_weft.core.orchestrator.control_capability import BACKGROUND_PROCESS_REPORT_NAME

    await ctx.event_bus.emit(make_event(
        state, EventType.TASK_RECAP_STARTED,
        payload={"task_id": state.task.id, "boundary": boundary, "agent_id": state.agent.id},
    ))
    try:
        async with _lock_for(state.task.id):
            # 重跑幂等护栏（恢复重跑时才生效）：非 close 边界若该段已无 active raw，说明上次
            # 崩溃前已折叠（raw 被 supersede），再折会产冗余胶囊 → 跳过（finally 仍发 DONE）。
            # 正常运行时该段刚产生 raw、计数 > 0，护栏为 no-op。
            if boundary not in _CLOSE_BOUNDARIES:
                n_raw = await ctx.memory.count_recent(
                    state.scope, [MemoryEventType.LLM_RESPONSE], ctx.provider_ctx,
                )
                if n_raw == 0:
                    logger.info(
                        "task recap re-fold guard: segment already folded (task=%s); skip",
                        state.task.id,
                    )
                    return
                # 短段免折（is_short_segment）：本段 active raw 低于阈值时跳过折叠——花一次
                # 后台 LLM 调用换一段常比原文还长的摘要不划算。跳过 = 段保 raw，与观察失败的
                # 降级同语义；raw 跨边界累积，下次边界重估的是累积后的 active raw，超阈值即
                # 一并折叠。
                if await is_short_segment(state, ctx):
                    logger.info(
                        "short segment kept raw (task=%s boundary=%s); skip fold",
                        state.task.id, boundary,
                    )
                    return
            try:
                agent = state.agent
                # 不能像 observe._llm_observe 那样用 has_agent() 短路：background observe 是
                # fire-and-forget（launch_background_observe → asyncio.create_task），常在
                # 本 run 的 _run_loop finally evict(agent.id) 之后才真正跑到这——per-agent 快照
                # 已被逐出，has_agent 为 False。控制工具（collect_process_report 等）是 session
                # 全局区（register_global，不随 evict 逐出），故这里应始终尝试 .get()（内部自动
                # 合并全局区），不能因 per-agent 快照缺失就整体清零、连全局控制工具也丢了。
                bound_caps = (
                    ctx.capability_cache.get(agent.id)
                    if ctx.capability_cache is not None
                    else []
                )
                request = ContextRequest(
                    purpose="background_observe",
                    scope=state.scope,
                    task=state.task,
                    agent=agent,
                    session=state.session,
                    template=state.extra.get("template"),
                    bound_capabilities=bound_caps,
                    extra={"observe_boundary": boundary},
                )
                prompt = await ctx.assembler.assemble(request)
                result, last_text = await run_observe_react(
                    state, ctx,
                    system=prompt.system,
                    messages=list(prompt.messages),
                    tools=prompt.tools,
                    request_id_prefix=f"bgobs_{state.task.id}",
                    max_rounds=agent.loop_config.max_turns_per_observe,
                    terminal_tool_name=BACKGROUND_PROCESS_REPORT_NAME,
                    event_types=BACKGROUND_OBSERVE_REACT_EVENTS,  # 后台 LLM 交互发独立类型，host 决定不进前端
                )
                # 报告取值：terminal 工具产出 → 纯文本复述兜底（observer 把复述写成正文而没调工具）。
                act_recap = ((result.content if result else "") or last_text or "").strip()
                task_summary = (result.metadata or {}).get("task_summary", "") if result else ""
                if not act_recap:
                    # 无任何可用报告：与异常路径同语义——段保 raw，不写占位摘要、不动 finish 对。
                    if boundary in _CLOSE_BOUNDARIES:
                        pop_close_synth(state.task.id)  # 弹掉登记防泄漏；finalize 占位 finish 对保持原样
                    logger.warning(
                        "background observe produced no usable report (task=%s boundary=%s); "
                        "segment kept raw", state.task.id, boundary,
                    )
                    return
                if boundary in _CLOSE_BOUNDARIES:
                    synth = pop_close_synth(state.task.id)  # sync check-and-clear（无 await）
                    if synth is not None:
                        tool_call_id, scope, outcome = synth
                        await _replace_finish_report(
                            ctx.memory, ctx.provider_ctx, scope, state.task.id,
                            tool_call_id, act_recap, task_summary, outcome,
                            state.task.title or "",
                        )
                    else:
                        # root 的 finish/normal 是终结点（单次 close）：槽写一次弹一次，不存在
                        # 跨 rerun 乱序覆盖（retry 仅在机械退出时产生，不经此路径）。
                        _close_report[state.task.id] = (act_recap, task_summary)  # 不写 memory（不变量 3）
                else:
                    await ctx.memory.apply_compact(
                        scope=state.scope,
                        summary=act_recap,
                        keep_last=0,
                        ctx=ctx.provider_ctx,
                        layer=MemoryLayer.TASK,
                        # 同时护 TASK_COMPACT_SUMMARY：多段交互（多轮 plain_text）各产一段胶囊须累积，
                        # 否则后一段折会 supersede 前一段摘要（前段丢失）、且新摘要锚到 UP 前 1μs 抢占前段
                        # 位置。与 observe._fold_retry_segment 的 protect_types 一致。
                        protect_types=(MemoryEventType.USER_PROMPT,
                                       MemoryEventType.TASK_COMPACT_SUMMARY),
                    )
            except Exception:
                # close 边界防泄漏：finalize 可能已 register_close_synth，本次失败后永远无人
                # 消费（task_id 唯一 + close 单入口），弹掉——与「无可用报告」分支对称。
                # 已知残余窗口（接受，不另引状态同步）：bg 比 finalize 先死时登记发生在 pop
                # 之后，仍漏一条；对称地，bg 先写 _close_report 而 finalize 异常中止也漏。
                # 两者触发概率与单次代价（几百字节/次）都低一个量级。
                if boundary in _CLOSE_BOUNDARIES:
                    pop_close_synth(state.task.id)
                logger.exception("background observe failed (ignored); segment kept raw")
    finally:
        await ctx.event_bus.emit(make_event(
            state, EventType.TASK_RECAP_DONE, payload={"task_id": state.task.id},
        ))


def launch_background_observe(
    state: "LoopState", ctx: "LoopContext", *, boundary: str
) -> asyncio.Task:
    snapshot = dataclasses.replace(state)
    task = asyncio.create_task(_run_background_observe(snapshot, ctx, boundary))
    _task_pending[state.task.id] = task
    tm = getattr(ctx, "task_manager", None)
    if tm is not None and hasattr(tm, "track_background"):
        tm.track_background(task)
    else:
        _orphan_tasks.add(task)
        task.add_done_callback(_orphan_tasks.discard)
    task.add_done_callback(lambda t, tid=state.task.id: _clear_pending(t, tid))
    return task


async def await_pending_background_observe(task_id: str) -> None:
    """等该 task 在途后台 observe 完成（强一致）。调用点：`_run_loop` 入口（覆盖常规
    `prepare` 与 `reconcile` dangling tool_call 重放两条 resume 路径，`runtime.py`）、
    用户冷应答注入（`_inject_user_reply`，`runtime.py`）。"""
    pending = _task_pending.get(task_id)
    if pending is not None and not pending.done():
        await asyncio.shield(pending)
