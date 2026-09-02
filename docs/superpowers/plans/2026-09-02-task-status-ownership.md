# task 状态所有权收口 · 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `TaskManager` 成为 task 状态的唯一改写者与唯一事件发射者；loop 只报「发生了什么」。

**Architecture:** 把「判决」与「处置」分开。loop 侧（observe / finalize / suspend / act / control tool）产出**判决**——observer 说 success/fail/retry、控制工具说完成或挂起、run 说被打断了；`TaskManager` 拿到判决后应用**重试预算**、决定 task 的下一个状态、写内存、发出那一条 task 事件。传递媒介是 `_run_loop` 返回的 `RunOutcome`。`RunFinished.final_status` 相应从「task 的状态」改成「run 自己的结局」。

**Tech Stack:** Python 3.11+、pytest（`asyncio_mode = "auto"`）、ruff（line-length 100）。

**Spec:** `docs/events-v2.md` §2.1（分层原则）、§2.3（Task 域）、§2.4（Run 域）

## Global Constraints

- **每层只发自己领域的事实。** 这是上一轮会话状态收口确立的规则，本计划把它推到 task 层：
  收工后 **task 状态事件只能从 `TaskManager` 发出**，loop 侧一条都不发。
- **判据只能是事件类型或结构化字段，不能是 payload 里的自由文本。**
  `reason` / `error_message` 只作溯源，不作路由。
- **行为等价优先**：本计划是所有权重构，**不改变任何 task 的最终状态转移结果**。
  哪条路径今天落到 `FAILED`，收口后仍落到 `FAILED`。测试红了如果是「事件从哪发变了」
  就改断言；如果是「某个状态不该到达了」——**停下来**，那是实现错了。
- **只删发射，不删枚举。** 现有 task 事件类型一个都不删。
- ruff line-length 100；新文件 `from __future__ import annotations`。
- **验收：全量 FAILED 恰好 2 条**（都是先于这一切的既有失败）：
  `tests/integration/test_compact_flow_e2e.py::test_multiround_retry_accumulates_then_l3_collapses_e2e`
  `tests/unit/test_observe_outcomes.py::test_default_role_prompt_uses_two_fields`
  用 **venv 解释器**（`./.venv/Scripts/python.exe -m pytest`），**不要**加 `-p no:warnings`
  （`test_close_process_report_a1.py` 用了 `filterwarnings` mark，会收集失败）。
  `test_dispatch_boundary_recap_e2e` 是**已知 flake**，撞上就再跑一遍并说明。

---

## 关于测试搭台（T2 / T3 / T4 都适用，只说一次）

本计划里 `loop_fixture` / `park_case` / `outage_case` / `crash_case` 这类名字
**本仓没有现成的 fixture**。参照 `tests/unit/test_hitl_park.py` 与
`tests/unit/test_run_loop_outage.py` —— 它们是**自包含**的：把 runtime 与替身建在
函数体里，不用 fixture。请按同样的方式改写，或在各测试文件顶部写一个共享的最小搭台函数。
**不要**去改那两个既有文件。

---

## 现状（写计划时实测，不是回忆）

**task 状态在约 30 处被写**，跨 7 个文件；**task 事件从 5 个组件发出**：

| 组件 | 发的 task 事件 | 还直接写内存 `task.status` |
|---|---|---|
| `TaskManager` | `TaskCreated` `TaskStarted` `TaskResumed` `TaskRequeued` `TaskCanceled` `TaskFailed` `TaskInterrupted` | 是（10 处） |
| `runtime._run_loop` | `TaskAwaitingHuman` `TaskInterrupted` `TaskCanceled` | 是（4 处） |
| `loop/steps/finalize.py` | `TaskFinished` `TaskFailed` `TaskRequeued` `TaskFinalized` | 是 |
| `loop/steps/suspend.py` | `TaskSuspended` | — |
| `loop/steps/observe.py`、`orchestrator/control_capability.py`、`loop/steps/act.py` | **不发事件** | 是 |

最后一行是问题最集中的地方：`ObserveStep._apply_assessment`（`observe.py:405-417`）
把 verdict 直接写成 task 状态（success→`FINISHED` / fail→`FAILED` / retry→`PENDING`）
**却不发事件**，事件要等 `FinalizeStep` 之后才补。那段窗口里「内存说什么」与「事件流说什么」是脱节的。

**而 `FinalizeStep` 正在做 TM 的活**：`finalize.py:695` 的
`retry_exhausted = outcome == "retry" and task.retry_count >= task.max_retries`
是**重试预算**判断——那是处置，不是判决。

**接线点是现成的**：`task_manager.py:462-495` 的 `_run_task` 在 run 跑完之后
**已经在读 `task.status` 做分派**（`_PARKED_STATUSES` → 挂起出口、`PENDING` → 重排、
其余 → `on_task_finished`）。本计划就是把「读 loop 写的状态」换成「读 loop 返回的结局」。

---

## 处置表（Task 1 的核心，后续 task 都以它为准）

| run 结局 | 判决 | 重试预算 | → task 状态 | TM 发出 |
|---|---|---|---|---|
| `completed` | `success` | — | `FINISHED` | `TaskFinished` |
| `completed` | `fail` | — | `FAILED` | `TaskFailed{error_code: TASK_FAILED_BY_OBSERVER}` |
| `completed` | `retry` | 有余额 | `PENDING` | `TaskRequeued` |
| `completed` | `retry` | **耗尽** | `FAILED` | `TaskFailed{error_code: TASK_FAILED_RETRY_EXHAUSTED}` |
| `awaiting_human` | — | — | `AWAITING_HUMAN` | `TaskAwaitingHuman{hitl_id}` |
| `suspended_on_children` | — | — | `SUSPENDED` | `TaskSuspended{summary, spawn_titles}` |
| `interrupted` | — | 有余额 ∧ 可重试 | `PENDING` | `TaskRequeued` |
| `interrupted` | — | 否 | `INTERRUPTED` | `TaskInterrupted{reason, error_code, …}` |
| `canceled` | — | — | `CANCELED` | `TaskCanceled{reason}` |

**「可重试」的口径必须与今天逐字一致**：LLM outage **从不原地重试**（等 `/resume`），
run 崩溃按 `retry_count < max_retries` 且 `getattr(exc, "retriable", True)`。
实施 Task 1 时**先去读今天的 `_handle_task_failure` 与 `runtime.py` 的 `will_retry` 计算**，
把实际口径抄进处置表，不要照本文档的散文重新发明。

---

## Task 1: `RunOutcome` 与处置表（纯函数）

**Files:**
- Create: `src/ctx_weft/core/orchestrator/task_disposition.py`
- Test: `tests/unit/test_task_disposition.py`

**Interfaces:**
- Consumes: 无（纯 stdlib + `TaskStatus` 类型）
- Produces:
  - `RunOutcomeKind` —— `StrEnum`：`COMPLETED` / `AWAITING_HUMAN` / `SUSPENDED_ON_CHILDREN` / `INTERRUPTED` / `CANCELED`
  - `RunOutcome` frozen dataclass
  - `Disposition` frozen dataclass：`{status: str, event_type: str, payload: dict}`
  - `disposition_for(outcome: RunOutcome, *, retry_count: int, max_retries: int) -> Disposition`

- [ ] **Step 1: 先读今天的重试口径**

在写任何代码之前，读这三处并把实际条件抄下来（报告里贴出来）：
- `src/ctx_weft/core/orchestrator/task_manager.py` 的 `_handle_task_failure`
- `src/ctx_weft/core/runtime.py` 里 `will_retry = …` 那一行
- `src/ctx_weft/core/loop/steps/finalize.py:695` 的 `retry_exhausted`

**处置表要与它们逐字等价**，不是重新设计。

- [ ] **Step 2: 写失败的测试**

```python
"""run 结局 + 判决 + 重试预算 → task 处置。纯函数，无 IO（Task 1）。

这里看不见事件总线、看不见 TaskManager 的队列——只有一张表。
"""

from __future__ import annotations

import pytest

from ctx_weft.core.orchestrator.task_disposition import (
    RunOutcome,
    RunOutcomeKind,
    disposition_for,
)


def _completed(verdict: str, **kw) -> RunOutcome:
    return RunOutcome(kind=RunOutcomeKind.COMPLETED, verdict=verdict, **kw)


def test_success_finishes():
    d = disposition_for(_completed("success", summary="done", outputs={"a": 1}),
                        retry_count=0, max_retries=3)
    assert d.status == "FINISHED"
    assert d.event_type == "TaskFinished"
    assert d.payload["outcome"] == "success"
    assert d.payload["outputs"] == {"a": 1}


def test_observer_fail_fails_with_its_own_code():
    d = disposition_for(_completed("fail", error="观察者判死"), retry_count=0, max_retries=3)
    assert d.status == "FAILED"
    assert d.event_type == "TaskFailed"
    assert d.payload["error_code"] == "TASK_FAILED_BY_OBSERVER"
    assert d.payload["error_message"] == "观察者判死"


def test_retry_with_budget_requeues():
    d = disposition_for(_completed("retry", summary="再来"), retry_count=1, max_retries=3)
    assert d.status == "PENDING"
    assert d.event_type == "TaskRequeued"
    assert d.payload["retry_count"] == 2      # 已 +1，与今天 finalize 的行为一致


def test_retry_exhausted_degrades_to_failed():
    """这条判断今天在 FinalizeStep 里——它是重试预算，属于处置不属于判决。"""
    d = disposition_for(_completed("retry", error="本轮受阻"), retry_count=3, max_retries=3)
    assert d.status == "FAILED"
    assert d.event_type == "TaskFailed"
    assert d.payload["error_code"] == "TASK_FAILED_RETRY_EXHAUSTED"
    assert d.payload["error_message"] == "本轮受阻"


def test_awaiting_human_carries_the_hitl_id():
    d = disposition_for(RunOutcome(kind=RunOutcomeKind.AWAITING_HUMAN, hitl_id="hit_1"),
                        retry_count=0, max_retries=3)
    assert d.status == "AWAITING_HUMAN"
    assert d.event_type == "TaskAwaitingHuman"
    assert d.payload == {"hitl_id": "hit_1"}


def test_suspended_on_children_carries_the_titles():
    d = disposition_for(
        RunOutcome(kind=RunOutcomeKind.SUSPENDED_ON_CHILDREN,
                   summary="等两个子任务", spawn_titles=("查资料", "写稿")),
        retry_count=0, max_retries=3)
    assert d.status == "SUSPENDED"
    assert d.event_type == "TaskSuspended"
    assert d.payload["spawn_titles"] == ["查资料", "写稿"]


def test_outage_never_retries_in_place():
    """LLM outage 等 /resume，从不原地重试——即使预算充足。"""
    d = disposition_for(
        RunOutcome(kind=RunOutcomeKind.INTERRUPTED, reason="llm_outage",
                   error_code="llm_outage", retriable=False),
        retry_count=0, max_retries=3)
    assert d.status == "INTERRUPTED"
    assert d.event_type == "TaskInterrupted"


def test_retriable_crash_with_budget_requeues():
    d = disposition_for(
        RunOutcome(kind=RunOutcomeKind.INTERRUPTED, reason="run_crash",
                   error_code="ValueError", retriable=True),
        retry_count=0, max_retries=3)
    assert d.status == "PENDING"
    assert d.event_type == "TaskRequeued"


def test_retriable_crash_without_budget_interrupts():
    d = disposition_for(
        RunOutcome(kind=RunOutcomeKind.INTERRUPTED, reason="run_crash",
                   error_code="ValueError", retriable=True),
        retry_count=3, max_retries=3)
    assert d.status == "INTERRUPTED"
    assert d.event_type == "TaskInterrupted"
    assert d.payload["reason"] == "run_crash"


def test_canceled():
    d = disposition_for(RunOutcome(kind=RunOutcomeKind.CANCELED, reason="user_cancel"),
                        retry_count=0, max_retries=3)
    assert d.status == "CANCELED"
    assert d.event_type == "TaskCanceled"
    assert d.payload == {"reason": "user_cancel"}


@pytest.mark.parametrize("kind", list(RunOutcomeKind))
def test_every_kind_yields_a_disposition(kind):
    """值域穷举：加一种结局就必须在表里给它一行，否则这条会红。"""
    d = disposition_for(RunOutcome(kind=kind, verdict="success"),
                        retry_count=0, max_retries=3)
    assert d.status and d.event_type
```

- [ ] **Step 3: 跑测试确认失败**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_task_disposition.py -v`
Expected: FAIL —— `ModuleNotFoundError: ctx_weft.core.orchestrator.task_disposition`

- [ ] **Step 4: 实现**

```python
"""run 结局 + 重试预算 → task 处置。**唯一**一份「task 下一步是什么」的判据。

为什么单独成模块、且是纯函数：这份判断今天散在四个地方——
`FinalizeStep` 判重试耗尽、`ObserveStep._apply_assessment` 把 verdict 写成状态、
`TaskManager._handle_task_failure` 判重试预算、`_run_loop` 的 except 链写挂起态。
把它们收进一张表，`TaskManager` 才可能成为 task 状态的唯一改写者
（docs/events-v2.md §2.1 的分层原则推到 task 层）。

纯函数是刻意的：它不该知道事件总线、不该知道队列、不该 await 任何东西。
loop 报「发生了什么」，这张表回答「那么 task 变成什么」，TaskManager 负责执行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

__all__ = ["Disposition", "RunOutcome", "RunOutcomeKind", "disposition_for"]


class RunOutcomeKind(StrEnum):
    """一次 run 是怎么结束的。**run 自己的词表**，不是 task 状态。"""

    COMPLETED = "completed"                        # step 链跑到头，带 observer 判决
    AWAITING_HUMAN = "awaiting_human"              # HITL 冷 park
    SUSPENDED_ON_CHILDREN = "suspended_on_children"  # 等子任务
    INTERRUPTED = "interrupted"                    # LLM outage / run 崩溃
    CANCELED = "canceled"                          # 被取消


@dataclass(frozen=True)
class RunOutcome:
    """loop 交给 TaskManager 的全部信息：**发生了什么**，不含「task 该变成什么」。"""

    kind: RunOutcomeKind
    verdict: str = ""            # COMPLETED 时：success / fail / retry
    summary: str = ""
    outputs: object = None
    error: str = ""              # 死因 / 受阻原因（自由文本，只作溯源）
    error_code: str = ""
    reason: str = ""             # INTERRUPTED：llm_outage / run_crash；CANCELED：取消原因
    retriable: bool = False      # INTERRUPTED 专用：这次打断允不允许原地重试
    hitl_id: str = ""            # AWAITING_HUMAN
    spawn_titles: tuple[str, ...] = ()   # SUSPENDED_ON_CHILDREN


@dataclass(frozen=True)
class Disposition:
    """处置结果：task 落到哪个状态 + TaskManager 该发哪条事件。"""

    status: str
    event_type: str
    payload: dict


def disposition_for(
    outcome: RunOutcome, *, retry_count: int, max_retries: int,
) -> Disposition:
    """结局 + 预算 → 处置。**不改变任何今天的转移结果**，只是把判断收到一处。"""
    if outcome.kind is RunOutcomeKind.AWAITING_HUMAN:
        return Disposition("AWAITING_HUMAN", "TaskAwaitingHuman",
                           {"hitl_id": outcome.hitl_id})

    if outcome.kind is RunOutcomeKind.SUSPENDED_ON_CHILDREN:
        return Disposition("SUSPENDED", "TaskSuspended", {
            "summary": outcome.summary,
            "spawn_titles": list(outcome.spawn_titles),
        })

    if outcome.kind is RunOutcomeKind.CANCELED:
        return Disposition("CANCELED", "TaskCanceled", {"reason": outcome.reason})

    if outcome.kind is RunOutcomeKind.INTERRUPTED:
        # outage 恒 retriable=False：它等 /resume，从不原地重试（今天的行为）。
        if outcome.retriable and retry_count < max_retries:
            return Disposition("PENDING", "TaskRequeued", {
                "reason": outcome.reason, "retry_count": retry_count + 1,
            })
        return Disposition("INTERRUPTED", "TaskInterrupted", {
            "reason": outcome.reason,
            "error_code": outcome.error_code,
            "error_message": outcome.error,
            "retry_count": retry_count,
        })

    # COMPLETED：observer 的判决 + 重试预算
    if outcome.verdict == "success":
        return Disposition("FINISHED", "TaskFinished", {
            "outcome": "success", "summary": outcome.summary, "outputs": outcome.outputs,
        })
    if outcome.verdict == "retry" and retry_count < max_retries:
        return Disposition("PENDING", "TaskRequeued", {
            "outcome": "retry", "summary": outcome.summary, "retry_count": retry_count + 1,
        })
    # fail，或 retry 但预算耗尽 —— 后者降级成 fail（今天在 FinalizeStep:695）
    exhausted = outcome.verdict == "retry"
    return Disposition("FAILED", "TaskFailed", {
        "error_code": ("TASK_FAILED_RETRY_EXHAUSTED" if exhausted
                       else "TASK_FAILED_BY_OBSERVER"),
        "error_message": outcome.error,
        "retry_count": retry_count,
    })
```

> ⚠️ 上面是**骨架**。Step 1 读出来的实际口径若与它不符（尤其 `retriable` 的判定
> 与 `retry_count` 何时自增），**以实际口径为准**并在报告里说明改了哪里、为什么。

- [ ] **Step 5: 跑测试确认通过**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_task_disposition.py -v`
Expected: PASS（10 个具名 + 5 个参数化）

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/core/orchestrator/task_disposition.py tests/unit/test_task_disposition.py
git commit -m "feat(task): 处置表独立成纯函数——run 结局 + 重试预算 → task 下一步"
```

---

## Task 2: loop 侧产出 `RunOutcome`，停止写 task 状态

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/observe.py`（`_apply_assessment` 不再写 `task.status`）
- Modify: `src/ctx_weft/core/loop/steps/finalize.py`（不判重试耗尽、不发 task 事件，产出判决）
- Modify: `src/ctx_weft/core/loop/steps/suspend.py`（不发 `TaskSuspended`，产出结局）
- Modify: `src/ctx_weft/core/orchestrator/control_capability.py`（不写 `task.status`）
- Modify: `src/ctx_weft/core/loop/steps/act.py`（不写 `task.status`）
- Modify: `src/ctx_weft/core/state/models.py`（`LoopState` 或 `Task` 上挂 `run_outcome`）
- Test: `tests/unit/test_loop_reports_outcome.py`

**Interfaces:**
- Consumes: Task 1 的 `RunOutcome` / `RunOutcomeKind`
- Produces: run 结束时 `state.run_outcome: RunOutcome | None` 已就位

> **本 task 之后 TM 还没开始消费它**，所以要保留一条过渡：loop 仍写 `task.status`
> 与仍发既有事件——**先只做「额外产出 RunOutcome」**，不删任何东西。
> 删除在 Task 4，那时 TM 已接管。这样每个 task 结束时全量都是绿的。

- [ ] **Step 1: 写失败的测试**

```python
"""loop 在 run 结束时产出 RunOutcome（Task 2）。

本 task 只加产出、不删旧路径——旧的写状态与发事件仍在，
所以既有行为一字不变，全量应当仍是那 2 条基线失败。
"""

from __future__ import annotations

from ctx_weft.core.orchestrator.task_disposition import RunOutcomeKind


async def test_observer_success_produces_a_completed_outcome(loop_fixture):
    state = await loop_fixture.run_until_finalize(verdict="success", summary="做完了")
    assert state.run_outcome is not None
    assert state.run_outcome.kind is RunOutcomeKind.COMPLETED
    assert state.run_outcome.verdict == "success"
    assert state.run_outcome.summary == "做完了"


async def test_observer_retry_produces_the_verdict_not_the_disposition(loop_fixture):
    """loop 只报「observer 说重试」，**不判**预算够不够——那是 TM 的活。"""
    state = await loop_fixture.run_until_finalize(verdict="retry", retry_count=99)
    assert state.run_outcome.kind is RunOutcomeKind.COMPLETED
    assert state.run_outcome.verdict == "retry"     # 即使预算早耗尽，这里仍是 retry


async def test_hitl_park_produces_awaiting_human_with_hitl_id(park_fixture):
    state = await park_fixture.run_until_park()
    assert state.run_outcome.kind is RunOutcomeKind.AWAITING_HUMAN
    assert state.run_outcome.hitl_id


async def test_suspend_on_children_carries_spawn_titles(suspend_fixture):
    state = await suspend_fixture.run_until_suspend(titles=["查资料"])
    assert state.run_outcome.kind is RunOutcomeKind.SUSPENDED_ON_CHILDREN
    assert list(state.run_outcome.spawn_titles) == ["查资料"]


async def test_llm_outage_produces_interrupted_not_retriable(outage_fixture):
    state = await outage_fixture.run_until_outage()
    assert state.run_outcome.kind is RunOutcomeKind.INTERRUPTED
    assert state.run_outcome.reason == "llm_outage"
    assert state.run_outcome.retriable is False     # outage 从不原地重试
```

> `loop_fixture` / `park_fixture` / `suspend_fixture` / `outage_fixture`
> **本仓没有现成的**。参照 `tests/unit/test_hitl_park.py` 与
> `tests/unit/test_run_loop_outage.py` 里自包含的搭台方式（它们把 runtime 与替身
> 建在函数体里，没有 fixture），把上面五条改写成同样的自包含形式，
> 或在本文件顶部写一个共享的最小搭台函数。**不要**去改那两个既有文件。

- [ ] **Step 2: 跑测试确认失败**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_loop_reports_outcome.py -v`
Expected: FAIL —— `AttributeError: 'LoopState' object has no attribute 'run_outcome'`

- [ ] **Step 3: `LoopState` 加字段**

`src/ctx_weft/core/state/models.py` 的 `LoopState`（或它所在处）加：

```python
    #: 本次 run 的结局，由 loop 在结束前填好、交给 TaskManager 决定 task 处置。
    #: loop 报「发生了什么」，不报「task 该变成什么」——后者是 TM 的活
    #: （docs/superpowers/plans/2026-09-02-task-status-ownership.md 的处置表）。
    run_outcome: "RunOutcome | None" = None
```

用 `if TYPE_CHECKING:` 导入 `RunOutcome`，避免运行期反向依赖。

- [ ] **Step 4: 五个产出点各填一次**

- `finalize.py`：在既有的 `if outcome == …` 分派**之前**，按 observer 的 `outcome`
  （`success` / `fail` / `retry`，**未经重试耗尽降级的原值**）填
  `state.run_outcome = RunOutcome(kind=COMPLETED, verdict=outcome, summary=…, outputs=…, error=task.error or "")`。
  **既有的发事件与写状态先原样留着。**
- `suspend.py`：填 `SUSPENDED_ON_CHILDREN` + `summary` + `spawn_titles`（就在它算出 `titles` 之后）。
- `runtime.py` 的 `except HitlPark`：填 `AWAITING_HUMAN` + `hitl_id=park.hitl_id`。
- `runtime.py` 的 `except LLMOutageError`：填 `INTERRUPTED` + `reason="llm_outage"` +
  `error_code="llm_outage"` + `retriable=False`。
- `runtime.py` 的泛 `except Exception`：填 `INTERRUPTED` + `reason="run_crash"` +
  `error_code=crash_error_code(exc)` + `retriable=getattr(exc, "retriable", True)`。
- `runtime.py` 的 `except asyncio.CancelledError`：填 `CANCELED`。

**`observe.py` / `control_capability.py` / `act.py` 本 task 不动**——
它们写的是中间态，Task 4 一起清。

**`TaskFinalized` 也不动，全程留在 `FinalizeStep`**：它**不写 task 状态**
（reducer 拿它写 `outputs`/`error`/`finished_at`），是结果记录不是状态转移，
所以不在本次收口范围内。Task 4 的静态守卫列表里也没有它——这是刻意的。

- [ ] **Step 5: 跑测试 + 全量**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_loop_reports_outcome.py -v`
然后 `./.venv/Scripts/python.exe -m pytest tests -q 2>&1 | grep "^FAILED"`
Expected: 新测试全过；全量**仍恰好 2 条基线**（本 task 纯新增，旧路径未动）

- [ ] **Step 6: 提交**

```bash
git add -A src tests
git commit -m "feat(task): loop 在 run 结束时产出 RunOutcome（尚无消费者）"
```

---

## Task 3: `_run_loop` 把 `RunOutcome` 交出来，`RunFinished` 改说 run 的结局

**Files:**
- Modify: `src/ctx_weft/core/runtime.py`（`_run_loop` 返回值 / `RunFinished` payload）
- Modify: `docs/events-v2.md` §2.4、`docs/spec/01-events.md`
- Test: `tests/unit/test_run_finished_outcome.py`

**Interfaces:**
- Consumes: Task 2 的 `state.run_outcome`
- Produces: `RunFinished.payload["outcome"]`（run 词表）；`_run_loop` 的返回值携带 `RunOutcome`

- [ ] **Step 1: 写失败的测试**

```python
"""RunFinished 说的是 run 自己的结局，不是 task 的状态（Task 3）。"""

from __future__ import annotations

from ctx_weft.protocols.events import EventType


async def test_run_finished_carries_the_run_outcome_not_task_status(outage_case):
    bus = await outage_case.run()
    ev = next(e for e in bus.events if e.type == EventType.RUN_FINISHED)
    assert ev.payload["outcome"] == "interrupted"


async def test_park_run_reports_awaiting_human(park_case):
    bus = await park_case.run()
    ev = next(e for e in bus.events if e.type == EventType.RUN_FINISHED)
    assert ev.payload["outcome"] == "awaiting_human"


async def test_normal_run_reports_completed(normal_case):
    bus = await normal_case.run()
    ev = next(e for e in bus.events if e.type == EventType.RUN_FINISHED)
    assert ev.payload["outcome"] == "completed"


async def test_will_retry_is_unchanged(normal_case):
    """will_retry 是给 host 决定关不关流的，语义不变。"""
    bus = await normal_case.run()
    ev = next(e for e in bus.events if e.type == EventType.RUN_FINISHED)
    assert "will_retry" in ev.payload
```

（搭台方式同 Task 2 的说明：自包含，参照 `test_run_loop_outage.py`。）

- [ ] **Step 2: 跑测试确认失败**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_run_finished_outcome.py -v`
Expected: FAIL —— payload 里还是 `final_status`

- [ ] **Step 3: 改 `RunFinished` 的 payload**

把 `"final_status": task.status` 换成 `"outcome": state.run_outcome.kind`
（`state.run_outcome` 为 `None` 时——正常跑完那条路——填 `RunOutcomeKind.COMPLETED`）。

**`final_status` 键本身保留一个发布周期**，值仍填 `task.status`，
并在 payload 里加注释说明它已废弃、host 应改读 `outcome`。
（理由：`RunFinished` 是 host 关流的信号之一，直接抽掉键会让 host 当场炸；
`outcome` 先上、`final_status` 下个周期再删。）

- [ ] **Step 4: `_run_loop` 把结局交出去**

`_run_loop` 今天返回 `state`。`state.run_outcome` 已在其上，所以**不必改签名**——
但要确保 `finally` 之后 `state` 的引用是带着 `run_outcome` 的那个
（注意 `state.apply_patch` 会返回**新对象**，别把结局丢在旧引用上）。
在报告里写明你怎么确认这一点的。

- [ ] **Step 5: 跑测试 + 全量**

既有断言里凡是读 `RunFinished.payload["final_status"]` 的**都还能过**（键还在）。
Expected: 新测试全过；全量仍恰好 2 条基线。

- [ ] **Step 6: 文档**

`docs/events-v2.md` §2.4 与 `docs/spec/01-events.md` 的 `RunFinished` 一行：
`outcome` 为新的权威字段（run 词表五值），`final_status` 标注为**已废弃、下周期删除**。

- [ ] **Step 7: 提交**

```bash
git add -A src tests docs
git commit -m "feat(run): RunFinished 改说 run 自己的结局，final_status 标记废弃"
```

---

## Task 4: TaskManager 接管——写状态、发事件，loop 侧全部删掉

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/task_manager.py`（`_run_task` 消费 `RunOutcome`）
- Modify: `src/ctx_weft/core/loop/steps/finalize.py`（删 4 条事件发射 + 重试耗尽判断）
- Modify: `src/ctx_weft/core/loop/steps/suspend.py`（删 `TaskSuspended` 发射）
- Modify: `src/ctx_weft/core/loop/steps/observe.py`（`_apply_assessment` 不写 `task.status`）
- Modify: `src/ctx_weft/core/orchestrator/control_capability.py`（不写 `task.status`）
- Modify: `src/ctx_weft/core/loop/steps/act.py`（不写 `task.status`）
- Modify: `src/ctx_weft/core/runtime.py`（删 `TaskAwaitingHuman` / `TaskInterrupted` / `TaskCanceled` 发射与状态写）
- Test: `tests/unit/test_task_manager_owns_status.py`

**Interfaces:**
- Consumes: Task 1 的 `disposition_for`、Task 2/3 的 `state.run_outcome`
- Produces: `TaskManager` 是 task 状态事件的唯一发射者

**这是破坏性的一个 task，会让一批既有测试变红。**

- [ ] **Step 1: 写失败的测试**

```python
"""task 状态事件只能从 TaskManager 发出（Task 4）。"""

from __future__ import annotations

import pathlib
import re

TASK_STATUS_EVENTS = (
    "TASK_FINISHED", "TASK_FAILED", "TASK_REQUEUED", "TASK_SUSPENDED",
    "TASK_AWAITING_HUMAN", "TASK_INTERRUPTED", "TASK_CANCELED",
)


def test_only_task_manager_emits_task_status_events():
    """loop 侧（steps/ 与 runtime.py）一条 task 状态事件都不许发。"""
    src = pathlib.Path("src/ctx_weft")
    offenders = []
    for p in src.rglob("*.py"):
        if p.name in ("task_manager.py", "events.py", "reducers.py"):
            continue
        text = p.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "emit" not in line and "make_event" not in line:
                continue
            for name in TASK_STATUS_EVENTS:
                if f"EventType.{name}" in line:
                    offenders.append(f"{p}:{lineno}:{name}")
    assert offenders == []


def test_loop_side_does_not_write_task_status():
    """判决归 loop，状态归 TM——loop 侧不许出现 task.status 的赋值。"""
    targets = [
        "src/ctx_weft/core/loop/steps/observe.py",
        "src/ctx_weft/core/loop/steps/finalize.py",
        "src/ctx_weft/core/loop/steps/act.py",
        "src/ctx_weft/core/orchestrator/control_capability.py",
    ]
    pat = re.compile(r"\btask\.status\s*=|\bctx\.task\.status\s*=|state\.task\.status\s*=")
    offenders = [
        f"{t}:{i}" for t in targets
        for i, line in enumerate(pathlib.Path(t).read_text(encoding="utf-8").splitlines(), 1)
        if pat.search(line)
    ]
    assert offenders == []
```

加一条行为测试（自包含搭台，参照 `test_run_crash_suspend.py`）：

```python
async def test_retry_exhausted_now_decided_by_task_manager(crash_case):
    """重试耗尽的降级判断从 FinalizeStep 搬到了 TM，结果必须一模一样。"""
    from ctx_weft.protocols.events import EventType
    bus = await crash_case.run(retry_count=3, max_retries=3, verdict="retry")
    ev = next(e for e in bus.events if e.type == EventType.TASK_FAILED)
    assert ev.payload["error_code"] == "TASK_FAILED_RETRY_EXHAUSTED"
```

- [ ] **Step 2: 跑测试确认失败**

Expected: 两条静态守卫各列出一串 offender

- [ ] **Step 3: TM 消费 `RunOutcome`**

`task_manager.py` 的 `_run_task`，把今天那段「读 `task.status` 分派」
（`_PARKED_STATUSES` → 挂起出口 / `PENDING` → 重排 / 其余 → `on_task_finished`）
换成：

```python
            outcome = await self._runner.execute(binding, task_id)   # 见 Step 3 末尾
            disp = disposition_for(outcome, retry_count=task.retry_count,
                                   max_retries=task.max_retries)
            task.status = disp.status
            if disp.event_type == "TaskRequeued":
                task.retry_count = disp.payload["retry_count"]
            await self._emit(EventType(disp.event_type), task_id=task_id,
                             payload=disp.payload)
```

然后按 `disp.status` 走既有的三条出口（parked / PENDING 重排 / 终态收尾）——
**出口逻辑本身不要改**，只是判据从「loop 写的 status」变成「处置表算的 status」。

**`RunOutcome` 怎么传到这里**：`_runner.execute(binding, task_id)` 今天返回 `None`。
把它改成返回 `RunOutcome`——`TaskRunner` 协议、`runtime` 侧的实现、以及测试里的
替身 runner 都要跟着改签名。`_run_loop` 里 `state` 会被 `apply_patch` 换成新对象，
所以**返回值要取自 `_run_loop` 最终返回的那个 `state`**，不是入口那个。
若某条路径没有 `run_outcome`（正常跑完且 finalize 未填），
按 `RunOutcomeKind.COMPLETED` + `verdict="success"` 兜底，并在报告里说明哪条路径会落到兜底。

- [ ] **Step 4: 删 loop 侧的发射与状态写**

逐个删（**只删发射与状态赋值，别动其它逻辑**）：
`finalize.py` 的四条事件 + `retry_exhausted` 判断；`suspend.py` 的 `TaskSuspended`；
`runtime.py` 三条；`observe.py::_apply_assessment` 的三处 `task.status = …`
（保留 `observer_outcome` / `task_summary` / `actor_done` 的写入——那些是判决）；
`control_capability.py` 的五处；`act.py` 的三处。

- [ ] **Step 5: 跑全量，逐条改断言**

Run: `./.venv/Scripts/python.exe -m pytest tests -q 2>&1 | grep "^FAILED"`

会红一批。**改断言、不改行为**：红是因为「事件从哪发变了 / 事件序列变了」就改断言；
红是因为「某个状态不该到达了」**停下来**——那是实现错了。
报告里把这两类分开列，**每一条都要说清属于哪类**。

- [ ] **Step 6: 提交**

```bash
git add -A src tests
git commit -m "refactor(task)!: task 状态与事件收归 TaskManager，loop 只报结局"
```

---

## Task 5: 收口验证与文档

**Files:**
- Modify: `docs/events-v2.md`（§2.3 各事件的「谁发」列）
- Modify: `docs/spec/03-reducer-rules.md`（`TASK_STATUS_BY_EVENT` 段的说明）
- Create: `docs/upgrade/2026-09-02-task-status-ownership.md`
- Modify: `docs/spec/golden/` 中受影响的 fixture
- Test: 既有 `tests/unit/test_golden_conformance.py`

- [ ] **Step 1: golden 逐份核对**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_golden_conformance.py -v`

**预期内的差异只有一类**：task 事件的**发射者**变了（`run_id` 可能从有变无）。
**task 的最终状态必须逐字相同**——这是本计划「行为等价优先」那条约束的验收点。
撞上别的差异**停下来查清**，不要改 golden 迁就。

**payload 一律从发射点抄，不许从相邻事件类推**——本仓在这上面栽过：
有人照相邻事件编了个不存在的字段，那份 fixture 反过来成了写 spec 时的「证据」。

- [ ] **Step 2: 文档**

`docs/events-v2.md` §2.3 每条 task 事件的「谁发」统一改成 `TaskManager`；
§2.4 补一句「Run 事件不写 task 状态」。
`03-reducer-rules.md` 的映射表说明补一句「这些事件现在只从 `TaskManager` 发出」。

- [ ] **Step 3: host 升级须知**

`docs/upgrade/2026-09-02-task-status-ownership.md`，第一段就写破坏性变更：

- `RunFinished` 新增 `outcome`（run 词表五值），`final_status` **已废弃、下周期删除**；
  host 若用它推 task 状态，改订 task 事件。
- task 状态事件现在**只从 `TaskManager` 发出**；部分事件的 `run_id` 从有变为 `None`
  （TM 不属于任何一次 run）。host 若按 `run_id` 归组 task 事件，要改。
- 事件语义与最终状态**一个都没变**——这是纯所有权重构。

- [ ] **Step 4: 全量 + lint**

Run: `./.venv/Scripts/python.exe -m pytest tests -q 2>&1 | grep "^FAILED"`
与 `./.venv/Scripts/python.exe -m ruff check src tests`
Expected: 恰好 2 条基线；ruff 无新增类别

- [ ] **Step 5: 提交**

```bash
git add -A src tests docs
git commit -m "docs(task): task 状态所有权收口的 spec、golden 与 host 升级须知"
```

---

## 完成判据

- [ ] `src/ctx_weft` 里除 `task_manager.py` / `events.py` / `reducers.py` 外，
      **零** task 状态事件发射点（静态守卫钉住）
- [ ] `observe.py` / `finalize.py` / `act.py` / `control_capability.py` 里**零** `task.status` 赋值
- [ ] 重试耗尽的降级判断只在 `disposition_for` 一处
- [ ] `RunFinished.payload["outcome"]` 取 run 词表五值之一
- [ ] golden 里各 task 的**最终状态**与重构前逐字相同
- [ ] 全量 FAILED 恰好 2 条（基线）

---

## 留给下一次的尾巴

- `RunFinished.final_status` 的最终删除（本计划只标废弃，给 host 一个周期）
- `forget_session`：生产路径零调用，`_states` 只增不减，回收时机待定
- `test_spec_reducer_rules_sync.py` 守卫测试（spec 与实现的机械对拍）
- `error_code` 仍是「谁想起来谁写」——落 `INTERRUPTED` 时不强制要码
- `test_dispatch_boundary_recap_e2e` 的 flake
