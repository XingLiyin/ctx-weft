"""root 后台异步 observe：在交互/finish 段边界产段摘要并折 raw（spec §3.2）。

仿 recognize_intent 的 fire-and-forget：快照 state、create_task、track_background。
同一 task 至多一个在跑（_task_locks 串行），失败吞掉（降级 = 该段保 raw，spec §3.6）。
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import TYPE_CHECKING

from ctx_weft.core.loop.steps.compact import summarize_for_compact
from ctx_weft.protocols import MemoryEventType, MemoryLayer

if TYPE_CHECKING:
    from ctx_weft.core.loop.driver import LoopContext, LoopState

logger = logging.getLogger(__name__)

_task_locks: dict[str, asyncio.Lock] = {}
_task_pending: dict[str, asyncio.Task] = {}
_orphan_tasks: set[asyncio.Task] = set()


def _lock_for(task_id: str) -> asyncio.Lock:
    lock = _task_locks.get(task_id)
    if lock is None:
        lock = asyncio.Lock()
        _task_locks[task_id] = lock
    return lock


async def _run_background_observe(state: "LoopState", ctx: "LoopContext") -> None:
    async with _lock_for(state.task.id):
        try:
            summary = await summarize_for_compact(state, ctx)
            await ctx.memory.apply_compact(
                scope=state.scope,
                summary=summary or "[Context compacted]",
                keep_last=0,
                ctx=ctx.provider_ctx,
                layer=MemoryLayer.TASK,
                protect_types=(MemoryEventType.USER_PROMPT,),
            )
        except Exception:
            logger.exception("background observe failed (ignored); segment kept raw")


def launch_background_observe(state: "LoopState", ctx: "LoopContext") -> asyncio.Task:
    snapshot = dataclasses.replace(state)
    task = asyncio.create_task(_run_background_observe(snapshot, ctx))
    _task_pending[state.task.id] = task
    tm = getattr(ctx, "task_manager", None)
    if tm is not None and hasattr(tm, "track_background"):
        tm.track_background(task)
    else:
        _orphan_tasks.add(task)
        task.add_done_callback(_orphan_tasks.discard)
    task.add_done_callback(lambda _t, tid=state.task.id: _task_pending.pop(tid, None) and None)
    return task


async def await_pending_background_observe(task_id: str) -> None:
    """供 finalize 强一致：若该 task 有在跑的后台 observe，等它完成（spec §3.3 step 1）。"""
    pending = _task_pending.get(task_id)
    if pending is not None and not pending.done():
        await asyncio.shield(pending)
