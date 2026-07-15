"""TaskManager：监听事件 + 调度 TaskQueue + parent resume 逻辑。

Phase 4 §4.2 + §4.7。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from ctx_weft.core.utils import as_utc, generate_id, now_utc

from ctx_weft.core.events.types import EVENT_TYPES, Event, EventType
from ctx_weft.core.orchestrator.task_queue import QueueEntry, TaskQueue
from ctx_weft.core.orchestrator.task_runner import AgentBinding, TaskRunner, effective_agent_id
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
        # 派发后登记的「真实执行 agent id」（task_id → binding.agent_id）——
        # 同 agent 串行判定对在跑任务用真值，只有队列候选才走 effective_agent_id 预测。
        self._running_agents: dict[str, str] = {}
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
        # pause 弃子窗口标记（runtime.pause_session 置位、_on_idle/_release 复位）：
        # 置位期间任务取消不改 session 状态、run 收尾 staged 直接丢弃。
        self._pause_abandon = False
        # ── 熔断真终结（failure threshold trip）状态 ──────────────────────────────
        # 幂等闩：trip 后在途任务再失败会重进 FAILED 分支，没有它会重复清场 + 重复发事件。
        self._threshold_tripped: bool = False
        # (title, reason) 随 failure_counter 同步积累（FAILED 追加、FINISHED 清空）；
        # 供 FAILURE_THRESHOLD_HIT payload 与 threshold_finalizer 引用。跨崩溃恢复不重建，接受。
        self._recent_failures: list[tuple[str, str]] = []
        # 三个 trip 序列的注入点（接线方式镜像 set_has_pending_hitl）：None = 该副作用跳过，
        # trip 序列本身永远不因缺注入而崩溃。runtime 侧实现见 Task 10。
        self._cancel_pending_hitl: Callable[[], Coroutine[Any, Any, None]] | None = None
        self._cancel_inflight: Callable[[str], bool] | None = None
        self._threshold_finalizer: (
            Callable[["Task | None", list[Task], list[tuple[str, str]]], Coroutine[Any, Any, None]] | None
        ) = None
        # 统一取消胶囊闭合（Task 14）：cancel_all / 熔断清场（已启动挂起排队） / 在途协作取消 funnel
        # 三处调用点共用同一注入点。None-tolerant：缺注入时三处调用点自身各自跳过、不崩溃。
        self._cancel_finalizer: (
            Callable[[list[Task], str], Coroutine[Any, Any, None]] | None
        ) = None

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

    def set_cancel_pending_hitl(self, cb: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """注入"取消该 session 所有未决 pending HITL"回调（runtime 侧遍历 HitlManager.cancel）。

        trip 序列第 3 步 best-effort 调用；HitlCancelled 需全部先于会话终态发出。
        """
        self._cancel_pending_hitl = cb

    def set_cancel_inflight(self, cb: Callable[[str], bool]) -> None:
        """注入"对指定在途 task 发协作取消信号"回调（runtime 侧查 _run_tokens 发 cancel）。

        只发信号不代表任务立即终结——该任务的 TASK_CANCELED（非 root）由 _run_loop
        退出路径发；root 则由 trip 序列自己先标 FAILED（顺序见 _trip_failure_threshold）。
        """
        self._cancel_inflight = cb

    def set_threshold_finalizer(
        self,
        cb: Callable[["Task | None", list[Task], list[tuple[str, str]]], Coroutine[Any, Any, None]],
    ) -> None:
        """注入熔断收尾回调：(root_we_failed_and_started|None, ack_tasks, failures) -> None。

        trip 序列第 7 步内联 await（不是后台甩），保证 memory 落盘发生在 SESSION_FINISHED
        （SSE 关闭）之前；异常只记日志不阻断终结。
        """
        self._threshold_finalizer = cb

    def set_cancel_finalizer(
        self, cb: Callable[[list[Task], str], Coroutine[Any, Any, None]],
    ) -> None:
        """注入统一取消胶囊闭合回调：(tasks, reason) -> None（Task 14）。

        调用点：`cancel_all`（reason="user_cancel"）、`_trip_failure_threshold` 清场步骤对
        已启动的挂起/排队任务（reason="failure_threshold"）、`on_task_finished` 的 CANCELED
        分支（在途协作取消 funnel，reason 取 task.error 回退通用文案）。异常记日志不阻断。
        """
        self._cancel_finalizer = cb

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
                    t.retry_count = 0  # 崩溃挂起带着耗尽的计数；恢复重跑从零重计
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
        if self._pause_abandon or self._cancelled:
            # pause 弃子窗口 / 硬取消（含熔断 trip 封闸）：本轮 staged 的子任务直接丢弃
            # （push 时才发 TASK_CREATED，无投影残留），防止清队后又有漏网新任务入队被派发——
            # _cancelled 分支专堵在途 run 收尾迟到的 staged 子任务变成新的搁浅任务。
            logger.info("staged tasks of %s dropped (pause_abandon=%s cancelled=%s): %d",
                        task_id, self._pause_abandon, self._cancelled, len(staged))
            return
        for task, blocked_by, parent_task_id in reversed(staged):
            await self.push_task(task, blocked_by=blocked_by, parent_task_id=parent_task_id)

    def _effective_agent(self, task: "Task | None") -> str:
        """任务实际执行所在的 agent id——同 agent 串行判定的键（单一真相见 effective_agent_id）。"""
        root = self._session.root_agent_id if self._session else ""
        return effective_agent_id(task, root)

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
                    self._running_agents.get(tid) or self._effective_agent(self._tasks.get(tid))
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

        # ── 阶段 1：装配（assemble）────────────────────────────────────────
        # 失败发生在 TASK_STARTED 之前 → 投影不会出现幽灵 ACTIVE；与执行失败
        # 共用重试路径，但 TASK_REQUEUED.reason=assembly_failure 可区分。
        try:
            binding = await self._runner.assemble(task_id)
        except Exception as e:
            logger.exception("Task %s assembly failed: %s", task_id, e)
            await self._handle_task_failure(
                task_id, error=str(e), exc=e, reason="assembly_failure",
            )
            return
        if binding is None:
            # task 已不存在（装配空转）：镜像旧行为（runner 首行 get_task None 即返回）
            await self.on_task_finished(task_id, status="FINISHED")
            return

        # 回填「真正用于执行的 agent id」+ 真实启动时刻；TASK_STARTED 由 TM 发，
        # 每次派发（含 retry/resume）恰好一条（reducer 只落非空 id；见 spec/07）。
        if task is not None:
            task.assigned_agent_id = binding.agent_id
            task.started_at = now_utc()
        self._running_agents[task_id] = binding.agent_id
        await self._emit(EventType.TASK_STARTED, task_id=task_id,
                         payload={"assigned_agent_id": binding.agent_id})

        # ── 阶段 2：执行（execute）────────────────────────────────────────
        try:
            try:
                await self._runner.execute(binding, task_id)
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
                    self._running_agents.pop(task_id, None)
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
                    self._running_agents.pop(task_id, None)
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
            key=lambda t: as_utc(t.created_at) if t.created_at else _epoch,
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
                f"Its updated result appears in the conversation above. "
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
        self, task_id: str, error: str = "", exc: BaseException | None = None,
        reason: str = "run_failure_retry",
    ) -> None:
        """运行层失败：先尝试自动 retry，耗尽或不可重试 → 挂起等恢复（绝不落终态 FAILED）。

        运行层崩溃（异常退出，未经 observer/FinalizeStep）是**可恢复中断**，不是任务失败：
        真失败只有 observer 判 fail 一条路（FinalizeStep 闭合胶囊、发 TaskFailed、回传父亲）。
        exc.retriable=False（如 LLMCallError 401 认证失败、CONTEXT_OVERFLOW）时跳过重试直接挂起，
        避免对确定性错误做无效重试。
        """
        # 不可重试的错误（如认证失败 / 上下文溢出），不重试、直接挂起等恢复
        if exc is not None and not getattr(exc, "retriable", True):
            logger.warning(
                "Task %s non-retriable error (%s), suspending for recovery: %s",
                task_id, type(exc).__name__, error,
            )
            await self._suspend_task_interrupted(task_id, error, exc)
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
                self._running_agents.pop(task_id, None)
                self._queue.unmark_running(task_id)  # 清除 queue._running，使 pop() 能再次调度
                entry = QueueEntry(task_id=task_id, session_id=self._session_id)
                self._queue.push(entry)
            # 重排落事件：使投影从 ACTIVE 回到 PENDING；进程在重试间隙崩溃时
            # restore 据 PENDING 重排（而非把停留 ACTIVE 的任务误当成可恢复后重跑）。
            await self._emit(EventType.TASK_REQUEUED, task_id=task_id, payload={
                "reason": reason,
                "retry_count": task.retry_count,
            })
            await self.drain()
        else:
            await self._suspend_task_interrupted(task_id, error, exc)

    async def _suspend_task_interrupted(
        self, task_id: str, error: str, exc: BaseException | None,
    ) -> None:
        """运行层崩溃的终局：挂起等 /resume，**不是失败**。

        置 SUSPENDED（非终态）+ 发 TASK_SUSPENDED——投影只认 TASK_* 事件
        （TASK_STATUS_BY_EVENT），不发则任务停留 ACTIVE、restore 语义错位（此前
        由已删除的 _emit_task_failed 发 TaskFailed 兜这一点，现由本事件顶上）。
        再发 SessionStatusChanged(INTERRUPTED)（形状对齐 runtime._emit_session_interrupted，
        reason=错误码——如 CONTEXT_OVERFLOW，host 据此提示换更大窗口的模型恢复）。
        不发 TASK_FAILED、不增 failure_counter、不闭合胶囊：真失败只有 observer 判 fail
        一条路。恢复由 /resume → restore() 据非终态重排（重排时 retry_count 归零）。
        """
        task = self._tasks.get(task_id)
        error_code = (getattr(exc, "code", None)
                      or (type(exc).__name__ if exc is not None else "RUN_CRASH"))
        if task is not None:
            task.status = "SUSPENDED"
            task.error = error
            task.error_code = error_code
        async with self._lock:
            self._running_tasks.discard(task_id)
            self._running_agents.pop(task_id, None)
            self._queue.unmark_running(task_id)
        if self._session is not None and self._session.status == "RUNNING":
            self._session.status = "INTERRUPTED"
        await self._emit(EventType.TASK_SUSPENDED, task_id=task_id, payload={
            "reason": "run_crash",
            "error_code": error_code,
            "error_message": error,
            "retry_count": task.retry_count if task else 0,
        })
        await self._emit(EventType.SESSION_STATUS_CHANGED, payload={
            "new_status": "INTERRUPTED",
            "reason": error_code,
        })
        # 其它 agent 的排队任务照常派发；全会话静止则通知 runtime 回收 per-run 控制信号
        await self.drain()
        if self.is_done():
            await self._fire_session_idle()

    async def on_task_finished(self, task_id: str, status: TaskStatus) -> None:
        async with self._lock:
            self._running_tasks.discard(task_id)
            self._running_agents.pop(task_id, None)
            if status == "FAILED":
                self._queue.mark_failed(task_id)
            else:
                self._queue.mark_complete(task_id)

            # Update task object
            task = self._tasks.get(task_id)
            if task:
                task.status = status
                task.finished_at = now_utc()

        # ── 取消胶囊闭合 funnel（Task 14）───────────────────────────────────────
        # 在途协作取消的任务：发信号时（cancel_all 的 CancelToken / 熔断的 cancel_inflight）
        # 只做了 ack 替换（幂等自愈），finish 对要等这里——终态真正坐实（_run_loop 退出把
        # task.status 置 CANCELED）——才补写。正常收尾的任务走 FinalizeStep，永不落到这个分支
        # （status 只会是 FINISHED/FAILED，与本 if 互斥）。未真正 start 过的任务（started_at 为
        # 空）不会有派发框/own scope 可闭，交由 synthesize_cancel_closure 的 find-only 兜底判定
        # 即可，这里额外用 started_at 提前短路只是省一次无意义调用。
        if status == "CANCELED" and task is not None and task.started_at \
                and self._cancel_finalizer is not None:
            try:
                await self._cancel_finalizer([task], task.error or "cancelled")
            except Exception:
                logger.exception("TaskManager: cancel_finalizer callback failed for %s", task_id)

        # ── failure_counter 维护 ──────────────────────────────────────────────
        if self._session is not None:
            if status == "FAILED":
                self._session.failure_counter += 1
                self._recent_failures.append((
                    (task.title if task and task.title else task_id),
                    ((task.error or task.process_report or "") if task else "")[:200],
                ))
                threshold = self._session.failure_threshold
                if (
                    threshold > 0
                    and self._session.failure_counter >= threshold
                    and not self._threshold_tripped
                ):
                    await self._trip_failure_threshold()
                    return
            elif status == "FINISHED":
                self._session.failure_counter = 0  # 成功时重置
                self._recent_failures.clear()  # 随 counter 同步清空
            elif status == "CANCELED":
                # 用户主动中断：标记 session 为 CANCELED，防止 is_done() 误判为 SUCCEEDED。
                # pause 弃子（_pause_abandon）除外：连带取消不定会话去向，由 root park 决定。
                # _threshold_tripped 除外：熔断已判会话 FAILED，在途任务迟到的协作取消收尾
                # 绝不能把终态盖回 CANCELED（trip 序列已先手，见 _trip_failure_threshold）。
                if not self._pause_abandon and not self._threshold_tripped:
                    self._session.status = "CANCELED"

        # Try to resume parent
        await self._try_resume_parent(task_id)
        # Drain next
        await self.drain()

        # 若 queue 已空且无任务在运行，通知 session 真正结束
        # （有重试时 drain() 会把重试任务入队，is_done() 为 False，不触发）
        if self.is_done():
            # 会话已终结（如熔断 trip 已发 SESSION_FINISHED）：在途/迟到的收尾路径重入此块
            # 绝不能重复发 SESSION_STATUS_CHANGED——_fire_session_done 内部虽已幂等，但它
            # 之前的 emit 语句不受它保护，须在此前置拦下。
            if self._session_done_fired:
                return
            # 被顶替旧 TM 的迟到收尾不得代表会话发终态/空闲信号——新 owner 的状态才是真相。
            # runtime 侧回调本就 compare-and-check，这里连事件（stale SESSION_STATUS_CHANGED /
            # SESSION_FINISHED）也一并静默，避免污染事件流的 host 显示与重放。
            if self._is_current is not None and not self._is_current():
                return
            # queue 空、无在跑任务；但若仍有未决 HITL 的 parked 任务，会话是"空闲等应答"而非"完成"
            # ——绝不能发 SESSION_FINISHED 把 parked 任务孤立（真相以 pending-HITL 为准，spec/07 §9.1）。
            if self._has_pending_hitl is not None and self._has_pending_hitl():
                await self._fire_session_idle()
            elif any(t.status == "SUSPENDED" for t in self._tasks.values()):
                # 崩溃/LLM 故障挂起（INTERRUPTED）的任务在等 /resume：会话是"中断待恢复"
                # 而非"完成"——绝不发 SESSION_FINISHED 把挂起任务孤立（与 pending-HITL 同理）。
                # 合法的"父等子"SUSPENDED 到不了这里：子未终态时 is_done() 为 False；
                # 子全终态时父已在上方 _try_resume_parent 重排回队列（不再 SUSPENDED）。
                await self._fire_session_idle()
            else:
                # 立即更新 session 终态并通知前端，SSE 保持开放直到后台协程完成
                if self._session is not None and self._session.status not in ("FAILED", "CANCELED"):
                    # failure_counter > 0 表示本轮有任务失败（成功时会被重置为 0）
                    self._session.status = "FAILED" if self._session.failure_counter > 0 else "SUCCEEDED"
                final_status = self._session.status if self._session else "SUCCEEDED"
                await self._emit(EventType.SESSION_STATUS_CHANGED, payload={"new_status": final_status})
                await self._fire_session_done()

    async def _trip_failure_threshold(self) -> None:
        """连败达阈值：真终结——清场 + root 判死 + 会话终态，而非裸终结。

        从 on_task_finished 的 FAILED 分支进入（其 `not _threshold_tripped` 前置保证只进一次）。
        步骤对应机制设计「trip 序列」1-8；顺序是定案，改动前请对照 task-9-brief.md。
        """
        # 1) 幂等闩置位；_cancelled 封闸——drain 守卫白拿，_flush_staged 的
        #    `_pause_abandon or _cancelled` 丢弃条件也据此堵住在途 run 迟到的 staged 子任务。
        self._threshold_tripped = True
        self._cancelled = True
        threshold = self._session.failure_threshold if self._session else 0
        counter = self._session.failure_counter if self._session else 0
        logger.warning(
            "Session %s failure_counter=%d reached threshold=%d → 熔断真终结",
            self._session_id, counter, threshold,
        )

        # 2) FAILURE_THRESHOLD_HIT，payload 带本轮已知连败清单
        await self._emit(EventType.FAILURE_THRESHOLD_HIT, payload={
            "failure_counter": counter,
            "threshold": threshold,
            "failures": [{"title": title, "reason": reason} for title, reason in self._recent_failures],
        })

        # 3) 取消该 session 所有未决 pending HITL（best-effort）：HitlCancelled 须全部
        #    先于会话终态发出，防止 host 投影翻态早于 hitl 侧收尾。
        if self._cancel_pending_hitl is not None:
            try:
                await self._cancel_pending_hitl()
            except Exception:
                logger.exception("TaskManager: cancel_pending_hitl callback failed")

        # ack_tasks：只收在途（未终结、仅发了协作取消信号）的已启动带框任务——它们的
        # finish 对要等 on_task_finished(CANCELED) 终态坐实后由取消胶囊闭合 funnel 补写
        # （Task 14），threshold_finalizer 这里只做 eager ack 替换（幂等自愈）。
        # cancel_now_tasks：已经直接标 CANCELED 的任务（清队 + 挂起），终态已坐实，
        # 经 _cancel_finalizer 立即整对闭合（ack + finish 对一次写完）。
        ack_tasks: list[Task] = []
        cancel_now_tasks: list[Task] = []

        def _has_dispatch_frame(t: Task) -> bool:
            return bool(t.started_at and t.origin_tool_call_id and t.parent_task_id)

        # 4) 清队：非 root 条目 → CANCELED + TASK_CANCELED（已启动者收进 cancel_now_tasks，
        #    经 _cancel_finalizer 闭合）；root 条目直接丢弃（它的去向是第 6 步的 root 判死，
        #    不在此处发事件）。
        async with self._lock:
            pending = self._queue.drain_pending()
        for tid in pending:
            t = self._tasks.get(tid)
            if t is None or t.parent_task_id is None:
                continue
            t.status = "CANCELED"
            t.finished_at = now_utc()
            await self._emit(EventType.TASK_CANCELED, task_id=tid, payload={"reason": "failure_threshold"})
            if t.started_at:
                cancel_now_tasks.append(t)

        # 5) 取消挂起：SUSPENDED 且非 root → CANCELED + 事件（已启动者收进 cancel_now_tasks，
        #    立即整对闭合——不再走 ack_tasks/threshold_finalizer 的 ack-only 半闭合，因为它已经
        #    是终态，没有后续 on_task_finished 会来补 finish 对）；
        #    在途非 root run → 只发协作取消信号，不发事件（其 TASK_CANCELED 由 _run_loop
        #    退出路径发，finish 对交由 on_task_finished 的取消胶囊闭合 funnel；已启动带框者
        #    收进 ack_tasks，供 threshold_finalizer 做 eager ack 替换）。
        for t in list(self._tasks.values()):
            if t.parent_task_id is None:
                continue
            if t.status == "SUSPENDED":
                t.status = "CANCELED"
                t.finished_at = now_utc()
                await self._emit(
                    EventType.TASK_CANCELED, task_id=t.id, payload={"reason": "failure_threshold"},
                )
                if t.started_at:
                    cancel_now_tasks.append(t)
            elif t.id in self._running_tasks:
                if self._cancel_inflight is not None:
                    try:
                        self._cancel_inflight(t.id)
                    except Exception:
                        logger.exception("TaskManager: cancel_inflight callback failed for %s", t.id)
                if _has_dispatch_frame(t):
                    ack_tasks.append(t)

        # 5.5) 立即整对闭合已终态的取消任务（清队 + 挂起，均已启动）——替代 Task 10 里对这批
        #    任务的 ack-only 处理；在途任务保持 eager ack（上面 ack_tasks）+ funnel finish 对。
        if cancel_now_tasks and self._cancel_finalizer is not None:
            try:
                await self._cancel_finalizer(cancel_now_tasks, "failure_threshold")
            except Exception:
                logger.exception("TaskManager: cancel_finalizer callback failed (threshold cleanup)")

        # 6) root 判 FAILED：所有 parent_task_id is None 且非终态的任务判死；
        #    已终态的 root（自己就是第 N 败，FinalizeStep 已闭合；或时序尾巴已 FINISHED）
        #    不改状态、不发事件——闭合跳过。**先标 FAILED 再**对在跑的 root 调 cancel_inflight
        #    （顺序保证 _run_loop 的终态守卫接得住，见 Task 10）。
        root_we_failed_and_started: Task | None = None
        for t in list(self._tasks.values()):
            if t.parent_task_id is not None:
                continue
            if t.status in ("FINISHED", "FAILED", "CANCELED"):
                continue
            t.status = "FAILED"
            t.error_code = "TASK_FAILED_BY_THRESHOLD"
            t.error = f"Session failure threshold reached ({counter} consecutive sub-task failures)."
            t.finished_at = now_utc()
            await self._emit(EventType.TASK_FAILED, task_id=t.id, payload={
                "error_code": "TASK_FAILED_BY_THRESHOLD",
                "error_message": t.error,
            })
            if t.started_at is not None:
                root_we_failed_and_started = t
            if t.id in self._running_tasks and self._cancel_inflight is not None:
                try:
                    self._cancel_inflight(t.id)
                except Exception:
                    logger.exception("TaskManager: cancel_inflight callback failed for root %s", t.id)

        # 7) finalizer：内联 await（不是后台甩），保证 memory 落盘先于 SESSION_FINISHED（SSE 关闭）；
        #    异常只记日志不阻断终结。
        if self._threshold_finalizer is not None:
            try:
                await self._threshold_finalizer(
                    root_we_failed_and_started, ack_tasks, list(self._recent_failures),
                )
            except Exception:
                logger.exception("TaskManager: threshold_finalizer callback failed")

        # 8) 会话终态 + 收尾事件
        if self._session is not None:
            self._session.status = "FAILED"
        await self._emit(EventType.SESSION_STATUS_CHANGED, payload={
            "new_status": "FAILED", "reason": "failure_threshold",
        })
        await self._fire_session_done()

    async def cancel_all(self, *, reason: str = "") -> None:
        """硬取消整条 session 链：清空 pending 队列并标 CANCELED，会话置 CANCELED。

        在途 task 不在此处理——由 CancelToken → act checkpoint → CancelledError →
        _run_loop 置该 task CANCELED → on_task_finished（其 drain() 被 _cancelled 守卫挡住，
        finish 对经 on_task_finished 的取消胶囊闭合 funnel 补写，见 Task 14）。

        标态后立即对已启动的任务（`started_at` 非空——含 root：root 没有 origin_tool_call_id，
        但 synthesize_cancel_closure 的 own-root 形态不需要框，仍要闭合自己的 scope）经
        `_cancel_finalizer` 闭合胶囊（ack 终态化 + `[outcome=cancelled]` finish 对）。未启动
        的任务从未铸框/写过任何 memory，跳过——零 memory 写。
        """
        self._cancelled = True
        async with self._lock:
            pending = self._queue.drain_pending()
        to_close: list[Task] = []
        for tid in pending:
            t = self._tasks.get(tid)
            if t is not None:
                t.status = "CANCELED"
                t.finished_at = now_utc()
                if t.started_at:
                    to_close.append(t)
            await self._emit(EventType.TASK_CANCELED, task_id=tid, payload={"reason": reason})
        if to_close and self._cancel_finalizer is not None:
            try:
                await self._cancel_finalizer(to_close, reason or "user_cancel")
            except Exception:
                logger.exception("TaskManager: cancel_finalizer callback failed (cancel_all)")
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

    async def finalize_idle_session(self, status: str) -> None:
        """恢复专用：会话所有 task 已终态但 session 因崩溃未落终态 —— 设终态并复用
        _fire_session_done（先 gather 重跑的后台 recap，再发 SESSION_FINISHED + 回调）。

        镜像 on_task_finished 的会话收尾：先 SESSION_STATUS_CHANGED，再 _fire_session_done。
        幂等：_fire_session_done 的 _session_done_fired 守卫保证只发一次。
        """
        if self._session is not None:
            self._session.status = status
        await self._emit(EventType.SESSION_STATUS_CHANGED, payload={"new_status": status})
        await self._fire_session_done()

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
        t.retry_count = 0  # 挂起期间的旧计数不带入新一轮 attempt
        self._queue.push(QueueEntry(
            task_id=task_id, session_id=self._session_id, priority=t.priority,
        ))

    def is_cancelled(self) -> bool:
        """本 TM 是否已被硬取消（cancel_all 置 _cancelled）——供派发点补投 born-cancel 判定。"""
        return self._cancelled

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

    def running_agent_of(self, task_id: str) -> str | None:
        """在跑 run 的真实执行 agent id（无此在跑任务 → None）。pause 弃子划分的真相源。"""
        return self._running_agents.get(task_id)

    def set_pause_abandon(self, flag: bool) -> None:
        """置位/复位 pause 弃子窗口标志。

        置位期间产生两个效果：① CANCELED 任务不再连带把 session 状态改成 CANCELED（会话去向留给
        root park 决定，见 _on_task_finished 里的判断）；② run 结束时本轮新 staged 出的子任务直接丢弃、
        不入队派发（见 _flush_staged），防止弃子清队后又漏网新任务被派发。
        """
        self._pause_abandon = flag

    async def abandon_pending(
        self, *, reason: str = "pause_abandon", keep_agent: str | None = None,
    ) -> list[str]:
        """放弃排队中任务（标 CANCELED、发 TASK_CANCELED），不触碰 session 状态。

        ``keep_agent`` 非 None **且该 agent 当前没有在途 run** 时：effective agent 等于它的
        排队条目保留在队列（相对顺序不变）、不放弃——pause 弃子须保留 root agent 已入队未派发
        的那一条（root run 尚未派发、无可 pause 的对象），交由调用方补 drain 派发成为唯一续跑点；
        否则它随全清被误取消，会话既无在途 run 又无排队条目、无气泡 → 永久滞留 RUNNING。
        keep_agent 已有在途 run（那一轮已被 pause→park 成续跑点）或为 None 时，root-scope 排队
        条目照旧全清——保证「一次暂停恰一个续跑点」，与全清行为一致。

        与 cancel_all 的差异：不置 _cancelled（弃子后仍需 drain 派发保留/重排的任务）、
        不把 session 置 CANCELED（pause 弃子不是用户取消）。
        """
        async with self._lock:
            entries = self._queue.peek_all()
            self._queue.drain_pending()
            busy_agents = set(self._running_agents.values())
            keep = keep_agent is not None and keep_agent not in busy_agents
            cancelled: list[str] = []
            for e in entries:
                if keep and self._effective_agent(self._tasks.get(e.task_id)) == keep_agent:
                    self._queue.push(e)  # 保留：相对顺序按原队列顺序回填
                else:
                    cancelled.append(e.task_id)
        for tid in cancelled:
            t = self._tasks.get(tid)
            if t is not None:
                t.status = "CANCELED"
                t.finished_at = now_utc()
            await self._emit(EventType.TASK_CANCELED, task_id=tid, payload={"reason": reason})
        # 队列弃子不经 on_task_finished，不会自动触发父任务重排：若某 SUSPENDED 父任务的
        # 子任务此刻**全部**还在排队（无一在途），无人调用 _try_resume_parent → 父任务永不
        # 重排、会话滞留 RUNNING 且无续跑点。此处对每个被弃子任务补触发重排检查（幂等：
        # 兄弟仍在途时 all_done 不成立、由其 on_task_finished 接力；父已 ACTIVE 不二次入队）。
        for tid in cancelled:
            await self._try_resume_parent(tid)
        return cancelled


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
