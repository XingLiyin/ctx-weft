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

# close 路径结果槽：task_id → process_report（finalize Task 8 通过 pop_close_report 取用）
_close_report: dict[str, str] = {}

_CLOSE_BOUNDARIES = {"finish", "normal"}


def pop_close_report(task_id: str) -> str | None:
    """取走 close 路径产出的 process_report；不存在则返回 None。"""
    return _close_report.pop(task_id, None)


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
    from ctx_weft.core.loop.steps.observe import run_observe_react
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
                actor_transcript=state.transcript,
                extra={"observe_boundary": boundary},
            )
            prompt = await ctx.assembler.assemble(request)
            content, _ = await run_observe_react(
                state, ctx,
                system=prompt.system,
                messages=list(prompt.messages),
                tools=prompt.tools,
                request_id_prefix=f"bgobs_{state.task.id}",
                max_rounds=agent.loop_config.max_turns_per_observe,
                terminal_tool_name=BACKGROUND_PROCESS_REPORT_NAME,
            )
            report = content or "[Context compacted]"
            if boundary in _CLOSE_BOUNDARIES:
                _close_report[state.task.id] = report  # 不写 memory（不变量 3）
            else:
                await ctx.memory.apply_compact(
                    scope=state.scope,
                    summary=report,
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
