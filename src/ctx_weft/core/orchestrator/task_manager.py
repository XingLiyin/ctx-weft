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
    NormalTaskSettings,
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
        # 同一轮（一次 task run）内 delegate_task / delegate_plan 先投这里，
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
        # 归属权谓词：runtime 注入，返回本 TM 是否仍是该 session 的当前 owner。
        # None = 不受管（永远视为 current，保持旧行为）。被同 session 上更新的 TM
        # 顶替后返回 False → 迟到的收尾变 no-op（不发 SessionFinished、不 _release_session）。
        self._is_current: Callable[[], bool] | None = None
        # 该 session 是否仍有未决 pending HITL —— runtime 注入（查 HitlManager）。完成判定据此：
        # 有未决 HITL 的 parked 任务时，会话是"空闲等应答"而非"完成"，绝不发 SESSION_FINISHED
        # 把 parked 任务孤立（真相以 pending-HITL 为准，spec/07 §9.1）。None = 退回旧行为。
        self._has_pending_hitl: Callable[[], bool] | None = None

    def track_background(self, t: "asyncio.Task") -> None:
        """Track a fire-and-forget background coroutine so the session awaits it before close."""
        self._background_asyncio_tasks.add(t)
        t.add_done_callback(self._background_asyncio_tasks.discard)

    def set_runner(self, runner: TaskRunner) -> None:
        self._runner = runner

    def set_is_current(self, predicate: "Callable[[], bool]") -> None:
        """注入归属权谓词：本 TM 是否仍是该 session 的当前 owner（见 `_is_current`）。"""
        self._is_current = predicate

    def set_has_pending_hitl(self, predicate: "Callable[[], bool]") -> None:
        """注入"该 session 是否仍有未决 pending HITL"谓词（runtime 查 HitlManager）。"""
        self._has_pending_hitl = predicate

    def set_session(self, session: Session) -> None:
        """注入 Session 对象，供 failure_counter 维护使用。"""
        self._session = session

    @property
    def session(self) -> "Session | None":
        """注入的 Session 对象（复用路径据它读/写本轮 llm 参数——model=会话状态）。"""
        return self._session

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
            # HITL-park：有未决 HITL → 保持挂起、不入队，**不论 ACTIVE 还是 SUSPENDED**。
            # 审批热等的任务恒为 ACTIVE（不会走 SUSPENDED 分支）；park 判据是"有无未决 HITL"
            # （parked_task_ids，源自 fold_pending_hitl），而非 task.status（spec/07 §9.1，缺陷 A）。
            if t.id in parked:
                continue
            if t.status == "SUSPENDED":
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

        用于 finish_task 与 delegate_* 同批出现时：当前 task 收尾，被派发任务
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

    def _effective_agent(self, task: "Task | None") -> str:
        """任务实际执行所在的 agent id —— 同 agent 串行判定的键。

        - subagent 任务：每次实例化独立 agent（assigned 未定时用 task.id 造唯一 token），
          彼此永不冲突，跨 subagent 并行度完全保留。
        - 其余（非 subagent 任务）：`assigned_agent_id or creator_agent_id or root` —— 非 subagent
          任务在**创建者**的 agent scope 上跑（延续创建者对话）；未派发（assigned 空）时以 creator
          预测该 scope。creator 也空（初始 root task 已预 assign 到 root，故不走此兜底）才退 root，
          三者皆空再退 per-task token，避免把"未知 agent"误并成一桶而过度串行。须与 runtime._resolve
          的 `assigned or creator or root` 保持一致，否则串行键与真实执行 scope 漂移。
        """
        if task is None:
            return ""
        s = task.settings
        if isinstance(s, NormalTaskSettings) and s.use_subagent:
            return task.assigned_agent_id or f"__sub__{task.id}"
        root = self._session.root_agent_id if self._session else ""
        return task.assigned_agent_id or task.creator_agent_id or root or f"__task__{task.id}"

    async def drain(self) -> None:
        """Pop and run tasks until queue is empty or max_concurrent reached.

        同 agent 不并发：跳过"目标 agent 正忙（已有同 agent 任务在跑）"的队列条目，
        它们留在队列里，等该 agent 空闲（某任务完成 → on_task_finished → 再 drain）时被选中。
        """
        if self._runner is None:
            raise RuntimeError("No task runner registered")

        if self._cancelled:
            return

        while True:
            # 被同一 session 上更新的 TM 顶替（recover_session 覆盖了 _task_managers 映射）→
            # 立即停止派发，无声（不发事件、不改状态）。避免重叠 resume 下两套 drain 并行派发
            # 同一批任务；在跑协程照旧靠 _fire_session_done 处的 _is_current 收敛（spec/07 §9）。
            if self._is_current is not None and not self._is_current():
                return
            async with self._lock:
                if len(self._running_tasks) >= self._max_concurrent:
                    break
                busy_agents = {
                    self._effective_agent(self._tasks.get(tid))
                    for tid in self._running_tasks
                }
                entry = self._queue.pop(
                    skip=lambda e: self._effective_agent(self._tasks.get(e.task_id)) in busy_agents
                )
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
            # TASK_STARTED 由 runner（_make_task_runner 的 run_task）在 _resolve 之后发一条——那时
            # assigned_agent_id 才是真正执行的 agent（reducer 只落非空 id；见 spec/07）。这里**不再**自发，
            # 否则与 runner 双发（每 task 两条 TaskStarted）。契约：TaskManager 的 runner 必须发 TASK_STARTED
            # （生产恒为 _make_task_runner；仅测试用 stub runner 时需自行补发）。每次派发（含 retry/resume）
            # runner 都会被调用一次 → 一条 TaskStarted。
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
            # queue 空、无在跑任务；但若仍有未决 HITL 的 parked 任务，会话是"空闲等应答"而非"完成"
            # ——绝不能发 SESSION_FINISHED 把 parked 任务孤立（真相以 pending-HITL 为准，spec/07 §9.1）。
            if self._has_pending_hitl is not None and self._has_pending_hitl():
                await self._fire_session_idle()
            else:
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
        # 归属权判定放在 gather **之后**：顶替可能发生在等待后台任务期间（旧 TM 的
        # background observe 拖久了，用户已开启下一轮、新 TM 接管了 session）。此时本 TM
        # 已非 owner → 收尾变 no-op，绝不发 SessionFinished、绝不触发 _release_session，
        # 否则会冲掉新一轮的 HITL 挂起态、把任务卡在 ACTIVE。
        if self._is_current is not None and not self._is_current():
            logger.info("TaskManager(%s): superseded during session-done; skip SessionFinished + callback",
                        self._session_id)
            return
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

        # 判定 all_done → 翻转 SUSPENDED→ACTIVE → push 三步必须在同一临界区内完成：
        # 否则两个（同 agent）子任务并发完成时会各自读到 all_done=True + status==SUSPENDED，
        # 双双 push/resume 父任务（父在同一 scope 上并发跑两遍，污染 memory）。drain 留到锁外。
        resumed = False
        async with self._lock:
            siblings = self._children_of.get(parent_id, set())
            # 空集守卫：无已登记子任务时绝不 resume（all([]) 恒为 True 的 vacuous-truth 防御）。
            all_done = bool(siblings) and all(
                (self._tasks[tid].status if tid in self._tasks else "PENDING")
                in ("FINISHED", "FAILED", "CANCELED")
                for tid in siblings
            )
            if all_done:
                parent_task = self._tasks.get(parent_id)
                if parent_task and parent_task.status == "SUSPENDED":
                    parent_task.status = "ACTIVE"
                    self._queue.push(QueueEntry(
                        task_id=parent_id,
                        session_id=self._session_id,
                    ))
                    resumed = True

        if resumed:
            logger.info("All children of %s done, resuming parent", parent_id)
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

    def resume_task(self, task_id: str) -> None:
        """重排一个被 HITL 应答唤醒的 task：置 PENDING 并入队，供**复用活 owner**的就地续跑路径。

        不重建 TM——直接把该 task 塞回本 owner 的队列。已终结/在跑/已在队列的任务不重复入队。
        wait_for_user 冷应答已由 `_inject_user_reply` 置 PENDING，这里补入队；approval 走此路径重排后
        由 reconcile 重放 dangling tool_call。
        """
        t = self._tasks.get(task_id)
        if t is None or t.status in ("FINISHED", "FAILED", "CANCELED"):
            return
        if task_id in self._running_tasks:
            return
        if any(e.task_id == task_id for e in self._queue.peek_all()):
            return
        t.status = "PENDING"
        self._queue.push(QueueEntry(
            task_id=task_id, session_id=self._session_id, priority=t.priority,
        ))

    def is_alive(self) -> bool:
        """本 TM 是否仍在驱动该 session（未终结、且仍是当前 owner）。

        供 recover_session 判断"是否有活 TM 正在跑"，以决定新 TM 是否要跳过其在跑任务。
        """
        if self._session_done_fired:
            return False
        return self._is_current is None or self._is_current()

    def running_task_ids(self) -> set[str]:
        """当前正在执行（已派发、_run_task 未返回）的 task id 集合。"""
        return set(self._running_tasks)


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
