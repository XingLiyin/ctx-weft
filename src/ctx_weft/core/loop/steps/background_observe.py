"""root 后台异步 observe：在交互/finish 段边界产段摘要并折 raw（spec §3.2）。

仿 recognize_intent 的 fire-and-forget：快照 state、create_task、track_background。
同一 task 至多一个在跑（_task_locks 串行），失败吞掉（降级 = 该段保 raw，spec §3.6）。

boundary 分流（Task 6）：
  - finish / normal → 结果落 _close_report 槽，不写 memory（finalize Task 8 取用）
  - 其他（interrupt、plain_text 等）→ apply_compact 写 TASK_COMPACT_SUMMARY
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import TYPE_CHECKING

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
                                 outcome: str) -> None:
    """supersede finish 对的 assistant + tool 两条占位，按新 act_recap / task_summary 重写。
    按 (tool_call_id + origin_task_id) 定位，不再靠 'Process Report:' 文本（spec 2026-06-30 §2.4）。"""
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

    report_prefix = "[outcome=fail] " if outcome == "fail" else ""
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
    from ctx_weft.core.loop.steps.observe import (
        BACKGROUND_OBSERVE_REACT_EVENTS, run_observe_react,
    )
    from ctx_weft.core.orchestrator.control_capability import BACKGROUND_PROCESS_REPORT_NAME

    async with _lock_for(state.task.id):
        try:
            agent = state.agent
            bound_caps = (
                ctx.capability_cache.get(agent.id)
                if ctx.capability_cache is not None and ctx.capability_cache.has_agent(agent.id)
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
            result, _ = await run_observe_react(
                state, ctx,
                system=prompt.system,
                messages=list(prompt.messages),
                tools=prompt.tools,
                request_id_prefix=f"bgobs_{state.task.id}",
                max_rounds=agent.loop_config.max_turns_per_observe,
                terminal_tool_name=BACKGROUND_PROCESS_REPORT_NAME,
                event_types=BACKGROUND_OBSERVE_REACT_EVENTS,  # 后台 LLM 交互发独立类型，host 决定不进前端
            )
            act_recap = (result.content if result else None) or "[Context compacted]"
            task_summary = (result.metadata or {}).get("task_summary", "") if result else ""
            if boundary in _CLOSE_BOUNDARIES:
                synth = pop_close_synth(state.task.id)  # sync check-and-clear（无 await）
                if synth is not None:
                    tool_call_id, scope, outcome = synth
                    await _replace_finish_report(
                        ctx.memory, ctx.provider_ctx, scope, state.task.id,
                        tool_call_id, act_recap, task_summary, outcome,
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
                    protect_types=(MemoryEventType.USER_PROMPT,),
                )
        except Exception:
            logger.exception("background observe failed (ignored); segment kept raw")


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
    """供 finalize 强一致：若该 task 有在跑的后台 observe，等它完成（spec §3.3 step 1）。"""
    pending = _task_pending.get(task_id)
    if pending is not None and not pending.done():
        await asyncio.shield(pending)
