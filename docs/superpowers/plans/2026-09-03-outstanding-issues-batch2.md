# 遗留问题批次二 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 清掉全部死代码与死值域，修四条小而真的行为缺陷，加固三道静态守卫，并把文档漂移一次性收干净。

**Architecture:** 七个任务。前两个清死代码（枚举/字段/变量/冗余写），中间三个各修一类真缺陷（租户漏填、终态守卫缺失、payload 不对称 / sequence 重号 / 孤儿 run），第六个加固守卫（字符串形态 + cwd 依赖 + 补零覆盖的分支），第七个收文档漂移。**不引入任何新契约** —— 需要新事件或新 View 字段的四条（A8/A9/B4/C4）留给批次三。

**Tech Stack:** Python **3.11.4**（实测；不要用 3.12 才有的特性），事件溯源（`EventType` / reducer / `*View` 投影 / 快照），pytest，ruff。

**Spec:** `docs/follow-ups/2026-09-03-outstanding-issues.md`（批次一已修 A1/A2/A3/B1/B3/C1/C2/D2 与 C7；本计划接着修其余可在不改契约前提下解决的条目）

## Global Constraints

- **行为等价优先**：除本计划显式裁定的修复外，**不改变任何 task 的最终状态转移结果**。
- 全量基线：`./.venv/Scripts/python.exe -m pytest tests/unit -q` **恰好 1 条既有失败**
  `tests/unit/test_observe_outcomes.py::test_default_role_prompt_uses_two_fields`
  （根因已查明：`resources/` 目录在本仓与上一级**都不存在**，真·环境缺失，**不要修它**）。
- golden 基线：**31 collected / 31 passed / 0 skipped**。
- 已知 flake（撞上单独重跑确认）：`test_dispatch_boundary_recap_e2e`、
  `test_finish_plus_delegate_same_batch_e2e`、`test_hitl_multimodal_validation.py`。
- 新代码 `from __future__ import annotations`；ruff line-length 100；
  **提交前跑 `ruff check --select I001,F401,RUF100`**。既有 RUF001/002/003（中文标点）是基线。
- 测试一律用仓内 venv：`./.venv/Scripts/python.exe -m pytest ...`
- **不要读 `docs/events-v2.md` 当事实来源** —— 它含「已定案、未实施」的内容（总账 E9）。
  本计划 Task 7 会处理它。
- **payload 一律从实际发射点抄**，不许从相邻事件或文档推断。

## 关键事实（已由控制方核实，可直接依赖）

1. **删 `EventType` 成员不会炸重放**：`providers/events/store/sql/store.py:207-213` 的
   `_row_to_event` 用 `type=row.type` **纯字符串赋值**，不经 `EventType(...)` 构造。
   存量里被删类型的字符串只是不再匹配任何 reducer 分支，退化成 no-op。
   golden 里也一个都没有（已逐个 grep）。
2. **真死枚举是 21 个，不是总账写的 29 个**。差额那 8 个
   （`SessionStatusChanged` / `SessionPausedHitl` / `HitlRequired` / `HitlApproved` /
   `HitlAnswered` / `HitlRejected` / `HitlModified` / `HitlCancelled`）
   **是 L 档，reducer 还在读存量日志，一个都不能删**。
3. 批次一已把 `_ALLOWED_STATUS_WRITES` 清成空 `frozenset()`，
   两条所有权不变量目前**零例外** —— 本计划不得开新豁免。

## 不在本批次（留给批次三，各有明确理由）

| 条目 | 为什么留 |
|---|---|
| **A8** 非终态任务的中途产出不可恢复 | 需要**新事件**（或让 `TaskSuspended` 携带 outputs 快照）——新契约 |
| **A9** `error_code` 不进投影 | 需要给 `TaskView` **加字段**，得走批次一 Task 2 建立的完整维护清单 |
| **B4** `retriable` 契约只有注释在守 | 需要 `outage_run_outcome` 工厂 + `__post_init__` 校验——改构造契约 |
| **B5** 「唯一真源」七份判据 | 收敛 `_handle_task_failure` / `will_retry` 是**行为面重构**，风险与批次一 T4 同级，值得独立立项 |
| **C3** `MemoryCompacted` 五个发射点键集不同 | 统一 payload 是对外契约变更，需 host 协同 |
| **C4** 25 类事件无 `run_id`/`sequence` | 结构性——要给 TM 一个 session 级单调序列 |
| **R10** `"mechanical"` 拆终态/retry 两个 boundary | 与 A8 同一片设计，一起做更省 |

---

## File Structure

| 文件 | 责任 | 涉及任务 |
|---|---|---|
| `src/ctx_weft/protocols/events.py` | 事件枚举 | T1 |
| `src/ctx_weft/core/control/types.py` | 投影对象 | T1 |
| `src/ctx_weft/core/control/tokens.py` | 控制令牌 | T2 |
| `src/ctx_weft/core/loop/driver.py` | step 驱动 | T2 |
| `src/ctx_weft/core/runtime.py` | run 循环 / 恢复 | T2, T3, T4, T5 |
| `src/ctx_weft/core/orchestrator/task_manager.py` | task 所有者 | T2, T3, T4 |
| `src/ctx_weft/core/hitl/service.py` | HITL 事实发射 | T3 |
| `src/ctx_weft/core/orchestrator/session_manager.py` | 会话状态 | T3 |
| `src/ctx_weft/core/loop/steps/background_observe.py` | 后台 recap | T5 |
| `tests/unit/test_task_manager_owns_status.py` | 两道所有权守卫 | T6 |
| `tests/unit/test_discriminators.py` | 判别值守卫 | T6 |
| `docs/events-v2.md`、`docs/spec/*` | 事件目录与规格 | T7 |

---

## Task 1: 删死枚举与死字段

**Files:**
- Modify: `src/ctx_weft/protocols/events.py`（删 21 个成员）
- Modify: `src/ctx_weft/core/control/types.py`（删 `RunStateView.extra` / `snapshot_at`）
- Test: `tests/unit/test_no_dead_event_types.py`（新建）

**Interfaces:**
- Produces: 一道「新增 `EventType` 成员必须有发射点」的守卫，后续任务与批次三都受它约束。

**背景**：`EventType` 里有 **21 个成员零发射、零引用**（控制方已逐个核实）。
另有 8 个是 **L 档**（`SessionStatusChanged` / `SessionPausedHitl` / 六条 legacy HITL），
reducer 还在读存量日志 —— **一个都不能删**。

`RunStateView.extra` 与 `snapshot_at` 全仓零读写（`types.py:107/109`）。
注意 `snapshot_at` 的同名字段存在于 `RunSnapshot`（`protocols/events.py`），**那是另一个类，不要动**。

- [ ] **Step 1: 写失败的测试**

新建 `tests/unit/test_no_dead_event_types.py`：

```python
"""EventType 的每个成员都必须有发射点，或明确登记为 L 档（只读存量）。

死枚举的代价不是运行期开销，是**读代码的人被误导** —— 他会以为那条事件会发生，
去写消费分支、去等一个永远不来的信号。本守卫把「定义即必须发射」变成红灯。
"""

from __future__ import annotations

import pathlib
import re

from ctx_weft.protocols.events import EventType

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src" / "ctx_weft"

#: L 档：已停止发射，但 reducer 仍读它们以重放存量日志。删除须过退役闸门。
_LEGACY_READ_ONLY = frozenset({
    "SessionStatusChanged", "SessionPausedHitl",
    "HitlRequired", "HitlApproved", "HitlAnswered", "HitlRejected",
    "HitlModified", "HitlCancelled",
})


def _referenced_outside_definition(member: str, value: str) -> bool:
    for p in _SRC.rglob("*.py"):
        if p.name == "events.py":
            continue
        text = p.read_text(encoding="utf-8")
        if f"EventType.{member}" in text or f'"{value}"' in text:
            return True
    return False


def test_guard_scans_a_real_tree_not_an_empty_one():
    """零扫描也会报绿——把「扫到了东西」本身变成断言。"""
    assert len(list(_SRC.rglob("*.py"))) > 50
    assert (_SRC / "protocols" / "events.py").exists()


def test_every_event_type_is_emitted_or_registered_legacy():
    dead = [
        m.name for m in EventType
        if m.value not in _LEGACY_READ_ONLY
        and not _referenced_outside_definition(m.name, m.value)
    ]
    assert dead == [], (
        f"这些 EventType 成员零发射、零引用，且未登记为 L 档: {dead}。"
        "要么给它发射点，要么删掉，要么登记进 _LEGACY_READ_ONLY 并说明理由。"
    )


def test_legacy_read_only_members_still_exist():
    """L 档成员不许被顺手删掉——reducer 还要用它们读存量日志。"""
    values = {m.value for m in EventType}
    missing = _LEGACY_READ_ONLY - values
    assert missing == set(), f"L 档成员被删了: {sorted(missing)}"
```

- [ ] **Step 2: 跑测试确认它按预期失败**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_no_dead_event_types.py -v`
Expected: `test_every_event_type_is_emitted_or_registered_legacy` FAIL，
列出 **21 个**成员。**若数量不是 21，停下来报告** —— 说明控制方的核实与你的实测不符。

- [ ] **Step 3: 删掉那 21 个成员**

按测试报出的清单删。预期是这些（控制方已核）：
`RunPaused` `RunResumed` `AgentStatusChanged` `AgentWaiting` `AgentFinalized`
`ContextTokensMeasured` `ContextOverflowed` `CapabilityFailed` `CapabilityCanceled`
`CompactTriggered` `CompactDispatched` `MemoryCompactFailedFallback`
`BlackboardSubscribed` `HitlTimeout` `TokenBudgetWarning` `TokenBudgetExceeded`
`MaxConcurrentAgentsExceeded` `MCPServerDisconnected` `MCPServerReconnected`
`EventsDropped` `SnapshotCreated`

**删除是安全的**（控制方已核实）：`providers/events/store/sql/store.py:207-213` 的
`_row_to_event` 用 `type=row.type` **纯字符串赋值**，不经 `EventType(...)` 构造 ——
存量事件里这些字符串只是不再匹配任何分支，退化成 no-op，不会抛错。

- [ ] **Step 4: 删 `RunStateView` 的两个死字段**

`src/ctx_weft/core/control/types.py:107/109` 的 `extra` 与 `snapshot_at`。

⚠️ **`snapshot_at` 在 `RunSnapshot`（`protocols/events.py`）上有同名字段，那是另一个类，不要动。**

删完跑批次一建的那道快照守卫（`tests/unit/test_view_serialization_coverage.py`）——
它的 `_EXEMPT["RunStateView"]` 里列了这两个名字，**一并删掉那两个豁免条目**
（字段没了，豁免也就不需要了）。

- [ ] **Step 5: 跑测试 + 全量 + golden**

- [ ] **Step 6: 提交**

```bash
git add -A && git commit -m "chore(events): 删 21 个死枚举成员与两个死字段，补「定义即必须发射」守卫"
```

---

## Task 2: 删死代码与冗余写

**Files:**
- Modify: `src/ctx_weft/core/loop/driver.py`（`:257` 的死条件）
- Modify: `src/ctx_weft/core/runtime.py`（`:1753` 的死变量、outage 支的冗余写）
- Modify: `src/ctx_weft/core/control/tokens.py`（删 `Deadline`）
- Modify: `src/ctx_weft/core/control/__init__.py`（去掉 `Deadline` 的 re-export）
- Modify: `src/ctx_weft/core/orchestrator/task_manager.py`（`_suspend_task_interrupted` 吞 reason）

**Interfaces:**
- Consumes: 无
- Produces: `_suspend_task_interrupted(task_id, error, exc, *, reason)` —— 新增必传 kw-only 参数

**四处死代码 / 冗余（控制方已逐个定位）**：

1. `driver.py:257` 的 `getattr(tok, "mode", "cancel") == "cancel"` ——
   `CancelToken` **没有 `mode` 属性**，该条件恒真，是历史残留
   （曾经 CancelToken 上挂过 pause 模式）。
2. `runtime.py:1753` 的 `token = CancelToken()` —— 创建后**从未传给任何人、从未 cancel**，
   纯占位（idle-guard 已改用 `_busy_sessions`）。
3. `tokens.py` 的 `Deadline` 类 —— 全仓零使用，只在 `control/__init__.py` 被 re-export。
4. outage 支的 `task.error` / `task.error_code` 在 `runtime.py`（约 `:2545`）与
   `apply_run_outcome`（`task_manager.py:531-535`）**各写一份**，值相同，纯冗余。

**外加一处吞值**：`_suspend_task_interrupted` 的签名里**根本没有 `reason` 形参**
（`task_manager.py:784` 附近），发射处硬编码 `"run_crash"`。
调用方传的 `InterruptReason.ASSEMBLY_FAILURE` 只在**重试**支被用上，
落到挂起支时被丢弃 —— **同一次装配失败的两个出口 reason 不同源**。

- [ ] **Step 1: 写失败的测试**

新建 `tests/unit/test_assembly_failure_reason.py`：

```python
"""装配失败的两个出口必须报同一个 reason（总账 D6）。"""

from __future__ import annotations

import pytest

from ctx_weft.core.discriminators import InterruptReason
from ctx_weft.protocols.events import EventType


@pytest.mark.asyncio
async def test_assembly_failure_suspend_carries_assembly_failure_reason(tm_with_bus):
    """不可重试的装配失败 → TaskInterrupted.reason 必须是 assembly_failure，不是 run_crash。"""
    tm, bus = tm_with_bus
    await tm._handle_task_failure(
        "tsk_1", error="boom", exc=_NonRetriable(), reason=InterruptReason.ASSEMBLY_FAILURE,
    )
    ev = next(e for e in bus.events if e.type == EventType.TASK_INTERRUPTED)
    assert ev.payload["reason"] == InterruptReason.ASSEMBLY_FAILURE


@pytest.mark.asyncio
async def test_run_crash_suspend_still_carries_run_crash(tm_with_bus):
    """执行崩溃那条路不受影响。"""
    tm, bus = tm_with_bus
    await tm._handle_task_failure(
        "tsk_1", error="boom", exc=_NonRetriable(), reason=InterruptReason.RUN_CRASH,
    )
    ev = next(e for e in bus.events if e.type == EventType.TASK_INTERRUPTED)
    assert ev.payload["reason"] == InterruptReason.RUN_CRASH
```

`tm_with_bus` fixture 与 `_NonRetriable` 按 `tests/unit/test_run_crash_suspend.py`
既有的搭台手法写（那个文件已经在造带 bus 的 TaskManager）。
**先读它，照其风格**，不要自己发明新搭台。

- [ ] **Step 2: 跑测试确认失败**

Expected: 第一条 FAIL（实际报 `"run_crash"`），第二条 PASS。

- [ ] **Step 3: 给 `_suspend_task_interrupted` 加 `reason` 参数**

改签名为 kw-only 必传，发射处用它替换硬编码：

```python
    async def _suspend_task_interrupted(
        self, task_id: str, error: str, exc: BaseException | None, *, reason: str,
    ) -> None:
```

发射处（原硬编码 `"run_crash"` 那行）改成 `"reason": reason`。
两个调用点（`_handle_task_failure` 的不可重试支与预算耗尽支）把自己收到的 `reason` 透传下去。

- [ ] **Step 4: 删三处死代码**

- `driver.py:257`：去掉 `and getattr(tok, "mode", "cancel") == "cancel"`，
  并**在注释里说明**曾经有过 pause 模式、现已退役（别让下一个人以为漏了判断）。
- `runtime.py:1753`：删 `token = CancelToken()` 那一行；若 `CancelToken` 的 import
  因此变成未使用，一并清掉。
- `tokens.py` 的 `Deadline` 类 + `control/__init__.py` 的 re-export。

- [ ] **Step 5: 删 outage 支的冗余写**

`runtime.py` 的 outage 分支里那两行 `task.error_code = ...` / `task.error = ...`
删掉 —— `apply_run_outcome`（`task_manager.py:531-535`）会从 `RunOutcome` 写同样的值。

⚠️ **删之前确认时序**：`announce_queue_state` 读 `task.error_code` 做分流，
而它发生在 `_settle` 里、`apply_run_outcome` **之后**。
**跑一遍 `tests/unit/test_outage_interrupt_reason.py` 确认没红**；红了就停下来报告。

- [ ] **Step 6: 全量 + golden**

- [ ] **Step 7: 提交**

---

## Task 3: `tenant_id` 三处漏填

**Files:**
- Modify: `src/ctx_weft/core/hitl/service.py`（`_emit`）
- Modify: `src/ctx_weft/core/runtime.py`（`_announce_queue_state_as_tm_proxy`）
- Modify: `src/ctx_weft/core/orchestrator/session_manager.py`（`_make_root_task_manager`）
- Test: `tests/unit/test_tenant_id_propagation.py`（新建）

**背景（总账 A5）**：三处发射点漏填 `tenant_id`，落到 `Event` 的默认值 `"default"`：

1. `HitlService._emit`（`hitl/service.py:187` 附近）→ `HitlOpened` / `HitlResolved`
2. `runtime._announce_queue_state_as_tm_proxy`（`runtime.py:2204` 附近）→ 恢复期的两条队列信号
3. **root task 的 `TaskCreated`** —— `_make_root_task_manager`
   （`session_manager.py:359-369`）先建 TM 就立刻 `push_task`，**从不调 `set_session`**；
   而 `TaskManager._emit` 取 `self._session.tenant_id if self._session else "default"`。

**同一 session 的其它事件都带真 tenant，这几条不带** → 非 default 租户的投影租户错。

- [ ] **Step 1: 写失败的测试**

```python
"""同一会话的全部事件必须带同一个 tenant_id（总账 A5）。"""

from __future__ import annotations

import pytest

from ctx_weft.protocols.events import EventType


@pytest.mark.asyncio
async def test_every_event_of_a_tenant_session_carries_that_tenant(runtime_with_tenant):
    """非 default 租户跑一轮，事件流里不许出现 tenant_id == "default"。"""
    rt, bus = runtime_with_tenant  # tenant_id="acme"
    await _drive_one_task_with_hitl(rt)
    offenders = [
        f"{e.type}:{e.tenant_id}" for e in bus.events if e.tenant_id != "acme"
    ]
    assert offenders == [], f"这些事件掉回了 default 租户: {offenders}"
```

`runtime_with_tenant` 与 `_drive_one_task_with_hitl` 要你自己写 ——
**先读 `tests/unit/test_hitl_recovery_v2.py` 与 `tests/unit/test_runtime_hitl_wiring.py`**，
它们已有造真 runtime + 走 HITL 的搭台，照其风格。

若造完整 runtime 成本过高，退一步：对三个发射点各写一条针对性单测
（直接构造发射方、断言事件的 `tenant_id`）。**退化时在报告里说明理由。**

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 修 `HitlService._emit`**

`HitlRequest` 上有没有 `tenant_id`？**去看** —— 若有就用它；
若没有，`HitlService` 构造时是否拿得到 session 的 tenant？
**两条路都不通就停下来报告**（那说明需要给 `HitlRequest` 加字段，属新契约，不在本批次）。

- [ ] **Step 4: 修恢复期的队列信号代发**

`_announce_queue_state_as_tm_proxy` 手上有 `session_id`，
从投影或 `SessionManager._states` 取 `tenant_id`（`_SessionState` 上有这个字段）。

- [ ] **Step 5: 修 root task 的 `TaskCreated`**

`_make_root_task_manager` 建完 TM 之后、`push_task` **之前**调 `set_session`。
⚠️ **确认 `set_session` 在那个时点拿得到 `Session` 对象**；
若拿不到，改为把 `tenant_id` 直接传给 TM 的构造器。**以实测为准。**

- [ ] **Step 6: 全量 + golden**

golden 里若有 fixture 断言了 `tenantId`，核对是否需要更新 —— **更新前先确认它断言的是真实行为**。

- [ ] **Step 7: 提交**

---

## Task 4: `_handle_task_failure` 补终态守卫

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/task_manager.py`
- Test: `tests/unit/test_terminal_guard_on_assembly_failure.py`（新建）

**背景（总账 A7）**：`apply_run_outcome` 有终态守卫（`task_manager.py:516-518`），
`_handle_task_failure` / `_suspend_task_interrupted` **一个都没有**。

**后果**：熔断 trip 已把某 task 判 `FAILED`（`task_manager.py` 约 `:1017` 发 `TaskFailed`）后，
若该 task 的**装配**随后失败，`_suspend_task_interrupted` 会无条件写
`task.status = "INTERRUPTED"` 并发 `TaskInterrupted` —— **把写定的终态盖回非终态**。

- [ ] **Step 1: 写失败的测试**

```python
"""终态不复活：熔断判死之后的装配失败不得把 FAILED 盖回 INTERRUPTED（总账 A7）。"""

from __future__ import annotations

import pytest

from ctx_weft.core.discriminators import InterruptReason
from ctx_weft.protocols.events import EventType


@pytest.mark.asyncio
async def test_assembly_failure_does_not_resurrect_a_terminal_task(tm_with_bus):
    tm, bus = tm_with_bus
    task = tm.get_task("tsk_1")
    task.status = "FAILED"          # 熔断已判死
    before = len(bus.events)

    await tm._handle_task_failure(
        "tsk_1", error="boom", exc=RuntimeError("x"),
        reason=InterruptReason.ASSEMBLY_FAILURE,
    )

    assert task.status == "FAILED", "终态被盖回了非终态"
    assert len(bus.events) == before, "终态 task 不该再发任何事件"


@pytest.mark.asyncio
async def test_non_terminal_task_still_handled(tm_with_bus):
    """守卫只挡终态——非终态照常走原逻辑。"""
    tm, bus = tm_with_bus
    tm.get_task("tsk_1").status = "ACTIVE"
    await tm._handle_task_failure(
        "tsk_1", error="boom", exc=RuntimeError("x"),
        reason=InterruptReason.ASSEMBLY_FAILURE,
    )
    assert any(e.type in (EventType.TASK_REQUEUED, EventType.TASK_INTERRUPTED)
               for e in bus.events)
```

- [ ] **Step 2: 跑测试确认第一条失败**

- [ ] **Step 3: 加守卫**

在 `_handle_task_failure` 入口加，**与 `apply_run_outcome` 同一手法**：

```python
        task = self._tasks.get(task_id)
        # 终态不复活：熔断 trip 可能已把这个 task 判 FAILED（见 apply_run_outcome
        # 的同源守卫）。此后的装配失败属内部清场，不该把写定的终态盖回非终态。
        if task is None or task.status in _TERMINAL_STATUSES:
            return
```

⚠️ **确认这个早返回不会漏掉必要的队列动作** —— 读一遍 `_handle_task_failure`
后续做了什么（`drain()` / `unmark_running` 之类）。若终态 task 仍需要某些收尾，
**只挡状态写与事件发射，不要挡队列清理**。以实测为准，并在报告里说明你的判断。

- [ ] **Step 4: 全量 + golden**

- [ ] **Step 5: 提交**

---

## Task 5: `sequence` 重号与孤儿 run

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/background_observe.py`（快照方式）
- Modify: `src/ctx_weft/core/runtime.py`（`compact_session` 的孤儿 run）
- Modify: `src/ctx_weft/core/loop/steps/recognize_intent.py`（孤儿 run）
- Test: `tests/unit/test_run_id_sequence_integrity.py`（新建）

**背景（总账 A4 / C5）**：

**A4 —— `(run_id, sequence)` 不唯一。** `launch_background_observe` 用
`dataclasses.replace(state)` 做快照（`background_observe.py:349` 附近）：
**`run_id` 相同，但 `sequence_counter` 是一份独立的 int 副本**（普通 `int` 字段，不可变）。
此后两边各自 `+= 1` —— 后台 recap 那 6 类事件与主 run **重号**。
连带：`RunFinished.total_events` 取主 run 的 counter，**不含后台 recap 的事件**。

**C5 —— 两个孤儿 run。** `recognize_intent`（`recognize_intent.py:43-51`）与
`compact_session`（`runtime.py:1813-1826` 附近）各自造新 `run_id`，
却**没有配套的 `RunStarted` / `RunFinished`** —— host 会看到凭空出现又凭空消失的 run。

- [ ] **Step 1: 写失败的测试**

```python
"""(run_id, sequence) 必须唯一；每个 run_id 必须有起止（总账 A4 / C5）。"""

from __future__ import annotations

import collections

import pytest

from ctx_weft.protocols.events import EventType


def _assert_no_duplicate_sequence(events):
    seen = collections.defaultdict(set)
    dupes = []
    for e in events:
        if e.run_id is None:
            continue          # run 外的事件恒 sequence=0，不参与唯一性
        if e.sequence in seen[e.run_id]:
            dupes.append(f"{e.run_id}:{e.sequence}:{e.type}")
        seen[e.run_id].add(e.sequence)
    assert dupes == [], f"(run_id, sequence) 撞号: {dupes}"


def _assert_every_run_has_start_and_finish(events):
    starts = {e.run_id for e in events if e.type == EventType.RUN_STARTED}
    finishes = {e.run_id for e in events if e.type == EventType.RUN_FINISHED}
    seen = {e.run_id for e in events if e.run_id is not None}
    orphans = sorted(seen - starts) + sorted(seen - finishes)
    assert orphans == [], f"孤儿 run（无 RunStarted 或无 RunFinished）: {orphans}"


@pytest.mark.asyncio
async def test_background_observe_does_not_reuse_main_run_sequence(bus_after_recap_run):
    _assert_no_duplicate_sequence(bus_after_recap_run)


@pytest.mark.asyncio
async def test_every_run_id_has_start_and_finish(bus_after_recap_run):
    _assert_every_run_has_start_and_finish(bus_after_recap_run)
```

`bus_after_recap_run` 要你自己写 —— 跑一个会触发 background observe 的 run，
收集全部事件。**先读 `tests/unit/test_background_observe_wiring.py`**，照其搭台手法。

- [ ] **Step 2: 跑测试确认失败**

Expected: 两条都 FAIL。

- [ ] **Step 3: 修 A4 —— 给后台 recap 自己的 run_id**

**两条路，选后者**：

- ❌ 让后台共享主 run 的 counter：要引入跨协程的可变共享状态，且主 run 可能已结束。
- ✅ **给后台 recap 一个自己的 `run_id`** —— 它本来就是一段独立的工作，
  有自己的起止事件（`TaskRecapStarted` / `TaskRecapDone`）。

在 `launch_background_observe` 里把快照的 `run_id` 换成新生成的：

```python
    snapshot = dataclasses.replace(
        state,
        run_id=generate_id("run"),   # 后台 recap 是独立的一段工作，不蹭主 run 的号
        sequence_counter=0,
    )
```

**这会让后台的 6 类事件换一个 `run_id`** —— 属对外可见变更，Task 7 写进升级须知。

- [ ] **Step 4: 修 C5 —— 给两个孤儿 run 补起止**

`recognize_intent` 与 `compact_session` 各自在造完 `LoopState` 之后、
跑 step 之前发 `RunStarted`，收尾时发 `RunFinished`。
**payload 从 `_run_loop` 的实际发射点抄**（`RunStarted{run_id, initial_step}`；
`RunFinished` 的七个键）—— 但这两条路径没有 `RunOutcome`，
`outcome` 取什么**由你判断并在报告里说明**（建议 `"completed"`，
因为它们跑完就是完成；异常路径若有则另说）。

⚠️ 若发现补起止会让某个既有测试红（例如断言「一个 run 只有一条 RunStarted」），
**停下来报告**。

- [ ] **Step 5: 全量 + golden**

`RunFinished.total_events` 现在只统计主 run —— 那本来就是它的语义，
后台 recap 有自己的 run 了，这条不再是缺陷。**在报告里确认这一点。**

- [ ] **Step 6: 提交**

---

## Task 6: 加固三道守卫

**Files:**
- Modify: `tests/unit/test_task_manager_owns_status.py`（cwd 依赖 + 字符串形态 + 清单理由）
- Modify: `tests/unit/test_discriminators.py`（`_ALLOWED` 粒度）
- Test: `tests/unit/test_agent_llm_changed_projection.py`（新建，补 A6 的零覆盖）

**三处加固**：

**① cwd 依赖（批次一终评的 M1 同类）**：`test_task_manager_owns_status.py:47/77`
用相对路径，**换个 cwd 就扫 0 个文件、报绿**。
批次一已修 `test_discriminators.py`，这两处是基线遗留。
统一成 `pathlib.Path(__file__).resolve().parents[2] / "src" / "ctx_weft"`，
并**各补一条「扫到了东西」的断言**（照 `test_discriminators.py` 里那条写）。

**② 守卫 A 认不出字符串字面量形态（总账 B2）**：它只匹配 `EventType.<NAME>` 属性访问，
而 `task_disposition.py` 自己就用字符串表达事件类型（TM 才 `EventType(...)` 转回来）。
模仿这个写法的新模块能绕过守卫 A。
**加一条按字符串扫的补充判据**，白名单放行 `task_disposition.py`（它是处置表，正当）。

**③ 守卫 A 排除 `TASK_STARTED` 的理由没写进注释**（批次一终评 M3）：
清单比 `TASK_STATUS_BY_EVENT` 少一个 `TASK_STARTED`，**排除是有理由的**
（AST 判据匹配任意 `EventType.X` 属性引用、不只发射，而 `session_manager.py`
有 `EventType.TASK_STARTED: SessionInput.TASK_STARTED` 的查表条目），
但理由只在台账里，下一个读的人只会看到「少一个」。**写进注释。**

**外加 A6 的零覆盖**：`AgentLlmChanged` 的 reducer 用**无条件覆盖**
（空值也写），与兄弟分支 `AgentInstantiated` 的「空值不覆盖」**刻意相反**，
而这个不对称**目前零测试、零 golden 覆盖**。补测试钉住两个方向。

- [ ] **Step 1: 写失败的测试（A6 那条）**

```python
"""AgentLlmChanged 与 AgentInstantiated 的空值口径刻意相反，必须各自钉住（总账 A6）。"""

from __future__ import annotations

from ctx_weft.core.control.reducers import reduce_events
# ... 事件构造 helper 照 tests/unit/test_agent_llm_replay.py 既有写法


def test_agent_llm_changed_overwrites_with_empty():
    """空 ModelChoice 是「切回账号默认」的合法选择，必须能写空。"""
    view = reduce_events([
        _instantiated(account="acct", model="mdl"),
        _llm_changed(account="", model=""),
    ], "run_1")
    assert view.agents["agt_1"].llm_account == ""
    assert view.agents["agt_1"].llm_model == ""


def test_agent_instantiated_does_not_overwrite_with_empty():
    """存量事件不带这两个字段——空值不许覆盖已有选择。"""
    view = reduce_events([
        _instantiated(account="acct", model="mdl"),
        _instantiated(account="", model=""),      # 存量形态
    ], "run_1")
    assert view.agents["agt_1"].llm_account == "acct"
    assert view.agents["agt_1"].llm_model == "mdl"
```

- [ ] **Step 2: 跑测试确认它按预期（应当 PASS —— 这是补覆盖，不是修 bug）**

若有一条 FAIL，**停下来报告** —— 那说明实现与设计意图不符，是真缺陷。

- [ ] **Step 3: 修 cwd 依赖 + 补「扫到了东西」断言**

- [ ] **Step 4: 补守卫 A 的字符串形态判据**

- [ ] **Step 5: 把 `TASK_STARTED` 的排除理由写进注释**

- [ ] **Step 6: 收窄 `test_discriminators.py` 的 `_ALLOWED` 粒度（批次一终评 M2）**

现在是**整份文件**豁免 `_loader.py`，将来该文件真出现散落判别值也抓不到。
改成按 `(路径, 字面量)` 精确放行 —— 只放过 `failure_threshold` 这一个。

- [ ] **Step 7: 做负向对照**

三道守卫**各注入一次**确认变红（含新加的字符串形态判据），
每次 `git checkout --` 撤销，最后 `git status --porcelain` 必须空。**过程写进报告。**

- [ ] **Step 8: 全量 + golden + 提交**

---

## Task 7: 文档漂移一次性收干净

**Files:**
- Modify: `docs/spec/03-reducer-rules.md`（E1，漂移 4 处）
- Modify: `docs/spec/golden/*.json`（E2，`runId`/`sequence` 失真）
- Create: `docs/spec/golden/16-task-human-resolved.json`、`17-agent-llm-changed.json`（E3）
- Modify: `docs/upgrade/2026-09-02-agent-llm-ownership.md`（E4，不实）
- Modify: `src/ctx_weft/core/orchestrator/task_manager.py`、`src/ctx_weft/core/runtime.py`（E6，注释）
- Modify: `docs/events-v2.md`（E9，标注为设计稿）
- Create: `docs/upgrade/2026-09-03-outstanding-issues-batch2.md`

**九条漂移**（总账 E1-E9，逐条修）：

| # | 位置 | 改法 |
|---|---|---|
| E1 | `docs/spec/03-reducer-rules.md` | `TaskResumed` 的映射值写 ACTIVE（代码是 PENDING）；缺 `TaskHumanResolved` 的表项与专属分支；缺 `AgentLlmChanged` 分支 —— **四处逐条对着代码改** |
| E2 | `docs/spec/golden/` | `03` 的 `TaskResumed` 与**八个 golden 里的全部 `TaskStarted`** 错带 `runId` 与非零 `sequence`。实际发射点恒 `run_id=None, sequence=0` —— 逐个改成 `"runId": null, "sequence": 0` |
| E3 | `docs/spec/golden/` | `TaskHumanResolved` 与 `AgentLlmChanged` **零 golden 覆盖**，各补一个 fixture（照 `13-task-canceled.json` 的单事件 fixture 体例） |
| E4 | `docs/upgrade/2026-09-02-agent-llm-ownership.md:28` | 说「派生新 agent 时也发 `AgentLlmChanged`」——**不发**，初始选择搭载在 `AgentInstantiated` 的 payload 里。订正 |
| E6 | `task_manager.py` / `runtime.py` | 两处注释声称会发 `HitlCancelled`，实际 `HitlService.cancel` 发的是 `HitlResolved{outcome: cancelled}`。订正（`HitlCancelled` 本批次 Task 1 已删） |
| E7 | `act_guidance.py` | 批次一 Task 7 已修，**核实一遍即可** |
| E8 | `task_manager.py`（`announce_queue_state`）/ `agent_registry.py`（`load`） | 前者说调用点在「`_run_task` 的挂起出口」（已搬到 `_settle`），漏了 compat 直调与 `_try_resume_parent`；后者说「`AgentView` 只有四个字段」，与代码矛盾。订正 |
| E9 | `docs/events-v2.md` | **在文件开头加一个显眼的状态标注** —— 说明哪些章节是已实施的现状、哪些是「已定案、未实施」的设计稿，并列出后者的具体条目（`TaskOutcomeRecorded` / `TaskRecapCompleted` / `PromptAssembled` / `StepSkipped` / `IntentRecognized`、envelope 的 `origin` 字段、`BackgroundObserve*` → `origin` 的合并）。**不要删这些内容**，它们是设计意图；要让读者一眼知道哪些能引用 |

- [ ] **Step 1: 逐条改（E1 → E9）**

每改一条，**回源核对代码**再写。**不许照抄总账的转述** ——
总账里的行号已多处漂移（批次一终评的 M5）。

- [ ] **Step 2: 补两个 golden fixture（E3）**

payload **从实际发射点抄**：
- `TaskHumanResolved` ← `task_manager.py` 的 `mark_human_resolved`
- `AgentLlmChanged` ← `agent_registry.py` 的 `set_agent_llm`

`AgentLlmChanged` 那个尤其要覆盖**空值无条件覆盖**这条（与 Task 6 的测试呼应）。

- [ ] **Step 3: 写批次二的升级须知**

`docs/upgrade/2026-09-03-outstanding-issues-batch2.md`，至少覆盖：
- **删了 21 个 `EventType` 成员**（从未发射过，故 host 不可能在消费它们；
  但若 host 侧有对应的 switch 分支，可以删了）
- **后台 recap 换了自己的 `run_id`**（Task 5）—— 这是本批次最实打实的对外变更
- **`recognize_intent` / `compact_session` 现在有 `RunStarted`/`RunFinished`**
- `TaskInterrupted.reason` 在装配失败时由 `run_crash` 变 `assembly_failure`（Task 2）
- 三处 `tenant_id` 修复（Task 3）
- 终态守卫（Task 4）

- [ ] **Step 4: 全量 + golden + 提交**

---

## 自查

**Spec 覆盖**：A4(T5) / A5(T3) / A6(T6) / A7(T4) / B2(T6) / C5(T5) /
D1(T1) / D3(T1) / D4(T2) / D6(T2) / E1-E9(T7)，
外加批次一终评留下的三条尾巴（cwd 依赖、`_ALLOWED` 粒度、`TASK_STARTED` 理由）在 T6。
**明确不覆盖**：A8 / A9 / B4 / B5 / C3 / C4 / R10 —— 上面「不在本批次」表里各有理由。

**类型一致性**：T2 产出的 `_suspend_task_interrupted(..., *, reason)` 在 T4 被间接依赖
（T4 的守卫加在 `_handle_task_failure` 入口，不改 T2 的签名）。
T1 产出的「定义即必须发射」守卫会约束 T5 —— **T5 若新增事件类型必须有发射点**
（它不新增，只是给既有事件换 run_id / 补起止）。

**已知风险**：
- T5 Step 3 改后台 recap 的 `run_id` 是**对外可见变更**，且可能撞既有测试 ——
  brief 已要求撞上停下来报告。
- T3 Step 3 的 HITL tenant 可能需要给 `HitlRequest` 加字段（新契约）——
  brief 已要求两条路都不通就停。
- T1 删枚举成员虽已核实安全，但**若 golden 或测试里出现被删的类型名会红** ——
  那是好事（说明有引用），停下来报告。
