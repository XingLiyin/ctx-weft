# Pause 令牌 per-run 化实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 pause/cancel 控制令牌从 session 单值降为 per-run registry；pause_session 改为"弃子留 root agent 当前那一轮"；新增 pause_task 定向暂停。

**Architecture:** 令牌随每次任务派发在 `_SessionTaskRunner.execute` 造出并登记进 runtime 级 registry（`session_id → task_id → RunTokens`），run 结束注销——信号作用域与消费者粒度对齐，根治跨代失联。pause_session 经 TaskManager 的 `_running_agents` 真相源划分"root agent 那一轮"（pause→park）与其余（cancel→终态）；`_pausing` 闩锁让重排后的新 run 出生即 paused。设计 spec：`docs/superpowers/specs/2026-07-05-pause-token-per-run-design.md`（先读）。

**Tech Stack:** Python 3 / asyncio / pytest（core 在 `ctx-weft/`，host 在 `src/ipmastercowork/`）。

## Global Constraints

- 分支：直接在 `refactor/hitl-id-unify` 上做，不新开分支。
- core 测试命令：`cd ctx-weft` 后 `uv run pytest <path> -v`（pyproject 已配 pythonpath；勿用裸 pytest）。
- host 测试命令：仓库根目录 `uv run pytest tests/<file> -v`（asyncio auto 模式，async 测试不用加 mark）。
- 事件类型只能用 `EVENT_TYPES` 白名单里已有的（本计划只用 `TASK_CANCELED`，不新增事件类型）。
- 注释/文档风格：与现有代码一致的中文注释，讲约束不讲改动来历。
- 每个 Task 结束提交一次，commit message 末尾带 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。

## File Structure

- `src/ctx_weft/core/control/tokens.py` — 新增 `RunTokens`；删 `PauseToken.resume()/wait_if_paused()` 死代码
- `src/ctx_weft/core/control/__init__.py` — 导出 `RunTokens`
- `src/ctx_weft/core/orchestrator/task_manager.py` — `running_agent_of` / `set_pause_abandon` / `abandon_pending` / `on_task_finished` CANCELED 守卫 / `_flush_staged` 弃子守卫
- `src/ctx_weft/core/runtime.py` — per-run registry、`pause_session` 弃子重写、`cancel_session` 重写、`pause_task`、compact busy-guard、三处 session 级令牌创建删除
- `src/ipmastercowork/api/sessions.py` — `/interrupt` 加 await；新增 `POST /{sid}/tasks/{task_id}/pause`
- 测试：core 新增 `tests/unit/test_run_tokens.py`、`test_pause_abandon_tm.py`、`test_run_token_registry.py`、`test_pause_session_abandon.py`；改 `test_interrupt_runtime.py`、`test_runtime_pause_wiring.py`、`test_compact_session.py`、`test_superseded_task_manager.py`、`tests/integration/test_token_reclaim.py`；host 新增 `tests/test_sessions_pause_task_endpoint.py`

---

### Task 1: RunTokens 数据类 + PauseToken 死代码删除

**Files:**
- Modify: `src/ctx_weft/core/control/tokens.py`
- Modify: `src/ctx_weft/core/control/__init__.py`
- Test: `tests/unit/test_run_tokens.py`（新建）

**Interfaces:**
- Produces: `RunTokens`（dataclass，字段 `cancel: CancelToken`、`pause: PauseToken`），从 `ctx_weft.core.control.tokens` 与 `ctx_weft.core.control` 均可导入。后续所有 Task 依赖它。

- [ ] **Step 1: 确认死代码无使用者**

Run: `rg -n "wait_if_paused|\.resume\(\)" ctx-weft/src ctx-weft/tests`
Expected: 仅 `tokens.py` 里的定义本身（`resume_task` 不匹配此模式）。若有其他命中，先看清用途再决定是否保留该方法（预期没有）。

- [ ] **Step 2: 写失败测试**

```python
# tests/unit/test_run_tokens.py
"""RunTokens：一次 run（单次任务派发）的控制信号对。"""

from ctx_weft.core.control import RunTokens
from ctx_weft.core.control.tokens import CancelToken, PauseToken


def test_run_tokens_pairs_are_independent():
    a = RunTokens(cancel=CancelToken(), pause=PauseToken())
    b = RunTokens(cancel=CancelToken(), pause=PauseToken())
    a.pause.pause()
    a.cancel.cancel()
    assert a.pause.is_paused and a.cancel.is_cancelled
    assert not b.pause.is_paused and not b.cancel.is_cancelled
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd ctx-weft; uv run pytest tests/unit/test_run_tokens.py -v`
Expected: FAIL，`ImportError: cannot import name 'RunTokens'`

- [ ] **Step 4: 实现**

`tokens.py`：删掉 `PauseToken.resume()`（第 46-48 行）和 `wait_if_paused()`（第 54-57 行）两个方法及 `_resume.set()` 相关初始化不动（`_resume` Event 字段保留会变死字段——一并删掉 `self._resume` 两处引用，`pause()` 只留 `self._paused.set()`）。PauseToken docstring 改为：

```python
class PauseToken:
    """Cooperative pause token（one-shot：只 pause 不复位，run 结束随 RunTokens 注销）。"""

    def __init__(self) -> None:
        self._paused = asyncio.Event()

    def pause(self) -> None:
        self._paused.set()

    @property
    def is_paused(self) -> bool:
        return self._paused.is_set()

    async def wait_paused(self) -> None:
        """Resolve once paused (mirror of CancelToken.wait for the soft-stop signal)."""
        await self._paused.wait()
```

文件末尾（Deadline 之后）加：

```python
@dataclass
class RunTokens:
    """一次 run（单次任务派发）的控制信号对；生命周期与 run 严格对齐（spec 2026-07-05）。"""

    cancel: CancelToken
    pause: PauseToken
```

`control/__init__.py`：import 行加 `RunTokens`，`__all__` 加 `"RunTokens"`。

- [ ] **Step 5: 跑测试确认通过**

Run: `cd ctx-weft; uv run pytest tests/unit/test_run_tokens.py tests/unit -v`
Expected: 新测试 PASS，unit 全绿（确认删方法没炸别人）。

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/control/ tests/unit/test_run_tokens.py
git commit -m "feat(core): RunTokens 控制信号对；删 PauseToken resume/wait_if_paused 死代码"
```

---

### Task 2: TaskManager 控制面（弃子基础设施）

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/task_manager.py`
- Test: `tests/unit/test_pause_abandon_tm.py`（新建）

**Interfaces:**
- Consumes: 无（独立于 Task 1）。
- Produces（Task 4 依赖）:
  - `TaskManager.running_agent_of(task_id: str) -> str | None`
  - `TaskManager.set_pause_abandon(flag: bool) -> None`
  - `TaskManager.abandon_pending(*, reason: str = "pause_abandon") -> list[str]`（async，返回被放弃的 task id 列表）
  - 行为变化：`_pause_abandon=True` 期间 ① `on_task_finished(CANCELED)` 不把 session 置 CANCELED ② `_flush_staged` 丢弃 staged 不入队。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_pause_abandon_tm.py
"""TaskManager pause 弃子控制面：abandon_pending / set_pause_abandon 守卫 / staged 丢弃。"""

import pytest

from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.state.models import Session, Task
from ctx_weft.core.utils import now_utc

pytestmark = pytest.mark.asyncio


class _StubRunner:
    async def assemble(self, task_id):
        return None

    async def execute(self, binding, task_id):
        return None


def _task(tid: str, status: str = "PENDING") -> Task:
    return Task(
        id=tid, session_id="s1", status=status, tenant_id="default",
        assigned_agent_id="", creator_agent_id="agr",
        title=tid, description="", user_prompt="x", created_at=now_utc(),
    )


def _tm_with_session() -> tuple[TaskManager, Session]:
    tm = TaskManager(session_id="s1")
    sess = Session(id="s1", tenant_id="default", user_prompt="x",
                   status="RUNNING", token_budget=0, root_agent_id="agr")
    tm.set_session(sess)
    tm.set_runner(_StubRunner())
    return tm, sess


async def test_abandon_pending_cancels_queue_without_touching_session():
    tm, sess = _tm_with_session()
    await tm.push_task(_task("t1"))
    await tm.push_task(_task("t2"))
    dropped = await tm.abandon_pending(reason="pause_abandon")
    assert set(dropped) == {"t1", "t2"}
    assert tm.get_task("t1").status == "CANCELED"
    assert tm.get_task("t2").status == "CANCELED"
    assert sess.status == "RUNNING"      # 弃子不动 session 状态
    assert tm.is_done() is True          # 队列已空、无在跑


async def test_pause_abandon_guard_keeps_session_status_on_cancel():
    tm, sess = _tm_with_session()
    tm.register_task(_task("t1", status="ACTIVE"))
    tm.set_pause_abandon(True)
    await tm.on_task_finished("t1", status="CANCELED")
    assert tm.get_task("t1").status == "CANCELED"
    assert sess.status != "CANCELED"     # pause 弃子 ≠ 用户取消


async def test_cancel_without_pause_abandon_still_cancels_session():
    tm, sess = _tm_with_session()
    tm.register_task(_task("t1", status="ACTIVE"))
    await tm.on_task_finished("t1", status="CANCELED")
    assert sess.status == "CANCELED"     # 既有语义不回归


async def test_flush_staged_dropped_under_pause_abandon():
    tm, _ = _tm_with_session()
    tm.register_task(_task("tp", status="ACTIVE"))
    tm.stage_task(_task("tc"), parent_task_id="tp")
    tm.set_pause_abandon(True)
    await tm._flush_staged("tp")
    assert tm.get_task("tc") is None     # 未入队、未登记（push 时才发 TASK_CREATED，无投影幽灵）
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ctx-weft; uv run pytest tests/unit/test_pause_abandon_tm.py -v`
Expected: FAIL，`AttributeError: 'TaskManager' object has no attribute 'abandon_pending'`（另两条守卫测试也 FAIL）。

- [ ] **Step 3: 实现**

`__init__`（`self._has_pending_hitl…` 附近，task_manager.py:86）加字段：

```python
        # pause 弃子窗口标记（runtime.pause_session 置位、_on_idle/_release 复位）：
        # 置位期间任务取消不改 session 状态、run 收尾 staged 直接丢弃。
        self._pause_abandon = False
```

`running_task_ids`（task_manager.py:786）旁边加三个方法：

```python
    def running_agent_of(self, task_id: str) -> str | None:
        """在跑 run 的真实执行 agent id（无此在跑任务 → None）。pause 弃子划分的真相源。"""
        return self._running_agents.get(task_id)

    def set_pause_abandon(self, flag: bool) -> None:
        self._pause_abandon = flag

    async def abandon_pending(self, *, reason: str = "pause_abandon") -> list[str]:
        """放弃全部排队中任务（标 CANCELED、发 TASK_CANCELED），不触碰 session 状态。

        与 cancel_all 的差异：不置 _cancelled（弃子后 _try_resume_parent 重排 root agent
        任务仍需 drain 派发）、不把 session 置 CANCELED（pause 弃子不是用户取消）。
        """
        async with self._lock:
            pending = self._queue.drain_pending()
        for tid in pending:
            t = self._tasks.get(tid)
            if t is not None:
                t.status = "CANCELED"
                t.finished_at = now_utc()
            await self._emit(EventType.TASK_CANCELED, task_id=tid, payload={"reason": reason})
        return pending
```

`on_task_finished` 的 CANCELED 分支（task_manager.py:599-601）改为：

```python
            elif status == "CANCELED":
                # 用户主动中断：标记 session 为 CANCELED，防止 is_done() 误判为 SUCCEEDED。
                # pause 弃子（_pause_abandon）除外：连带取消不定会话去向，由 root park 决定。
                if not self._pause_abandon:
                    self._session.status = "CANCELED"
```

`_flush_staged`（task_manager.py:~253，`staged` 取出之后、`for task, blocked_by, parent_task_id in reversed(staged):` 循环之前）插入守卫：

```python
        if self._pause_abandon:
            # pause 弃子窗口：本轮 staged 的子任务直接丢弃（push 时才发 TASK_CREATED，无投影残留），
            # 防止弃子清队后又有漏网新任务入队被派发。
            logger.info("pause_abandon: dropping %d staged task(s) of %s", len(staged), task_id)
            return
```

注意 `_flush_staged` 里 staged 为空时已有 early-return，守卫放在非空之后；logger 里的 `task_id` 以该方法实际形参名为准。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ctx-weft; uv run pytest tests/unit/test_pause_abandon_tm.py -v`
Expected: 4 条全 PASS。

- [ ] **Step 5: 回归 TM 相关既有测试**

Run: `cd ctx-weft; uv run pytest tests/unit -k task_manager -v; uv run pytest tests/unit/test_superseded_task_manager.py -v`
Expected: 全 PASS（守卫默认 False，不改既有行为）。

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/orchestrator/task_manager.py tests/unit/test_pause_abandon_tm.py
git commit -m "feat(core): TaskManager 弃子控制面：abandon_pending/pause_abandon 守卫/staged 丢弃"
```

---

### Task 3: runtime 令牌 per-run registry 化

**Files:**
- Modify: `src/ctx_weft/core/runtime.py`
- Modify: `src/ipmastercowork/api/sessions.py:361`（`/interrupt` 加 await）
- Test: `tests/unit/test_run_token_registry.py`（新建）
- Modify tests: `tests/unit/test_interrupt_runtime.py`、`test_runtime_pause_wiring.py`、`test_compact_session.py`、`test_superseded_task_manager.py`、`tests/integration/test_token_reclaim.py`

**Interfaces:**
- Consumes: `RunTokens`（Task 1）、`TaskManager.set_pause_abandon`（Task 2）。
- Produces（Task 4/5 依赖）:
  - `CtxWeftRuntime._run_tokens: dict[str, dict[str, RunTokens]]`
  - `CtxWeftRuntime._pausing: set[str]`、`CtxWeftRuntime._busy_sessions: set[str]`
  - `CtxWeftRuntime._register_run_tokens(session_id, task_id) -> RunTokens`（`_pausing` 中 → pause 出生即置位）
  - `CtxWeftRuntime._deregister_run_tokens(session_id, task_id) -> None`
  - `async CtxWeftRuntime.pause_session(session_id) -> bool`（本 Task 过渡语义：pause 全部在途 run；Task 4 改弃子）
  - `async CtxWeftRuntime.cancel_session(session_id) -> bool`（cancel 全部在途 run + cancel_all）

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_run_token_registry.py
"""Per-run token registry：随派发登记、随 run 注销；_pausing 闩锁下出生即 paused。"""

import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.control import RunTokens
from ctx_weft.providers.llm.mock import MockLLMAdapter
from tests.integration.test_minimal_loop import InMemoryTemplateResolver

pytestmark = pytest.mark.asyncio


def _rt():
    return CtxWeftRuntime(llm=MockLLMAdapter(responses=[]),
                          template_resolver=InMemoryTemplateResolver())


async def test_register_and_deregister_run_tokens():
    rt = _rt()
    tokens = rt._register_run_tokens("s1", "t1")
    assert isinstance(tokens, RunTokens)
    assert rt._run_tokens["s1"]["t1"] is tokens
    assert not tokens.pause.is_paused and not tokens.cancel.is_cancelled
    rt._deregister_run_tokens("s1", "t1")
    assert "s1" not in rt._run_tokens          # 空桶随手回收


async def test_born_paused_under_pausing_latch():
    rt = _rt()
    rt._pausing.add("s1")
    tokens = rt._register_run_tokens("s1", "t1")
    assert tokens.pause.is_paused is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ctx-weft; uv run pytest tests/unit/test_run_token_registry.py -v`
Expected: FAIL，`AttributeError: ... has no attribute '_register_run_tokens'`

- [ ] **Step 3: runtime 实现（一次改完，改动点如下）**

3a. import（runtime.py:16）改为 `from ctx_weft.core.control.tokens import CancelToken, PauseToken, RunTokens`。

3b. 字段（runtime.py:471-473）替换：

```python
        # Per-run 控制信号 registry：session_id → {task_id → RunTokens}。随派发登记、随 run
        # 注销（_SessionTaskRunner.execute），被顶替旧 TM 的 inflight 一样在册——pause/cancel
        # 经 registry 必达全部在途 run（spec 2026-07-05）。
        self._run_tokens: dict[str, dict[str, RunTokens]] = {}
        # pause 弃子进行中的 session：新派发 run 的 PauseToken 出生即 paused
        # （root agent 任务被重排后，新 run 在 act 首个 checkpoint 立即 park）。
        self._pausing: set[str] = set()
        # compact 等一次性操作的忙位（原先借 _cancel_tokens dict 占位）。
        self._busy_sessions: set[str] = set()
        self._task_managers: dict[str, TaskManager] = {}
```

3c. registry helpers（放在 `pause_session` 前面）：

```python
    def _register_run_tokens(self, session_id: str, task_id: str) -> RunTokens:
        """为一次派发发放控制信号对；pause 弃子窗口内出生即 paused。"""
        tokens = RunTokens(cancel=CancelToken(), pause=PauseToken())
        if session_id in self._pausing:
            tokens.pause.pause()
        self._run_tokens.setdefault(session_id, {})[task_id] = tokens
        return tokens

    def _deregister_run_tokens(self, session_id: str, task_id: str) -> None:
        per = self._run_tokens.get(session_id)
        if per is None:
            return
        per.pop(task_id, None)
        if not per:
            self._run_tokens.pop(session_id, None)
```

3d. `pause_session`（runtime.py:521-531）过渡版重写（Task 4 再改弃子语义）：

```python
    async def pause_session(self, session_id: str) -> bool:
        """软打断：pause 该 session 全部在途 run → act checkpoint park。

        Returns True if any live run was signalled.
        """
        per = self._run_tokens.get(session_id)
        if not per:
            return False
        for tokens in per.values():
            tokens.pause.pause()
        return True
```

3e. `cancel_session`（runtime.py:533-553）重写：保留原 docstring 首行与 idle 回收注释，令牌部分改为遍历 registry：

```python
    async def cancel_session(self, session_id: str) -> bool:
        """硬取消：取消全部在途 run（per-run CancelToken）+ 全部后续 task（drain 队列）→ 会话 CANCELED。

        memory 保留。开新对话由调用方另起（新 /messages → 同 session_id 的 new run）。
        """
        per = self._run_tokens.get(session_id, {})
        task_manager = self._task_managers.get(session_id)
        if not per and task_manager is None:
            return False
        # 取消前判定会话是否已空闲挂起（无在跑任务）。RUNNING：在途 task 经 CancelToken→checkpoint
        # 协作取消→on_task_finished→is_done→_fire_session_done→_on_done 自行回收，故此处不抢着回收。
        idle = task_manager is not None and task_manager.is_done()
        if task_manager is not None:
            await task_manager.cancel_all(reason="user_cancel")
        for tokens in per.values():
            tokens.cancel.cancel()
        if idle:
            # 已暂停/中断（无在跑 task）的会话被取消：cancel_all 不经 _fire_session_done，_on_done
            # 不会触发，故显式回收 runtime 侧 per-session 状态（含较重的 TaskManager），避免滞留。
            self._release_session(session_id)
        return True
```

3f. 删三处 session 级令牌创建：
- `start_session`（runtime.py:681-684）四行删除；`_make_task_runner(...)` 调用里删 `cancel_token=cancel_token, pause_token=pause_token,` 两参。
- `recover_session` 冷重建（runtime.py:902-905）四行删除；runner 调用同上删两参。
- `_resume_in_existing_tm`（runtime.py:982-983）两行删除，其 docstring 中「cancel/pause token：idle 时已回收，这里重建…」一条改为「控制令牌随 run 在派发时发放（per-run registry），无需在此重建」。

3g. `_make_task_runner` 签名（runtime.py:779-803）删 `cancel_token` / `pause_token` 两参数及传递；`_SessionTaskRunner.__init__`（runtime.py:1540-1571）删同名参数与 `self._cancel_token` / `self._pause_token` 字段。

3h. `_SessionTaskRunner.execute`（runtime.py:1637-1659）重写：

```python
    async def execute(self, binding: "AgentBinding", task_id: str) -> None:
        t = self._task_manager.get_task(task_id)
        if t is None:
            return
        # 单 owner 架构 seam：model 在派发时从可变 session 读取；控制令牌 per-run 发放——
        # 随本次派发登记进 runtime registry、run 结束注销，pause/cancel 经 registry 必达在途 run。
        tokens = self._runtime._register_run_tokens(self._session.id, task_id)
        try:
            s, _ = await self._runtime._execute_task(
                session=self._session,
                task=t,
                agent=binding.agent,
                template=binding.template,
                run_id=binding.run_id,
                memory=self._memory,
                llm_account=self._session.llm_provider or self._llm_account,
                llm_model=self._session.llm_model or self._llm_model,
                initial_step=binding.initial_step,
                task_manager=self._task_manager,
                cancel_token=tokens.cancel,
                pause_token=tokens.pause,
            )
        finally:
            self._runtime._deregister_run_tokens(self._session.id, task_id)
        if self._handle is not None and s is not None:
            self._handle._state = s
```

3i. `_register_and_drain` 的 `_on_idle`（runtime.py:752-758）重写：

```python
        async def _on_idle() -> None:
            # per-run token 生命周期已随 run 对齐（execute finally 注销），无需在此回收。
            # 只清 pause 弃子闩锁；compare-and-check 防被顶替旧 TM 的迟到 idle 误清新一轮闩锁。
            if self._task_managers.get(session.id) is task_manager:
                self._pausing.discard(session.id)
                task_manager.set_pause_abandon(False)
```

3j. `_release_session`（runtime.py:765-777）：把两行 token pop 换成：

```python
        self._run_tokens.pop(session_id, None)
        self._pausing.discard(session_id)
```

docstring 里「pause/cancel token」改述为「per-run 令牌 registry 残余 + pause 闩锁」。

3k. `compact_session` idle-guard（runtime.py:1024-1028）与收尾（runtime.py:1100-1101）：

```python
        # ── idle-guard: claim the slot synchronously (no await before the claim) ──
        if session_id in self._busy_sessions or self._run_tokens.get(session_id):
            raise SessionBusyError(session_id)
        self._busy_sessions.add(session_id)
        token = CancelToken()
        try:
```

finally 改 `self._busy_sessions.discard(session_id)`。

3l. host `src/ipmastercowork/api/sessions.py:361`：`runtime.pause_session(session_id)` → `await runtime.pause_session(session_id)`。

- [ ] **Step 4: 更新既有测试**

4a. `tests/unit/test_interrupt_runtime.py`：前三条测试改为：

```python
async def test_pause_session_pauses_all_live_run_tokens():
    rt = _runtime()
    a = rt._register_run_tokens("s1", "t1")
    b = rt._register_run_tokens("s1", "t2")
    assert await rt.pause_session("s1") is True
    assert a.pause.is_paused and b.pause.is_paused


async def test_cancel_session_cancels_all_run_tokens_and_drains_queue():
    rt = _runtime()
    tokens = rt._register_run_tokens("s1", "t1")
    drained = {"called": False}

    class _TM:
        def is_done(self):
            return False          # active drain in flight → _on_done reclaims after cancel completes

        async def cancel_all(self, *, reason=""):
            drained["called"] = True

    rt._task_managers["s1"] = _TM()
    assert await rt.cancel_session("s1") is True
    assert tokens.cancel.is_cancelled is True
    assert drained["called"] is True


async def test_unknown_session_returns_false():
    rt = _runtime()
    assert await rt.pause_session("nope") is False
    assert await rt.cancel_session("nope") is False
```

顶部 `from ctx_weft.core.control.tokens import CancelToken, PauseToken` 若不再被用则删；模块 docstring 更新为 per-run 语义。

4b. `tests/unit/test_runtime_pause_wiring.py:16`：`assert rt._pause_tokens == {}` → `assert rt._run_tokens == {}`；`test_build_loop_ctx_wires_pause_token` 不变（`_build_loop_ctx` 签名未动）。

4c. `tests/unit/test_compact_session.py:47`：`rt._cancel_tokens["ses_busy"] = CancelToken()` → `rt._busy_sessions.add("ses_busy")`；`:113`：`assert sid not in rt._cancel_tokens` → `assert sid not in rt._busy_sessions`。CancelToken import 若空置则删。

4d. `tests/unit/test_superseded_task_manager.py`：两处（:265-266 与 :304-305）`pause7 = PauseToken(); rt._pause_tokens[sid] = pause7` → `rt._pausing.add(sid)`；对应断言（:273、:311）→ `assert sid in rt._pausing, "新一轮的 pause 闩锁不得被旧 TM 迟到收尾清除"`。PauseToken import 若空置则删。

4e. `tests/integration/test_token_reclaim.py`：:34-35 两行 → `assert sid not in rt._run_tokens, "per-run tokens deregister when the run parks"`；:41-42 → `assert sid not in rt._run_tokens`。模块 docstring 里 token 措辞同步改 per-run。

- [ ] **Step 5: 跑测试确认通过**

Run: `cd ctx-weft; uv run pytest tests/unit/test_run_token_registry.py tests/unit/test_interrupt_runtime.py tests/unit/test_runtime_pause_wiring.py tests/unit/test_compact_session.py tests/unit/test_superseded_task_manager.py tests/integration/test_token_reclaim.py -v`
Expected: 全 PASS。

- [ ] **Step 6: 全量回归（core + host）**

Run: `cd ctx-weft; uv run pytest`
Expected: 全绿。
Run（仓库根）: `rg -n "pause_session|_pause_tokens|_cancel_tokens" tests src/ipmastercowork` 检查 host 侧残余引用（stub runtime 若定义了 `pause_session` 需改成 `async def`）；然后 `uv run pytest tests -x -q`。
Expected: 全绿；若 stub 未适配则按报错把对应 stub 的 `pause_session` 改 async。

- [ ] **Step 7: Commit**

```bash
git add src/ctx_weft/core/runtime.py ctx-weft/tests src/ipmastercowork/api/sessions.py tests
git commit -m "refactor(core): pause/cancel 令牌 per-run registry 化，根治跨代失联"
```

---

### Task 4: pause_session 弃子语义（留 root agent 当前那一轮）

**Files:**
- Modify: `src/ctx_weft/core/runtime.py`（`pause_session` 终版）
- Test: `tests/unit/test_pause_session_abandon.py`（新建）

**Interfaces:**
- Consumes: Task 2 的 `running_agent_of` / `set_pause_abandon` / `abandon_pending`；Task 3 的 registry / `_pausing`。
- Produces: `async pause_session(session_id) -> bool` 终版语义（host `/interrupt` 已 await，无需再动）。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_pause_session_abandon.py
"""pause_session 弃子语义：只留 root agent 当前那一轮（pause→park），其余 cancel/放弃。"""

import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.state.models import Session, Task
from ctx_weft.core.utils import now_utc
from ctx_weft.providers.llm.mock import MockLLMAdapter
from tests.integration.test_minimal_loop import InMemoryTemplateResolver

pytestmark = pytest.mark.asyncio


class _StubRunner:
    async def assemble(self, task_id):
        return None

    async def execute(self, binding, task_id):
        return None


def _rt():
    return CtxWeftRuntime(llm=MockLLMAdapter(responses=[]),
                          template_resolver=InMemoryTemplateResolver())


def _task(tid: str, status: str = "PENDING") -> Task:
    return Task(
        id=tid, session_id="s1", status=status, tenant_id="default",
        assigned_agent_id="", creator_agent_id="agr",
        title=tid, description="", user_prompt="x", created_at=now_utc(),
    )


def _wire(rt) -> tuple[TaskManager, Session]:
    tm = TaskManager(session_id="s1")
    sess = Session(id="s1", tenant_id="default", user_prompt="x",
                   status="RUNNING", token_budget=0, root_agent_id="agr")
    tm.set_session(sess)
    tm.set_runner(_StubRunner())
    rt._task_managers["s1"] = tm
    return tm, sess


async def test_pause_session_partitions_root_run_vs_rest():
    rt = _rt()
    tm, sess = _wire(rt)
    # 在跑两轮：root agent 的一轮 + 子 agent 的一轮（真实执行 agent 以 _running_agents 为准）
    for tid, agent in (("t_root", "agr"), ("t_sub", "ag_sub")):
        tm.register_task(_task(tid, status="ACTIVE"))
        tm._running_tasks.add(tid)
        tm._running_agents[tid] = agent
    root_tokens = rt._register_run_tokens("s1", "t_root")
    sub_tokens = rt._register_run_tokens("s1", "t_sub")
    await tm.push_task(_task("t_q"))     # 排队中一个

    assert await rt.pause_session("s1") is True
    # root agent 那一轮：pause → park；不 cancel
    assert root_tokens.pause.is_paused and not root_tokens.cancel.is_cancelled
    # 其余在途 run：cancel → 终态；不 pause
    assert sub_tokens.cancel.is_cancelled and not sub_tokens.pause.is_paused
    # 排队任务被放弃
    assert tm.get_task("t_q").status == "CANCELED"
    # 闩锁置位（等 root park 后 _on_idle 清除）、session 状态未被弃子污染
    assert "s1" in rt._pausing
    assert sess.status == "RUNNING"


async def test_pause_session_unknown_task_runs_are_cancelled():
    # 旧 TM inflight：registry 在册但当前 TM 的 _running_agents 不认识 → 按"非 root 那一轮"cancel
    rt = _rt()
    tm, _ = _wire(rt)
    tm.register_task(_task("t_keepalive", status="ACTIVE"))
    tm._running_tasks.add("t_keepalive")
    tm._running_agents["t_keepalive"] = "agr"
    rt._register_run_tokens("s1", "t_keepalive")
    stale = rt._register_run_tokens("s1", "t_stale")   # 旧代 run，当前 TM 不认识
    assert await rt.pause_session("s1") is True
    assert stale.cancel.is_cancelled is True


async def test_pause_session_idle_session_is_noop_false():
    rt = _rt()
    _wire(rt)   # TM 存在但无在跑、无排队 → is_done
    assert await rt.pause_session("s1") is False
    assert "s1" not in rt._pausing
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ctx-weft; uv run pytest tests/unit/test_pause_session_abandon.py -v`
Expected: FAIL（过渡版 pause_session 会 pause 两个 run、不 cancel、不清队列——第一条断言 `sub_tokens.cancel.is_cancelled` 失败；idle 会话 registry 为空时过渡版恰好也返回 False，但 partition 测试必挂）。

- [ ] **Step 3: pause_session 终版实现**

替换 Task 3 的过渡版：

```python
    async def pause_session(self, session_id: str) -> bool:
        """软打断（spec 2026-07-05）：放弃其余在途/排队任务，只留 root agent 当前那一轮。

        - 置 _pausing 闩锁：其间新派发 run 出生即 paused（root agent 任务被 _try_resume_parent
          重排后，新 run 在 act 首个 checkpoint park，不烧 LLM）。
        - 排队任务全部放弃（abandon_pending：标 CANCELED，不动 session 状态、不封 drain）。
        - 在途 run 按真实执行 agent 划分：== root agent 的那一轮（同 agent 串行 ≤1）pause →
          park 一个 wait 气泡；其余（含被顶替旧 TM 的 inflight）cancel → 协作取消终态。
        - 闩锁由 _on_idle（root park 后会话空闲）或 _release_session 清除。
        """
        per = self._run_tokens.get(session_id, {})
        tm = self._task_managers.get(session_id)
        if not per and (tm is None or tm.is_done()):
            return False
        self._pausing.add(session_id)
        root_agent = ""
        if tm is not None:
            tm.set_pause_abandon(True)
            root_agent = (tm.session.root_agent_id or "") if tm.session is not None else ""
            await tm.abandon_pending(reason="pause_abandon")
        for task_id, tokens in list(per.items()):
            if root_agent and tm is not None and tm.running_agent_of(task_id) == root_agent:
                tokens.pause.pause()
            else:
                tokens.cancel.cancel()
        # 竞态兜底：信号发完会话已静止（root 恰好收尾、无可 park 对象）→ 立即清闩锁防残留。
        if tm is not None and tm.is_done():
            self._pausing.discard(session_id)
            tm.set_pause_abandon(False)
        return True
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ctx-weft; uv run pytest tests/unit/test_pause_session_abandon.py tests/unit/test_interrupt_runtime.py -v`
Expected: 新测试全 PASS；`test_interrupt_runtime` 里 Task 3 写的 `test_pause_session_pauses_all_live_run_tokens` 会 FAIL——按终版语义改掉它：

```python
async def test_pause_session_without_tm_cancels_all_runs():
    # 无 TM（纯 registry 残留）：无法辨认 root agent → 全部按"其余"cancel，返回 True
    rt = _runtime()
    a = rt._register_run_tokens("s1", "t1")
    b = rt._register_run_tokens("s1", "t2")
    assert await rt.pause_session("s1") is True
    assert a.cancel.is_cancelled and b.cancel.is_cancelled
```

再跑同命令，Expected: 全 PASS。

- [ ] **Step 5: 全量回归**

Run: `cd ctx-weft; uv run pytest`
Expected: 全绿。
Run（仓库根）: `uv run pytest tests -x -q`
Expected: 全绿。

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/runtime.py tests/unit/test_pause_session_abandon.py tests/unit/test_interrupt_runtime.py
git commit -m "feat(core): pause_session 弃子留 root agent 当前那一轮（spec 2026-07-05）"
```

---

### Task 5: pause_task 定向暂停 + host 端点

**Files:**
- Modify: `src/ctx_weft/core/runtime.py`（`pause_session` 后面加 `pause_task`）
- Modify: `src/ipmastercowork/api/sessions.py`（`interrupt_session` 后面加端点）
- Test: `tests/unit/test_pause_session_abandon.py`（追加一条）
- Test: `tests/test_sessions_pause_task_endpoint.py`（新建，host）

**Interfaces:**
- Consumes: Task 3 registry。
- Produces: `CtxWeftRuntime.pause_task(session_id, task_id) -> bool`（同步）；host `POST /sessions/{sid}/tasks/{task_id}/pause`。

- [ ] **Step 1: core 失败测试**（追加到 `test_pause_session_abandon.py`）

```python
async def test_pause_task_targets_single_run():
    rt = _rt()
    a = rt._register_run_tokens("s1", "ta")
    b = rt._register_run_tokens("s1", "tb")
    assert rt.pause_task("s1", "ta") is True
    assert a.pause.is_paused and not b.pause.is_paused
    assert rt.pause_task("s1", "nope") is False    # 不在跑 → False
```

Run: `cd ctx-weft; uv run pytest tests/unit/test_pause_session_abandon.py::test_pause_task_targets_single_run -v`
Expected: FAIL，`AttributeError: ... no attribute 'pause_task'`

- [ ] **Step 2: core 实现**

```python
    def pause_task(self, session_id: str, task_id: str) -> bool:
        """定向暂停（spec 2026-07-05 §2.3）：pause 指定在途 task 的 run → 它在检查点 park
        自己的 wait 气泡，经多 pending 面板回复续跑。不在跑（无本 run 令牌）→ False。
        只停该 task 本身的 run，不涉及其子任务。"""
        tokens = self._run_tokens.get(session_id, {}).get(task_id)
        if tokens is None:
            return False
        tokens.pause.pause()
        return True
```

Run 同 Step 1 命令，Expected: PASS。

- [ ] **Step 3: host 失败测试**

```python
# tests/test_sessions_pause_task_endpoint.py
"""POST /{id}/tasks/{task_id}/pause — 定向暂停单个在途 task（spec 2026-07-05 §2.3）。"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import ipmastercowork.api.sessions as sessions_mod
from ipmastercowork.api.models.session import SessionEntry


def _entry(sid: str, status: str) -> SessionEntry:
    e = SessionEntry(session_id=sid, template_id="t", user_prompt="x",
                     tenant_id="default", llm_model=None, llm_account=None)
    e.status = status
    return e


class _StubRuntime:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[tuple[str, str]] = []

    def pause_task(self, sid: str, tid: str) -> bool:
        self.calls.append((sid, tid))
        return self.ok


async def test_pause_task_endpoint_pauses_running_task() -> None:
    sid = "s_pt"
    sessions_mod._sm._sessions[sid] = _entry(sid, "RUNNING")
    runtime = _StubRuntime(ok=True)
    try:
        await sessions_mod.pause_task_endpoint(sid, "t1", runtime=runtime)
        assert runtime.calls == [(sid, "t1")]
    finally:
        sessions_mod._sm._sessions.pop(sid, None)


async def test_pause_task_endpoint_409_when_task_not_running() -> None:
    sid = "s_pt_idle"
    sessions_mod._sm._sessions[sid] = _entry(sid, "RUNNING")
    try:
        with pytest.raises(HTTPException) as exc:
            await sessions_mod.pause_task_endpoint(sid, "t1", runtime=_StubRuntime(ok=False))
        assert exc.value.status_code == 409
    finally:
        sessions_mod._sm._sessions.pop(sid, None)


async def test_pause_task_endpoint_404_unknown_session() -> None:
    with pytest.raises(HTTPException) as exc:
        await sessions_mod.pause_task_endpoint("nope", "t1", runtime=_StubRuntime())
    assert exc.value.status_code == 404
```

Run（仓库根）: `uv run pytest tests/test_sessions_pause_task_endpoint.py -v`
Expected: FAIL，`AttributeError: module ... has no attribute 'pause_task_endpoint'`

- [ ] **Step 4: host 实现**（`interrupt_session` 之后、`cancel_session_endpoint` 之前）

```python
@router.post("/{session_id}/tasks/{task_id}/pause", response_model=dict)
async def pause_task_endpoint(
    session_id: str,
    task_id: str,
    runtime=Depends(deps.get_runtime),
) -> dict:
    """定向暂停指定在途 task（spec 2026-07-05 §2.3）：该 run 在检查点 park 自己的 wait
    气泡，经多 pending 面板回复续跑。task 不在跑（已 park / 已终态 / 不存在）→ 409。"""
    entry = _sm._sessions.get(session_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    if not runtime.pause_task(session_id, task_id):
        raise HTTPException(status_code=409, detail=f"Task {task_id} is not running")
    return entry.to_dict()
```

同时在 sessions.py 头部的路由清单注释（第 11-12 行附近）补一行 `POST /sessions/{id}/tasks/{task_id}/pause  定向暂停单个在途 task`。

- [ ] **Step 5: 跑测试确认通过**

Run（仓库根）: `uv run pytest tests/test_sessions_pause_task_endpoint.py -v`
Expected: 3 条全 PASS。

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/runtime.py tests/unit/test_pause_session_abandon.py src/ipmastercowork/api/sessions.py tests/test_sessions_pause_task_endpoint.py
git commit -m "feat: pause_task 定向暂停单个在途 task（core API + host 端点）"
```

---

### Task 6: 全量回归 + 冒烟清单

**Files:**
- 无新代码；只跑套件、修意外挂掉的测试（若有）。

- [ ] **Step 1: core 全量**

Run: `cd ctx-weft; uv run pytest`
Expected: 全绿。若有挂：先读挂的测试判断是"测试锁旧行为"（改测试对齐 spec）还是"实现 bug"（改实现），逐个清零。

- [ ] **Step 2: host 全量**

Run（仓库根）: `uv run pytest tests -q`
Expected: 全绿。重点关注 `test_pause_during_resume_regression.py`、`test_hitl_cancel_on_interrupt.py`、`test_sessions_paused_routing.py`（它们围着 /interrupt 与 PAUSED 路由转）。

- [ ] **Step 3: 收尾提交（若 Step 1/2 有修复）**

```bash
git add -A
git commit -m "test: pause per-run 化全量回归修复"
```

- [ ] **Step 4: 手动冒烟清单（桌面端起真实服务验证，报告结果，不自动化）**

1. 单任务会话：运行中点暂停 → 恰一个 wait 气泡、会话 PAUSED；回复后续跑。
2. 并发子任务会话（模板带 delegate）：运行中点暂停 → 子任务终态 CANCELED、root agent 一个 wait 气泡；回复后 root 续跑、可见子任务已取消。
3. root 等子任务时点暂停：子任务取消 → root 自动重排即刻 park（观察无新 LLM 请求）→ 一个 wait 气泡。
4. P2 回归：任务 A park（问答气泡）、任务 B 仍在跑时回复 A → 再点暂停 → B 能被停住（终态或 park，按其 agent 归属）。
5. pause_task：curl `POST /sessions/{sid}/tasks/{tid}/pause` 暂停一个在跑子任务 → 该任务出 wait 气泡、兄弟任务不受影响；回复该气泡续跑。
6. 暂停后的会话点「停止」（/cancel）→ 会话 CANCELED、无残留 pending HITL。
