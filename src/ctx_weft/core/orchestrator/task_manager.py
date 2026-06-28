"""TaskManager：监听事件 + 调度 TaskQueue + parent resume 逻辑。

Phase 4 §4.2 + §4.7。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from ctx_weft.core.utils import generate_id, now_utc

from ctx_weft.core.events.types import EVENT_TYPES, Event, EventType
from ctx_weft.core.orchestrator.task_queue import QueueEntry, TaskQueue
from ctx_weft.core.state.models import (
    CompactTaskSettings,
    MetadataFillerTaskSettings,
    Session,
    Task,
    TaskStatus,
)
from ctx_weft.core.utils import generate_id, now_utc

if TYPE_CHECKING:
    from ctx_weft.core.events.bus import EventBus

logger = logging.getLogger(__name__)

# Callback type: (session_id, task_id) → None
TaskRunner = Callable[[str, str], Coroutine[Any, Any, None]]

# 默认值；实际值由 host 经 RuntimeConfig → TaskManager 构造参数注入。
_DEFAULT_MAX_RETRIES    = 3
_DEFAULT_MAX_CONCURRENT = 4


class TaskManager:
    """Manages a per-session TaskQueue and drives task execution.

    The caller (SessionManager / CtxWeftRuntime) must:
    1. Register a task_runner callback (runs a single task).
    2. Call push_task() to add tasks.
    3. Call tick() or rely on event-driven draining.
    """

    def __init__(
        self,
        session_id: str,
        max_concurrent: int | None = None,
        task_max_retries: int | None = None,
        event_bus: "EventBus | None" = None,
    ) -> None:
        self._session_id = session_id
        self._max_concurrent = max_concurrent if max_concurrent is not None else _DEFAULT_MAX_CONCURRENT
        self._task_max_retries = task_max_retries if task_max_retries is not None else _DEFAULT_MAX_RETRIES
        self._queue: TaskQueue = TaskQueue()
        self._cancelled: bool = False
        self._tasks: dict[str, Task] = {}
        # 同一轮（一次 task run）内 delegate_task / delegate_plan / replan 先投这里，
        # runner 正常返回后由 _flush_staged 统一入队，实现「同批次 FIFO」。
        # key = 正在运行的 task_id（即被 spawn 子任务的 parent_task_id）。
        self._staged: dict[str, list[tuple[Task, list[str] | None, str | None]]] = {}
        self._parent_map: dict[str, str] = {}  # child_task_id → parent_task_id
        self._children_of: dict[str, set[str]] = {}  # parent_task_id → set[child_task_ids]
        self._runner: TaskRunner | None = None
        self._running_tasks: set[str] = set()
        self._lock = asyncio.Lock()
        self._session: Session | None = None  # 注入后供 failure_counter 维护使用
        self._event_bus: "EventBus | None" = event_bus
        self._on_session_done: Callable[[], Coroutine[Any, Any, None]] | None = None
        self._on_session_idle: Callable[[], Coroutine[Any, Any, None]] | None = None
        self._session_done_fired: bool = False
        self._background_asyncio_tasks: set[asyncio.Task] = set()

    def track_background(self, t: "asyncio.Task") -> None:
        """Track a fire-and-forget background coroutine so the session awaits it before close."""
        self._background_asyncio_tasks.add(t)
        t.add_done_callback(self._background_asyncio_tasks.discard)

    def set_runner(self, runner: TaskRunner) -> None:
        self._runner = runner

    def set_session(self, session: Session) -> None:
        """注入 Session 对象，供 failure_counter 维护使用。"""
        self._session = session

    def set_session_done_callback(self, cb: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """session 真正结束（所有任务处理完、无重试待执行）时调用的回调。只调用一次。"""
        self._on_session_done = cb
        self._session_done_fired = False

    def set_session_idle_callback(self, cb: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """session 进入**空闲挂起**（有任务 park/suspend 且无其它在跑任务、非终结）时调用的回调。

        区别于 `_on_session_done`：那是终结回调（FINISHED/FAILED/CANCELED，回收全部 per-session 状态）；
        这是「会话暂停、待续接」的信号，供 runtime 回收按 run 计、续跑会重建的控制信号（pause/cancel token）。
        可多次触发（每次 park 一次）；回调须幂等。
        """
        self._on_session_idle = cb

    def register_task(self, task: Task) -> None:
        self._tasks[task.id] = task

    def restore(
        self,
        all_tasks: list[Task],
        terminal_ids: set[str],
        parked_task_ids: set[str] | None = None,
    ) -> None:
        """Rebuild task registry and re-queue resumable tasks after a crash.

        ``parked_task_ids``: tasks with an unresolved pending HITL (spec/07 §9.1) —
        SUSPENDED but must STAY parked (not re-queued) until ``/answer`` resumes them.
        Empty/None → legacy behavior.

        compact / recognize_intent are no longer scheduled as Tasks; any such obsolete
        task found in a replayed event stream is skipped (never re-queued). Recovery is
        condition-based.
        """
        _TERMINAL = {"FINISHED", "FAILED", "CANCELED"}
        parked = parked_task_ids or set()

        for t in all_tasks:
            self._tasks[t.id] = t
            if t.parent_task_id:
                self._parent_map[t.id] = t.parent_task_id
                self._children_of.setdefault(t.parent_task_id, set()).add(t.id)

        for tid in terminal_ids:
            self._queue._completed.add(tid)

        for t in all_tasks:
            if t.status in _TERMINAL:
                continue
            if isinstance(t.settings, (CompactTaskSettings, MetadataFillerTaskSettings)):
                continue  # obsolete ephemeral helpers — never re-scheduled (recovery is condition-based)
            if t.status == "SUSPENDED":
                if t.id in parked:
                    continue                       # HITL-park：保持挂起，不入队（spec/07 §9.1）
                children = self._children_of.get(t.id, set())
                if all(cid in terminal_ids for cid in children):
                    t.status = "PENDING"
                    self._queue.push(QueueEntry(
                        task_id=t.id, session_id=self._session_id, priority=t.priority,
                    ))
            else:
                t.status = "PENDING"
                t.retry_count = 0
                blocked = {dep for dep in (t.dag_deps or []) if dep not in terminal_ids}
                self._queue.push(QueueEntry(
                    task_id=t.id, session_id=self._session_id,
                    priority=t.priority, blocked_by=blocked,
                ))

    async def push_task(
        self,
        task: Task,
        blocked_by: list[str] | None = None,
        parent_task_id: str | None = None,
    ) -> None:
        # 统一用 TaskManager 级别的 max_retries，覆盖 Task 模型的硬编码默认值
        task.max_retries = self._task_max_retries
        self._tasks[task.id] = task
        if parent_task_id:
            self._parent_map[task.id] = parent_task_id
            self._children_of.setdefault(parent_task_id, set()).add(task.id)
        if blocked_by:
            task.dag_deps = list(blocked_by)

        entry = QueueEntry(
            task_id=task.id,
            session_id=self._session_id,
            priority=task.priority,
            blocked_by=set(blocked_by or []),
        )
        self._queue.push(entry)
        logger.debug("TaskManager.push_task: %s blocked_by=%s", task.id, blocked_by)
        # 创建即落盘：未跑过的排队 / blocked task 也进事件日志，
        # 崩溃恢复时 restore() 可由 dag_deps 重建依赖链，无需父任务重新 spawn。
        # push_task 是唯一的「新建」路径（retry / resume / active 重排都走 _queue.push），
        # 故此处恰好 emit 一次。
        await self._emit(EventType.TASK_CREATED, task_id=task.id, payload=_task_payload(task))

    def stage_task(
        self,
        task: Task,
        blocked_by: list[str] | None = None,
        parent_task_id: str | None = None,
    ) -> None:
        """把子任务投入当前 run 的缓冲区，延迟到 _flush_staged 才真正入队。

        与 push_task 同签名；调度顺序问题（同批次 FIFO）由 _flush_staged 统一处理。
        缓冲按 parent_task_id 分桶，避免 max_concurrent>1 时并发 run 的 spawn 串台。
        """
        key = parent_task_id or ""
        self._staged.setdefault(key, []).append((task, blocked_by, parent_task_id))
        logger.debug("TaskManager.stage_task: %s under parent=%s", task.id, key)

    def detach_staged(self, from_parent_id: str, to_parent_id: str | None) -> None:
        """把 from_parent_id 本轮 staged 的子任务改投到 to_parent_id 名下（独立后续）。

        用于 finish_task 与 delegate_* / replan 同批出现时：当前 task 收尾，被派发任务
        不再做它的阻塞子任务，而改挂到它的 parent（当前是 root 则为 None→顶层）独立调度。

        只改写 tuple 的 parent_task_id（驱动 _flush_staged 时 push_task 的归属）与
        task.parent_task_id，**不搬桶**——桶 key 仍是当前运行 task 的 id，_flush_staged
        才能在本 run 结束时正常弹桶入队。plan 内部兄弟间的 blocked_by 顺序链原样保留。
        """
        bucket = self._staged.get(from_parent_id)
        if not bucket:
            return
        rewritten: list[tuple[Task, list[str] | None, str | None]] = []
        for task, blocked_by, _ in bucket:
            task.parent_task_id = to_parent_id
            rewritten.append((task, blocked_by, to_parent_id))
        self._staged[from_parent_id] = rewritten
        logger.debug(
            "TaskManager.detach_staged: %d staged task(s) re-parented %s → %s",
            len(rewritten), from_parent_id, to_parent_id,
        )

    async def _flush_staged(self, task_id: str) -> None:
        """run 正常结束时把缓冲区的子任务入队。

        队列是 LIFO（pop 取末尾），因此按投入顺序 reversed 后再 push，
        使同一批次内的子任务 pop 出来是 FIFO（投入顺序）；整批位于栈顶，
        相对更早批次仍是 LIFO（深度优先）。
        """
        staged = self._staged.pop(task_id, None)
        if not staged:
            return
        for task, blocked_by, parent_task_id in reversed(staged):
            await self.push_task(task, blocked_by=blocked_by, parent_task_id=parent_task_id)

    async def drain(self) -> None:
        """Pop and run tasks until queue is empty or max_concurrent reached."""
        if self._runner is None:
            raise RuntimeError("No task runner registered")

        if self._cancelled:
            return

        while True:
            async with self._lock:
                if len(self._running_tasks) >= self._max_concurrent:
                    break
                entry = self._queue.pop()
                if entry is None:
                    break
                self._running_tasks.add(entry.task_id)

            asyncio.create_task(self._run_task(entry.task_id))

    async def _run_task(self, task_id: str) -> None:
        assert self._runner is not None
        task = self._tasks.get(task_id)
        if task is not None:
            task.status = "ACTIVE"
            task.actor_done = False
            # 本 task 开始执行（创建已在 push_task 落盘）。每次首段都发，
            # 体现 retry / resume 后的「重新 active」；reducer 将其映射为 ACTIVE。
            await self._emit(
                EventType.TASK_STARTED,
                task_id=task_id,
                payload={"assigned_agent_id": task.assigned_agent_id or ""},
            )
        try:
            try:
                await self._runner(self._session_id, task_id)
            except BaseException:
                # cancel(CancelledError) / 异常退出：丢弃未 flush 的缓冲，
                # 防止泄漏或日后 resume 时被误入队。再交回外层原有处理。
                self._staged.pop(task_id, None)
                raise
            # runner 正常跑完才把本轮 spawn 的子任务入队（“一轮跑完之后 push”）
            await self._flush_staged(task_id)
            task = self._tasks.get(task_id)
            if task and task.status == "SUSPENDED":
                # 子任务已由 control tool 推入队列；parent 等待所有子任务完成后
                # 由 _try_resume_parent 重新入队，此处只需移出 running set 并 drain。
                # 注：LLM 故障中断（_run_loop except LLMOutageError）也置 SUSPENDED 到此挂起，无子任务，待 /resume 由 restore 重排。
                async with self._lock:
                    self._running_tasks.discard(task_id)
                    self._queue.unmark_running(task_id)
                await self.drain()
                # 整个会话因 park/suspend 进入空闲（无在跑任务、无待派子任务）→ 通知 runtime 回收
                # 按 run 计的控制信号。注意是 is_done（而非"本 task 挂起"）：父等子时子仍在跑，
                # is_done 为 False、不触发，待子完成 resume 父；只有全会话静止才算空闲挂起。
                if self.is_done():
                    await self._fire_session_idle()
                return
            if task and task.status == "PENDING":
                # Observer 判 retry（本轮未完成，含机械退出）：重新入队（retry_count 已在 finalize +1）。
                async with self._lock:
                    self._running_tasks.discard(task_id)
                    self._queue.unmark_running(task_id)
                    self._queue.push(QueueEntry(task_id=task_id, session_id=self._session_id))
                await self.drain()
                return
            # 使用 task 的实际终态，避免将 FAILED/CANCELED 覆盖为 FINISHED
            final_status: TaskStatus = "FINISHED"
            if task and task.status in ("FAILED", "CANCELED"):
                final_status = task.status
            await self.on_task_finished(task_id, status=final_status)
        except Exception as e:
            if getattr(e, "retriable", False):
                logger.warning("Task %s failed (retriable): %s", task_id, e)
            else:
                logger.exception("Task %s failed: %s", task_id, e)
            await self._handle_task_failure(task_id, error=str(e), exc=e)

    async def reopen_chain(self, head_id: str, reason: str = "") -> bool:
        """Reopen a FINISHED sub-task **and force-reopen its plan successors**.

        When a plan step is reopened, every later step in the same plan chain
        (tasks that carry `head_id` in their `tracking_task_ids`) is invalidated too,
        because they were produced against the now-rejected result. This re-queues the
        whole chain in plan order, re-establishing `blocked_by` so each step waits for
        its predecessor's corrected result (delivered via the blackboard subscription).

        - head: reopened with the direct revision `reason`.
        - successors: reopened with an "upstream revised" note instead, pointing them at
          head's updated result.

        Returns True if the head was reopened.
        """
        head = self._tasks.get(head_id)
        if head is None or head.status != "FINISHED":
            return False

        _epoch = datetime.min.replace(tzinfo=timezone.utc)
        successors = sorted(
            (
                t for t in self._tasks.values()
                if t.session_id == self._session_id
                and t.status == "FINISHED"
                and head_id in (t.tracking_task_ids or [])
            ),
            key=lambda t: t.created_at or _epoch,
        )
        head_title = head.title or head_id

        prev_id: str | None = None
        for t in [head, *successors]:
            blocked = None if prev_id is None else [prev_id]
            upstream = None if t.id == head_id else (head_title, reason)
            await self.reopen_task(t.id, reason, blocked_by=blocked, upstream=upstream)
            prev_id = t.id
        return True

    async def reopen_task(
        self,
        task_id: str,
        reason: str = "",
        *,
        blocked_by: list[str] | None = None,
        upstream: tuple[str, str] | None = None,
    ) -> bool:
        """Re-queue a previously FINISHED task (observer review 'reopen').

        Resets the task to a clean PENDING state and rewrites `user_prompt` =
        original prompt + previous output + revision reason, so the re-run's prompt
        (task_spec block + re-ingested USER_PROMPT memory) tells the actor what was
        produced before and what to fix. The rewrite is always based on
        `original_user_prompt` so repeated reopens don't accumulate. Clearing
        `outputs` re-arms the success-guardrail so a no-op cannot be re-marked
        FINISHED with stale output.

        `blocked_by` gates re-execution until the given tasks complete (used by
        reopen_chain to keep plan order). `upstream=(head_title, head_reason)` marks a
        cascade-reopened successor: instead of a direct revision note it gets an
        "upstream task revised" instruction pointing at the predecessor's updated result
        (delivered via the blackboard subscription once the predecessor re-finishes).

        Emits TASK_REQUEUED (projections/SSE + restart-safe). Scheduling happens on
        the next drain (after the current run returns); does not drain re-entrantly.
        Returns True if the task was re-queued.
        """
        task = self._tasks.get(task_id)
        if task is None or task.status != "FINISHED":
            return False

        # base = 首次执行的原始 prompt（首次 reopen 时快照下来）
        if task.original_user_prompt is None:
            task.original_user_prompt = task.user_prompt if isinstance(task.user_prompt, str) else ""
        base_prompt = task.original_user_prompt

        prev_output = _outputs_to_text(task.outputs) or (task.process_report or "")
        parts = [p for p in [base_prompt] if p]
        if prev_output:
            parts.append(f"## Previous attempt (rejected)\n{prev_output}")
        if upstream is not None:
            head_title, head_reason = upstream
            parts.append(
                f"## Upstream task revised\n"
                f"Predecessor '{head_title}' was reopened (reason: {head_reason}). "
                f"Its updated result is shown under \"Upstream task results\" above. "
                f"Redo this task based on the updated result."
            )
        elif reason:
            parts.append(f"## Revision required\n{reason}")
        new_prompt = "\n\n".join(parts) if parts else base_prompt

        async with self._lock:
            self._queue.unmark_completed(task_id)
            self._queue.unmark_running(task_id)
            task.status = "PENDING"
            task.actor_done = False
            task.retry_count = 0
            task.outputs = None
            task.finished_at = None
            task.user_prompt = new_prompt
            task.user_prompt_in_memory = False  # let the driver re-ingest the revised prompt
            if blocked_by is not None:
                task.dag_deps = list(blocked_by)  # restart 时由 dag_deps 重建依赖链
            self._queue.push(QueueEntry(
                task_id=task_id, session_id=self._session_id, priority=task.priority,
                blocked_by=set(blocked_by or []),
            ))
        # 把改写后的 prompt 一并落进事件，使崩溃恢复（event replay）能重建修订后的 user_prompt。
        await self._emit(
            EventType.TASK_REQUEUED,
            task_id=task_id,
            payload={
                "reason": "observer_review_reopen",
                "user_prompt": new_prompt,
                "original_user_prompt": task.original_user_prompt,
            },
        )
        logger.info("TaskManager.reopen_task: re-queued %s", task_id)
        return True

    async def _handle_task_failure(
        self, task_id: str, error: str = "", exc: BaseException | None = None
    ) -> None:
        """task 失败时：先尝试 retry，超出 max_retries 才真正失败。

        exc.retriable=False（如 LLMCallError 401 认证失败、transport 重试耗尽）时跳过重试直接失败，
        避免对永久性错误做无效重试。
        """
        # 不可重试的错误（如认证失败），直接判定失败
        if exc is not None and not getattr(exc, "retriable", True):
            logger.warning(
                "Task %s non-retriable error (%s), failing immediately: %s",
                task_id, type(exc).__name__, error,
            )
            await self._emit_task_failed(task_id, error)
            await self.on_task_finished(task_id, status="FAILED")
            return

        task = self._tasks.get(task_id)
        if task is not None and task.retry_count < task.max_retries:
            task.retry_count += 1
            task.status = "PENDING"
            task.error = error
            logger.info(
                "Task %s retrying (%d/%d): %s",
                task_id, task.retry_count, task.max_retries, error,
            )
            async with self._lock:
                self._running_tasks.discard(task_id)
                self._queue.unmark_running(task_id)  # 清除 queue._running，使 pop() 能再次调度
                entry = QueueEntry(task_id=task_id, session_id=self._session_id)
                self._queue.push(entry)
            # 重排落事件：使投影从 ACTIVE 回到 PENDING；进程在重试间隙崩溃时
            # restore 据 PENDING 重排（而非把停留 ACTIVE 的任务误当成可恢复后重跑）。
            await self._emit(EventType.TASK_REQUEUED, task_id=task_id, payload={
                "reason": "run_failure_retry",
                "retry_count": task.retry_count,
            })
            await self.drain()
        else:
            await self._emit_task_failed(task_id, error)
            await self.on_task_finished(task_id, status="FAILED")

    async def _emit_task_failed(self, task_id: str, error: str) -> None:
        """运行层失败（异常退出，未经 observer/FinalizeStep）补发 TaskFailed。

        task 状态投影只认 TASK_* 事件（TASK_STATUS_BY_EVENT）；observer 判失败由
        FinalizeStep 发 TaskFailed，而运行崩溃这条路（_run_loop 抛异常 → 本方法）此前
        只发 RunFinished、不发 TaskFailed，导致任务在投影里停留 ACTIVE、被 restore 误复活。
        本方法独属运行崩溃路径（observer 判失败正常返回、不进 _handle_task_failure），故不与
        FinalizeStep 重复。"""
        task = self._tasks.get(task_id)
        await self._emit(EventType.TASK_FAILED, task_id=task_id, payload={
            "error_code": "TASK_FAILED_AT_RUN",
            "error_message": error,
            "retry_count": task.retry_count if task else 0,
        })

    async def on_task_finished(self, task_id: str, status: TaskStatus) -> None:
        async with self._lock:
            self._running_tasks.discard(task_id)
            if status == "FAILED":
                self._queue.mark_failed(task_id)
            else:
                self._queue.mark_complete(task_id)

            # Update task object
            task = self._tasks.get(task_id)
            if task:
                task.status = status
                task.finished_at = now_utc()

        # ── failure_counter 维护 ──────────────────────────────────────────────
        if self._session is not None:
            if status == "FAILED":
                self._session.failure_counter += 1
                threshold = self._session.failure_threshold
                if threshold > 0 and self._session.failure_counter >= threshold:
                    logger.warning(
                        "Session %s failure_counter=%d reached threshold=%d → FAILED",
                        self._session_id, self._session.failure_counter, threshold,
                    )
                    self._session.status = "FAILED"
                    await self._emit(EventType.FAILURE_THRESHOLD_HIT, payload={
                        "failure_counter": self._session.failure_counter,
                        "threshold": threshold,
                    })
                    await self._emit(EventType.SESSION_STATUS_CHANGED, payload={"new_status": "FAILED"})
                    await self._fire_session_done()
                    return
            elif status == "FINISHED":
                self._session.failure_counter = 0  # 成功时重置
            elif status == "CANCELED":
                # 用户主动中断：标记 session 为 CANCELED，防止 is_done() 误判为 SUCCEEDED
                self._session.status = "CANCELED"

        # Try to resume parent
        await self._try_resume_parent(task_id)
        # Drain next
        await self.drain()

        # 若 queue 已空且无任务在运行，通知 session 真正结束
        # （有重试时 drain() 会把重试任务入队，is_done() 为 False，不触发）
        if self.is_done():
            # 立即更新 session 终态并通知前端，SSE 保持开放直到后台协程完成
            if self._session is not None and self._session.status not in ("FAILED", "CANCELED"):
                # failure_counter > 0 表示本轮有任务失败（成功时会被重置为 0）
                self._session.status = "FAILED" if self._session.failure_counter > 0 else "SUCCEEDED"
            final_status = self._session.status if self._session else "SUCCEEDED"
            await self._emit(EventType.SESSION_STATUS_CHANGED, payload={"new_status": final_status})
            await self._fire_session_done()

    async def cancel_all(self, *, reason: str = "") -> None:
        """硬取消整条 session 链：清空 pending 队列并标 CANCELED，会话置 CANCELED。

        在途 task 不在此处理——由 CancelToken → act checkpoint → CancelledError →
        _run_loop 置该 task CANCELED → on_task_finished（其 drain() 被 _cancelled 守卫挡住）。
        memory 不触碰（保留）。
        """
        self._cancelled = True
        async with self._lock:
            pending = self._queue.drain_pending()
        for tid in pending:
            t = self._tasks.get(tid)
            if t is not None:
                t.status = "CANCELED"
                t.finished_at = now_utc()
            await self._emit(EventType.TASK_CANCELED, task_id=tid, payload={"reason": reason})
        if self._session is not None:
            self._session.status = "CANCELED"
            await self._emit(EventType.SESSION_STATUS_CHANGED, payload={"new_status": "CANCELED"})

    async def _emit(
        self,
        event_type: EventType,
        task_id: str | None = None,
        payload: dict | None = None,
    ) -> None:
        """构造并发出一个 session/task 级别的事件（无 LoopState）。"""
        if self._event_bus is None:
            return
        # 与 make_event 一致的白名单校验：直接构造 Event 的路径此前会绕过它。
        if event_type not in EVENT_TYPES:
            raise ValueError(f"Unknown event type: {event_type}; not in EVENT_TYPES")
        tenant_id = self._session.tenant_id if self._session else "default"
        await self._event_bus.emit(Event(
            id=generate_id("evt"),
            run_id=None,
            sequence=0,
            session_id=self._session_id,
            type=event_type,
            timestamp=now_utc(),
            tenant_id=tenant_id,
            task_id=task_id,
            payload=payload or {},
        ))

    async def _fire_session_idle(self) -> None:
        """会话空闲挂起（park/suspend，非终结）：通知 runtime 回收 per-run 控制信号。不发事件。"""
        if self._on_session_idle is not None:
            try:
                await self._on_session_idle()
            except Exception:
                logger.exception("TaskManager: session_idle callback failed")

    async def _fire_session_done(self) -> None:
        """触发 session 结束：发 SessionFinished 事件 + 可选回调，保证只执行一次。"""
        if self._session_done_fired:
            return
        self._session_done_fired = True
        # 等待所有后台协程完成，确保 RecognizeIntent 等事件全部 emit 后再关闭 SSE 流
        if self._background_asyncio_tasks:
            await asyncio.gather(*list(self._background_asyncio_tasks), return_exceptions=True)
        if self._event_bus is not None:
            final_status = self._session.status if self._session else "FINISHED"
            tenant_id = self._session.tenant_id if self._session else "default"
            await self._event_bus.emit(Event(
                id=generate_id("evt"),
                run_id=None,
                sequence=0,
                session_id=self._session_id,
                type=EventType.SESSION_FINISHED,
                timestamp=now_utc(),
                tenant_id=tenant_id,
                payload={"final_status": final_status},
            ))
        if self._on_session_done is not None:
            try:
                await self._on_session_done()
            except Exception:
                logger.exception("TaskManager: session_done callback failed")

    async def _try_resume_parent(self, finished_task_id: str) -> None:
        parent_id = self._parent_map.get(finished_task_id)
        if parent_id is None:
            return

        siblings = self._children_of.get(parent_id, set())
        all_done = all(
            self._tasks.get(tid, Task(id="x", session_id="", status="PENDING")).status
            in ("FINISHED", "FAILED", "CANCELED")
            for tid in siblings
        )
        if all_done:
            logger.info("All children of %s done, resuming parent", parent_id)
            parent_task = self._tasks.get(parent_id)
            if parent_task and parent_task.status == "SUSPENDED":
                parent_task.status = "ACTIVE"
                entry = QueueEntry(
                    task_id=parent_id,
                    session_id=self._session_id,
                )
                self._queue.push(entry)
                await self.drain()
                await self._emit(EventType.TASK_RESUMED, task_id=parent_id)

    def get_task(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def children_of(self, task_id: str) -> set[str]:
        """该 task 已派生的子任务 id 集合（用于 blackboard 订阅）。"""
        return set(self._children_of.get(task_id, set()))

    def all_tasks(self) -> list[Task]:
        return list(self._tasks.values())

    def is_done(self) -> bool:
        """True when queue is empty and nothing is running."""
        return not self._queue.has_pending() and not self._running_tasks


def _outputs_to_text(outputs: Any) -> str:
    """把 task.outputs（str 或 ContentPart 列表）渲染成纯文本，供 reopen prompt 复用。"""
    if isinstance(outputs, str):
        return outputs
    if isinstance(outputs, list):
        return "\n".join(
            p.get("text", "")
            for p in outputs
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def _task_payload(task: Task) -> dict:
    """TaskCreated 事件的 payload，供 sessions.py translate_event 构建前端 task 对象。"""
    import dataclasses
    settings_d = dataclasses.asdict(task.settings)
    settings_d["_type"] = type(task.settings).__name__
    ts = task.created_at.isoformat() if task.created_at else ""
    return {
        "task": {
            "id": task.id,
            "session_id": task.session_id,
            "status": task.status,
            "title": task.title or "",
            "description": task.description or "",
            "creator_agent_id": task.creator_agent_id or "",
            "assigned_agent_id": task.assigned_agent_id or "",
            "parent_task_id": task.parent_task_id or "",
            "user_prompt": task.user_prompt or "",
            "priority": task.priority,
            "max_retries": task.max_retries,
            "timeout_ms": task.timeout_ms,
            "dag_deps": task.dag_deps,
            "interaction_mode": task.interaction_mode,
            "settings": settings_d,
            "result": None,
            "outputs": {},
            "error": None,
            "created_at": ts,
            "updated_at": ts,
        }
    }
