# Runtime 对外面向 agent-centric 对齐 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `CtxWeftRuntime` 的对外面收敛成一套——agent 是唯一寻址单位，session 降为成员登记表 / 事件存储分区键 / 资源边界，task 与 run 退回引擎内部。

**Architecture:** 四阶段自下而上。① 协议与读模型地基（`EventFilter.agent_id`、HITL 的 agent 过滤、ALM 只读访问器）——纯增量，不破坏任何现有行为；② run_id 与句柄（删 `_default_run_id` 立起「一轮一个 run_id」，`RunHandle` 换轴成 `TurnHandle`）；③ 恢复换轴（`recover()` 装填 ALM、`recover_session` → `recover_agent`、`TASK_QUEUE_*` 停发改发 `AGENT_*`）；④ 会话级 API 执行部分重建与导出面收口。每阶段内部按「一个可独立验证的交付物」切任务。

**Tech Stack:** Python 3.11+、pytest（`asyncio_mode = "auto"`）、ruff（line-length 100）。

**Spec:** `docs/superpowers/specs/2026-09-04-runtime-agent-centric-surface-design.md`

**上游 spec:** `docs/superpowers/specs/2026-09-03-agent-centric-interaction-design.md`
**事件体系权威文件:** `docs/events-v2.md`（§0 信封 / §5 L 档 / §6 三条不变式）

## Global Constraints

以下约束适用于**每一个** Task，不再逐条重复：

1. **Python** `>=3.11`；**ruff** `line-length = 100`，`select = ["E","W","F","I","B","UP","RUF"]`，`ignore = ["E501"]`。
2. **pytest** `asyncio_mode = "auto"`、`testpaths = ["tests"]`、`addopts = "-ra -q --strict-markers"`。无 Makefile / CI，命令直接跑：
   - 单文件：`python -m pytest tests/unit/test_x.py -q`
   - 全量：`python -m pytest -q`
   - lint：`python -m ruff check --output-format=concise <改过的文件> | grep -v 'RUF00[23]'`
3. **基线不是全绿。** 2026-09-04 在 `feat/multimodal` 上实测 **7 个先行失败**（Task 0 复核）。判据不是「全绿」，而是「**失败集合逐个 id 不变，通过数只增不减**」。任何新出现的红当场停下回退，不允许「顺手修一下」。
4. **ruff 既存告警约 24000 条，几乎全是 `RUF002`/`RUF003`**（中文注释里的全角标点）。判据是「除 RUF002/RUF003 外不新增」。特别盯 `F401`（删符号后残留的未用 import）。
5. **禁区：不要碰 golden 用例。** `tests/unit/test_capsule_golden.py`、`tests/unit/test_dispatch_fold_golden.py`、`tests/unit/test_golden_conformance.py`。若本计划的改动让它们变红，**记录下来交给用户**，不要自行修改这三个文件。
6. **只删发射，不删枚举**（`docs/events-v2.md` §5）。本计划停发 3 个事件类型，`EventType` 枚举值与 `reducers._apply` 的对应分支**一律保留**。
7. **不留兼容 shim**（spec §1）。改签名 / 改名 / 删方法一次改干净，`src` + `tests` 全量同步，不在原位置留转发。
8. **事件体系三条不变式**（`docs/events-v2.md` §6）每个碰事件的任务都受约束：`EventType` 全集 ≡ 实际发射集合 ∪ L 档；S/O/L 两两不交且并集为全集；所有发射出的事件 `origin` 非空。
9. **`TaskStatus` 是 `Literal` 不是枚举**——没有 `TaskStatus.FINISHED` 属性，一律与字符串字面量比较，或用 `core.models.status.TERMINAL_TASK_STATUSES`。
10. **提交粒度**：每个 Task 一次 commit，message 用中文，沿用仓内风格（`feat:` / `refactor:` / `fix(api):`）。尾部附：
    ```
    Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
    ```

---

## File Structure

**新建**

| 文件 | 职责 |
|---|---|
| `tests/unit/test_event_filter_agent.py` | `EventFilter.agent_id` 过滤（协议 + 内置 bus） |
| `tests/unit/test_hitl_agent_filter.py` | `list_pending(agent_id=)` 与 `HitlRequestView.delivery` |
| `tests/unit/test_alm_record_accessor.py` | `record_of()` 只读访问器 |
| `tests/unit/test_turn_handle.py` | `TurnHandle` 的身份字段、`events()`、`wait_for_finish()` |
| `tests/unit/test_run_id_per_turn.py` | 「一轮一个 run_id」与 `(run_id, sequence)` 唯一 |
| `tests/unit/test_recover_loads_agents.py` | `recover()` 装填 ALM + 恢复期 `AGENT_*` 广播 |
| `tests/unit/test_rebuild_agent.py` | `rebuild_agent` / `rebuild_all_agents` |
| `tests/unit/test_compact_agent.py` | `compact_agent` + `CompactReceipt` |

**修改**

| 文件 | 改什么 |
|---|---|
| `src/ctx_weft/protocols/events.py` | `EventFilter` 加 `agent_id`；3 个 `TASK_QUEUE_*` 登记进 `L_TIER_EVENT_TYPES` |
| `src/ctx_weft/protocols/hitl.py` | `HitlRequestView` 加 `delivery` |
| `src/ctx_weft/protocols/agent.py` | 无改动（`created_at` 字段已在，本计划填值） |
| `src/ctx_weft/providers/events/bus/in_process/bus.py` | `_matches` 支持 `agent_id` |
| `src/ctx_weft/core/hitl/registry.py` | `list_pending` 加 `agent_id`；`to_view` 带 `delivery` |
| `src/ctx_weft/core/hitl/status.py` | **删除**（唯一消费方 `session_status_after_recover` 一并删） |
| `src/ctx_weft/core/orchestrator/lifecycle/agent_manager.py` | `AgentRecordView` + `record_of()`；`_AgentRecord.created_at`；`load()` 后广播 `AGENT_*` |
| `src/ctx_weft/core/orchestrator/task/manager.py` | 删 `announce_queue_state` 与 `_final_status`（3 个 `TASK_QUEUE_*` 停发） |
| `src/ctx_weft/core/loop/steps/background_observe.py` | 快照另起 `run_id` + 补 `RunStarted`/`RunFinished` |
| `src/ctx_weft/core/loop/steps/recognize_intent.py` | 补 `RunStarted`/`RunFinished` |
| `src/ctx_weft/core/runtime.py` | 全部对外面改造（见各 Task） |
| `src/ctx_weft/__init__.py` | 导出面补全 |

**删除**：`src/ctx_weft/core/hitl/status.py`（Task 15）。

---

## Phase A · 协议与读模型地基

本阶段全部是**纯增量**：只加字段、加参数（带默认值）、加方法。不改任何现有调用方的行为，因此每个 Task 结束时全量测试的失败集合必须与基线**逐字相同**。

### Task 0: 记录基线 + 与批次二 Task 5 交接

**Files:**
- Modify: `docs/superpowers/plans/2026-09-03-outstanding-issues-batch2.md`

**Interfaces:**
- Consumes: 无（本计划起点）
- Produces: 一份写进本文件的基线失败清单，后续每个 Task 拿它做对照

- [ ] **Step 1: 跑全量测试记录基线**

Run: `python -m pytest -q 2>&1 | tail -20`

Expected（2026-09-04 实测，7 个先行失败）：

```
FAILED tests/integration/test_compact_flow_e2e.py::test_multiround_retry_accumulates_then_l3_collapses_e2e
FAILED tests/unit/test_bash_exec_liveness.py::test_bash_exec_streams_and_completes
FAILED tests/unit/test_bash_exec_liveness.py::test_bash_exec_idle_timeout_reports_error
FAILED tests/unit/test_observe_outcomes.py::test_default_role_prompt_uses_two_fields
FAILED tests/unit/test_script_runner.py::test_clean_exit_collects_output
FAILED tests/unit/test_script_runner.py::test_idle_timeout_kills_silent_sleeper
FAILED tests/unit/test_script_runner.py::test_hard_cap_kills_busy_but_overlong
```

把**实际输出的最后一行**（`N failed, M skipped, K passed in Xs`）抄进本 Task 的执行记录。后续每个 Task 的验收都对照这个 K：**K 只增不减**。

若实际失败集合与上面不同，以实际为准并在此记录——不要试图修它们，它们与本计划无关。

- [ ] **Step 2: 确认批次二 Task 5 无人在改**

Run:

```bash
git log --oneline -5 -- src/ctx_weft/core/loop/steps/background_observe.py src/ctx_weft/core/loop/steps/recognize_intent.py
git status --porcelain src/ctx_weft/core/loop/steps/
```

Expected: 工作区干净，且最近提交不含「批次二 Task 5」相关字样。

**如果发现有另一个进程正在改这两个文件**：停下来告诉用户，本计划的 Task 8 / Task 9 需要重新协调。不要并行改。

- [ ] **Step 3: 在批次二文档里标记 Task 5 已并入**

在 `docs/superpowers/plans/2026-09-03-outstanding-issues-batch2.md` 的 `## Task 5: sequence 重号与孤儿 run` 标题下方插入一行：

```markdown
> **已并入 `docs/superpowers/plans/2026-09-04-runtime-agent-centric-surface.md`（Task 7/8/9）** —— 2026-09-04。
> 「一轮一个 run_id」是那份计划的地基（删 `_default_run_id`），A4/C5 两项修复与它同源，分开做会两次改同一批文件。本 Task 不再单独执行。
```

- [ ] **Step 4: 提交**

```bash
git add docs/superpowers/plans/2026-09-03-outstanding-issues-batch2.md
git commit -m "docs(plan): 批次二 Task 5 并入 2026-09-04 runtime 对外面计划

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 1: `EventFilter.agent_id`

**Files:**
- Modify: `src/ctx_weft/protocols/events.py:56-62`
- Modify: `src/ctx_weft/providers/events/bus/in_process/bus.py:124-132`
- Test: `tests/unit/test_event_filter_agent.py`（新建）

**Interfaces:**
- Consumes: 无
- Produces: `EventFilter(agent_id: str | None = None)`；内置 `InProcessEventBus` 的 `_matches` 支持该维度。Task 5 的 `TurnHandle.events()` 依赖它。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_event_filter_agent.py
"""EventFilter 的 agent 维度（2026-09-04 spec §5.1）。

信封本来就带 agent_id，这里只是让订阅侧能按它过滤——host 要为单个 agent
渲染事件流，不加这一维就只能订阅整个 session 再自己丢弃。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from ctx_weft.protocols.events import Event, EventFilter
from ctx_weft.providers.events.bus.in_process.bus import InProcessEventBus, _matches


def _ev(**kw) -> Event:
    base = dict(
        id="evt_1", run_id="run_1", sequence=0, session_id="s1",
        type="TaskStarted", timestamp=datetime.now(UTC), origin="runtime",
    )
    base.update(kw)
    return Event(**base)


def test_filter_defaults_to_none():
    assert EventFilter().agent_id is None


def test_matches_when_agent_id_equal():
    assert _matches(_ev(agent_id="agt_1"), EventFilter(agent_id="agt_1"))


def test_rejects_when_agent_id_differs():
    assert not _matches(_ev(agent_id="agt_2"), EventFilter(agent_id="agt_1"))


def test_rejects_when_event_has_no_agent_id():
    """信封 agent_id 可空（SessionCreated 等就没有）。按 agent 过滤时它们不该混进来。"""
    assert not _matches(_ev(agent_id=None), EventFilter(agent_id="agt_1"))


def test_unset_filter_still_matches_everything():
    """回归：不传 agent_id 的既有订阅方行为一个字不变。"""
    assert _matches(_ev(agent_id="agt_1"), EventFilter())
    assert _matches(_ev(agent_id=None), EventFilter())


def test_agent_id_composes_with_other_dimensions():
    ev = _ev(agent_id="agt_1", task_id="tsk_1")
    assert _matches(ev, EventFilter(agent_id="agt_1", task_id="tsk_1"))
    assert not _matches(ev, EventFilter(agent_id="agt_1", task_id="tsk_2"))


@pytest.mark.asyncio
async def test_stream_filters_by_agent_id():
    """端到端：内置 bus 的 stream 只吐指定 agent 的事件。"""
    bus = InProcessEventBus()
    got: list[str] = []

    async def _consume():
        async for ev in bus.stream(EventFilter(agent_id="agt_1")):
            got.append(ev.id)
            if len(got) == 2:
                return

    task = asyncio.create_task(_consume())
    await asyncio.sleep(0)
    await bus.emit(_ev(id="e1", agent_id="agt_1"))
    await bus.emit(_ev(id="e2", agent_id="agt_2"))
    await bus.emit(_ev(id="e3", agent_id="agt_1"))
    await asyncio.wait_for(task, timeout=2.0)

    assert got == ["e1", "e3"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_event_filter_agent.py -q`
Expected: FAIL — `TypeError: EventFilter.__init__() got an unexpected keyword argument 'agent_id'`

- [ ] **Step 3: 实现**

`src/ctx_weft/protocols/events.py`，`EventFilter` 加字段（放在 `task_id` 之后、`types` 之前，与 `Event` 信封的字段顺序一致）：

```python
@dataclass
class EventFilter:
    """订阅过滤器。"""

    session_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    # agent 维度：信封的 agent_id 是 agent-centric 下 host 最常用的订阅轴
    # （只渲染某一个 agent 的事件流）。事件 agent_id 为 None 时不匹配任何
    # 具体 agent_id——「没有归属」不等于「属于你要的那个」。
    agent_id: str | None = None
    types: list[str] | None = None  # None=全部
```

`src/ctx_weft/providers/events/bus/in_process/bus.py` 的 `_matches` 加一行（放在 `task_id` 判断之后）：

```python
def _matches(event: Event, filter: EventFilter) -> bool:
    if filter.session_id and event.session_id != filter.session_id:
        return False
    if filter.run_id and event.run_id != filter.run_id:
        return False
    if filter.task_id and event.task_id != filter.task_id:
        return False
    if filter.agent_id and event.agent_id != filter.agent_id:
        return False
    if filter.types is not None and event.type not in filter.types:
        return False
    return True
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_event_filter_agent.py -q`
Expected: PASS（7 passed）

- [ ] **Step 5: 全量回归 + lint**

Run: `python -m pytest -q 2>&1 | tail -12`
Expected: 失败集合与 Task 0 基线逐字相同，通过数 ≥ 基线。

Run: `python -m ruff check --output-format=concise src/ctx_weft/protocols/events.py src/ctx_weft/providers/events/bus/in_process/bus.py tests/unit/test_event_filter_agent.py | grep -v 'RUF00[23]'`
Expected: 空。

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/protocols/events.py src/ctx_weft/providers/events/bus/in_process/bus.py tests/unit/test_event_filter_agent.py
git commit -m "feat(events): EventFilter 加 agent_id 维度

host 要按 agent 渲染事件流，信封已有该字段，订阅侧补上过滤。
内置 InProcessEventBus 同步支持；host 自实现的 bus 需跟进（破坏性变更清单）。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: HITL 的 agent 维度 —— `delivery` 暴露 + `agent_id` 过滤

**Files:**
- Modify: `src/ctx_weft/protocols/hitl.py`（`HitlRequestView` 加 `delivery`）
- Modify: `src/ctx_weft/core/hitl/registry.py:106-114`（`to_view`）、`:304-308`（`list_pending`）
- Modify: `src/ctx_weft/core/runtime.py:1965`（`list_pending_hitl`）
- Test: `tests/unit/test_hitl_agent_filter.py`（新建）

**Interfaces:**
- Consumes: 无
- Produces:
  - `HitlRequestView.delivery: Delivery`（默认 `NoResumeDelivery()`）
  - `HitlRegistry.list_pending(session_id: str | None = None, *, agent_id: str | None = None) -> list[PendingHitl]`
  - `CtxWeftRuntime.list_pending_hitl(*, session_id: str | None = None, agent_id: str | None = None) -> list[HitlRequestView]`

  Task 15 删 `session_status_after_recover` 时依赖 `delivery` 已经暴露。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_hitl_agent_filter.py
"""HITL 读模型的 agent 维度（2026-09-04 spec §5.2）。

HITL 自 09-03 起已经彻底 agent 化（HitlReply.agent_id 必填、cancel_agent 按
agent 过滤未决项），只有查询接口还停在 session 维度。这里补上，并把
delivery 这个原始事实暴露出去——它此前只活在 core 内部，host 想知道
「等的是面板还是一句话」只能靠一个 session 级派生串。
"""

from __future__ import annotations

from ctx_weft.core.hitl.registry import HitlRegistry
from ctx_weft.protocols.hitl import (
    AskUser,
    NoResumeDelivery,
    ToolResultDelivery,
    UserTurnDelivery,
)


def _open(reg, hitl_id, *, session_id, agent_id, delivery):
    """照 HitlRegistry.open 的既有签名登记一条未决项。

    实施时先读 registry.open 的真实签名（core/hitl/registry.py:128），
    按它构造 AskUser；下面的关键字只保证 id/session/agent/delivery 四项到位。
    """
    return reg.open(
        AskUser(prompt="?", delivery=delivery),
        hitl_id=hitl_id, session_id=session_id, task_id="tsk_1",
        agent_id=agent_id, tenant_id="default",
    )


def test_list_pending_filters_by_agent():
    reg = HitlRegistry()
    _open(reg, "h1", session_id="s1", agent_id="agt_1", delivery=UserTurnDelivery())
    _open(reg, "h2", session_id="s1", agent_id="agt_2", delivery=UserTurnDelivery())

    ids = [r.id for r in reg.list_pending(agent_id="agt_1")]
    assert ids == ["h1"]


def test_agent_and_session_filters_compose():
    reg = HitlRegistry()
    _open(reg, "h1", session_id="s1", agent_id="agt_1", delivery=UserTurnDelivery())
    _open(reg, "h2", session_id="s2", agent_id="agt_1", delivery=UserTurnDelivery())

    assert [r.id for r in reg.list_pending("s2", agent_id="agt_1")] == ["h2"]
    assert reg.list_pending("s1", agent_id="agt_2") == []


def test_no_filter_returns_all():
    """回归：既有调用方（不传过滤）行为不变。"""
    reg = HitlRegistry()
    _open(reg, "h1", session_id="s1", agent_id="agt_1", delivery=UserTurnDelivery())
    _open(reg, "h2", session_id="s2", agent_id="agt_2", delivery=ToolResultDelivery())
    assert len(reg.list_pending()) == 2


def test_view_carries_delivery():
    """delivery 是原始事实，host 据此自己判「面板还是一句话」。"""
    reg = HitlRegistry()
    _open(reg, "h1", session_id="s1", agent_id="agt_1", delivery=UserTurnDelivery())
    view = reg.list_pending()[0].to_view()
    assert isinstance(view.delivery, UserTurnDelivery)


def test_view_delivery_defaults_to_no_resume():
    """装填期占位项没有 delivery——默认值必须是安全的那个，不是 None。"""
    from ctx_weft.protocols.hitl import HitlRequestView
    from datetime import UTC, datetime

    v = HitlRequestView(
        id="h1", form="wait", session_id="s1", task_id="t1",
        created_at=datetime.now(UTC),
    )
    assert isinstance(v.delivery, NoResumeDelivery)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_hitl_agent_filter.py -q`
Expected: FAIL — `TypeError: list_pending() got an unexpected keyword argument 'agent_id'`

- [ ] **Step 3: 实现**

`src/ctx_weft/protocols/hitl.py`，`HitlRequestView` 加字段（放在 `resolved_at` 之前，与其余可选字段同区）：

```python
    outcome: HitlOutcome = ""
    #: 这条未决项要怎么把人的答复送回去——`UserTurnDelivery` = 等一句话，
    #: `ToolResultDelivery` / `NoResumeDelivery` = 有面板要拍板。host 据此
    #: 自行渲染，core 不再提供派生的会话级状态串（2026-09-04 spec §6.5）。
    delivery: Delivery = field(default_factory=NoResumeDelivery)
    resolved_at: datetime | None = None
```

`src/ctx_weft/core/hitl/registry.py` 的 `to_view` 补一行：

```python
    def to_view(self) -> HitlRequestView:
        return HitlRequestView(
            id=self.id, form=self.form, session_id=self.session_id, task_id=self.task_id,
            created_at=self.created_at, agent_id=self.agent_id, subject_id=self.subject_id,
            prompt=self.prompt, detail=self.detail, fields=list(self.fields),
            proposal=self.proposal,
            outcome=self.decision.outcome if self.decision else "",
            delivery=self.delivery,
            resolved_at=self.resolved_at,
        )
```

`list_pending` 加关键字参数（`session_id` 保持位置参数，91 处既有调用方不受影响）：

```python
    def list_pending(
        self, session_id: str | None = None, *, agent_id: str | None = None,
    ) -> list[PendingHitl]:
        return [
            r for r in self._requests.values()
            if not r.resolved
            and (session_id is None or r.session_id == session_id)
            and (agent_id is None or r.agent_id == agent_id)
        ]
```

`src/ctx_weft/core/runtime.py` 的 `list_pending_hitl` 改成全关键字：

```python
    def list_pending_hitl(
        self, *, session_id: str | None = None, agent_id: str | None = None,
    ) -> "list[HitlRequestView]":
        """未决 HITL 的**只读视图**列表。两个过滤都可选、可叠加，都不传 = 全部。

        host 面向 HITL 的读入口。刻意不暴露 `HitlRegistry`：`PendingHitl` 是 core 的活
        记录（带等待槽、stage、invocation_key 这些内部键），它自己的 docstring 就写着
        「不出 core」。经由本方法拿到的 `HitlRequestView` 才是契约层类型。

        `agent_id` 是 agent-centric 下的主用过滤轴（2026-09-04 spec §5.2）：HITL 自
        09-03 起已彻底 agent 化，只有这个查询入口此前停在 session 维度。

        **只读内存**：注意重启之后 registry 要先被装填（`recover()` / `rebuild_hitl()`）
        才有内容——「恢复是喂进来、不是查回去」（spec §3.1）。
        """
        return [
            r.to_view()
            for r in self.hitl_registry.list_pending(session_id=session_id, agent_id=agent_id)
        ]
```

**改签名的连带**：`list_pending_hitl(session_id)` 的位置参数调用方全部要改成关键字。

Run 先定位：`grep -rn "list_pending_hitl(" src/ tests/`
逐个改成 `list_pending_hitl(session_id=...)`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_hitl_agent_filter.py -q`
Expected: PASS（5 passed）

- [ ] **Step 5: 全量回归 + lint**

Run: `python -m pytest -q 2>&1 | tail -12`
Expected: 失败集合与基线逐字相同。

Run: `python -m ruff check --output-format=concise src/ctx_weft/protocols/hitl.py src/ctx_weft/core/hitl/registry.py src/ctx_weft/core/runtime.py tests/unit/test_hitl_agent_filter.py | grep -v 'RUF00[23]'`
Expected: 空。

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "feat(hitl): 查询接口补 agent 维度，HitlRequestView 暴露 delivery

list_pending / list_pending_hitl 加 agent_id 过滤；delivery 从 core 内部
事实提升为契约字段——host 据此自判「等面板还是等一句话」，为删掉
session_status_after_recover 这个派生入口铺路。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: ALM 只读访问器 `record_of()`

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/lifecycle/agent_manager.py`
- Modify: `src/ctx_weft/core/runtime.py`（五处 `_agents` 穿透）
- Test: `tests/unit/test_alm_record_accessor.py`（新建）

**Interfaces:**
- Consumes: 无
- Produces: `AgentRecordView`（frozen dataclass）与 `AgentLifecycleManager.record_of(agent_id) -> AgentRecordView | None`。Task 4 / 11 / 16 / 17 都用它取 `session_id`。

**为什么不复用 `AgentDetail`**：`AgentDetail`（`protocols/agent.py:23`）是 host-facing 视图，带 `current_task_status`（要查 TaskManager）、不带 `tenant_id`。`AgentRecordView` 是 core 内部对 `_AgentRecord` 的只读投影，两者字段不同，合并会让 core 内部取个 `tenant_id` 都要先查一次 TM。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_alm_record_accessor.py
"""ALM 的只读记录访问器（2026-09-04 spec §5.3）。

runtime 此前有五处直接读 `AgentLifecycleManager._agents`。私有穿透让
「ALM 是 agent 身份唯一住所」这条边界只存在于文档里。
"""

from __future__ import annotations

from ctx_weft.core.orchestrator.lifecycle.agent_manager import (
    AgentRecordView,
    _AgentRecord,
)
from ctx_weft.protocols import LoopConfig, MemoryConfig
from tests.integration.test_minimal_loop import (
    InlineAgentTemplateProvider,
    make_runtime,
)


def _alm():
    return make_runtime(agent_provider=InlineAgentTemplateProvider())._agent_lifecycle_manager


def _plant(alm, agent_id, *, session_id="s1", parent=None, status="idle"):
    alm._agents[agent_id] = _AgentRecord(
        session_id=session_id, tenant_id="default", template_id="tpl",
        parent_agent_id=parent, spawn_depth=0 if parent is None else 1,
        memory_config=MemoryConfig(), loop_config=LoopConfig(), status=status,
    )


def test_record_of_returns_view():
    alm = _alm()
    _plant(alm, "agt_1")
    rec = alm.record_of("agt_1")
    assert isinstance(rec, AgentRecordView)
    assert rec.agent_id == "agt_1"
    assert rec.session_id == "s1"
    assert rec.tenant_id == "default"
    assert rec.template_id == "tpl"
    assert rec.status == "idle"


def test_record_of_unknown_returns_none():
    """查无此 agent 不是编程错误——调用方（如 send_message 的守卫）自己决定怎么报。"""
    assert _alm().record_of("nope") is None


def test_view_is_frozen():
    alm = _alm()
    _plant(alm, "agt_1")
    rec = alm.record_of("agt_1")
    import dataclasses
    import pytest
    with pytest.raises(dataclasses.FrozenInstanceError):
        rec.status = "running"


def test_view_is_a_snapshot_not_a_live_reference():
    """改 registry 不该反映到已经取出的视图上——否则调用方会拿到会变的『只读』对象。"""
    alm = _alm()
    _plant(alm, "agt_1", status="idle")
    snap = alm.record_of("agt_1")
    alm._agents["agt_1"].status = "running"
    assert snap.status == "idle"
    assert alm.record_of("agt_1").status == "running"


def test_runtime_no_longer_reads_private_agents_dict():
    """结构性守卫：runtime.py 里不该再出现 `_agents[` / `_agents.get(`。"""
    from pathlib import Path
    src = Path("src/ctx_weft/core/runtime.py").read_text(encoding="utf-8")
    assert "_agents[" not in src
    assert "_agents.get(" not in src
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_alm_record_accessor.py -q`
Expected: FAIL — `ImportError: cannot import name 'AgentRecordView'`

- [ ] **Step 3: 实现**

`agent_manager.py`，在 `_AgentRecord` 之后加只读投影与访问器：

```python
@dataclass(frozen=True)
class AgentRecordView:
    """`_AgentRecord` 的只读快照——ALM 对外（core 内部）唯一的记录读法。

    是**快照**不是引用：取出之后 registry 再变也不影响手上这份，调用方不会拿到
    一个会自己变的「只读」对象。要最新值就再调一次 `record_of`。
    """

    agent_id: str
    session_id: str
    tenant_id: str
    template_id: str
    parent_agent_id: str | None
    spawn_depth: int
    status: str
    current_task_id: str | None
```

在 `status_of` 附近（读区）加：

```python
    def record_of(self, agent_id: str) -> AgentRecordView | None:
        """该 agent 的只读记录快照；未登记返回 None。

        取代 runtime 对 `_agents` 的私有穿透（2026-09-04 spec §5.3）。返回 None
        而不是抛错：调用方各有各的报错口径（`send_message` 抛 `AgentNotFound`、
        `_task_is_terminal` 静默降级），由它们自己决定。
        """
        rec = self._agents.get(agent_id)
        if rec is None:
            return None
        return AgentRecordView(
            agent_id=agent_id,
            session_id=rec.session_id,
            tenant_id=rec.tenant_id,
            template_id=rec.template_id,
            parent_agent_id=rec.parent_agent_id,
            spawn_depth=rec.spawn_depth,
            status=rec.status,
            current_task_id=rec.current_task_id,
        )
```

`runtime.py` 五处穿透逐个换掉：

1. `list_agents`（`:1977` 附近）：`reg._agents[i].parent_agent_id` → 先 `rec = reg.record_of(i)`，跳过 `None`。
2. `get_agent`（`:2019`）：`reg._agents.get(agent_id)` → `reg.record_of(agent_id)`。
3. `send_message`（`:2048`）：`reg._agents[agent_id]` → `reg.record_of(agent_id)`（`assert_can_receive` 已保证存在，仍要判 `None` 以满足类型检查，判到 `None` 时抛 `AgentNotFound`）。
4. `pause_agent`（`:877` 与循环内 `:887`）：两处 `reg._agents.get(...)` → `reg.record_of(...)`。
5. `_start_task_for_agent`（`:2206`）：`reg._agents[agent_id]` → `reg.record_of(agent_id)`。

**注意 `_start_task_for_agent` 末尾有一处写**：`rec.current_task_id = task.id`。视图是冻结的，写不进去。这一行改成调 ALM 的写口：在 `agent_manager.py` 加

```python
    def set_current_task(self, agent_id: str, task_id: str | None) -> None:
        """外部消息新建 task 时同步 `current_task_id`——`send_message` 的路由依据。

        运行期这个字段由 `AGENT_*` 事件的折叠维护；`_start_task_for_agent` 是唯一
        「task 还没派发、但路由已经必须认它」的时刻，故留这一个显式写口。
        """
        rec = self._agents.get(agent_id)
        if rec is not None:
            rec.current_task_id = task_id
```

`_start_task_for_agent` 里改成 `reg.set_current_task(agent_id, task.id)`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_alm_record_accessor.py tests/unit/test_runtime_agent_api.py tests/unit/test_agent_cascade.py -q`
Expected: PASS

- [ ] **Step 5: 全量回归 + lint**

Run: `python -m pytest -q 2>&1 | tail -12`
Expected: 失败集合与基线逐字相同。

Run: `python -m ruff check --output-format=concise src/ctx_weft/core/orchestrator/lifecycle/agent_manager.py src/ctx_weft/core/runtime.py tests/unit/test_alm_record_accessor.py | grep -v 'RUF00[23]'`
Expected: 空。

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "refactor(alm): 加 record_of() 只读访问器，消掉 runtime 五处 _agents 穿透

AgentRecordView 是快照不是引用；current_task_id 的唯一外部写口收成
set_current_task()。「ALM 是 agent 身份唯一住所」从文档变成结构。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `created_at` 落地 + `list_agents` 签名放宽

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/lifecycle/agent_manager.py`（`_AgentRecord` / `AgentRecordView` / `instantiate` / `load`）
- Modify: `src/ctx_weft/core/runtime.py`（`list_agents` / `get_agent`）
- Test: `tests/unit/test_runtime_agent_api.py`（追加）

**Interfaces:**
- Consumes: Task 3 的 `AgentRecordView`
- Produces: `AgentRecordView.created_at: datetime | None`；`list_agents(*, session_id=None, parent_agent_id=None, include_terminated=False)`

- [ ] **Step 1: 写失败测试**

追加到 `tests/unit/test_runtime_agent_api.py` 末尾：

```python
# ── 2026-09-04 spec §5.4 / §8：created_at 与 list_agents 签名 ──────────────


async def test_summary_carries_created_at_for_live_agents():
    """字段 2026-09-03 就声明了，一直是 None——运行期实例化的 agent 必须有值。"""
    rt = _rt()
    handle = await rt.start_session(SessionStartParams.create(
        template_id="echo", user_prompt="hi", context_limit=8000,
    ))
    [summary] = [a for a in rt.list_agents(session_id=handle.session_id)
                 if a.agent_id == handle.agent_id]
    assert summary.created_at is not None


async def test_list_agents_without_session_id_spans_sessions():
    rt = _rt()
    _plant(rt, "agt_a", None, session_id="s1")
    _plant(rt, "agt_b", None, session_id="s2")
    ids = {a.agent_id for a in rt.list_agents()}
    assert {"agt_a", "agt_b"} <= ids


async def test_list_agents_session_id_still_filters():
    rt = _rt()
    _plant(rt, "agt_a", None, session_id="s1")
    _plant(rt, "agt_b", None, session_id="s2")
    assert [a.agent_id for a in rt.list_agents(session_id="s1")] == ["agt_a"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_runtime_agent_api.py -q -k "created_at or spans_sessions"`
Expected: FAIL — `created_at is None`；`list_agents()` 缺必填位置参数 `session_id`。

- [ ] **Step 3: 实现**

`_AgentRecord` 加字段（放在末尾，全部有默认值）：

```python
    #: 实例化时刻。恢复路径（`load`）留 None——`AgentView` 不带该字段，
    #: 硬造一个「恢复时刻」当创建时刻是在撒谎。host 侧要精确值可读
    #: AgentInstantiated 事件的 timestamp。
    created_at: datetime | None = None
```

`AgentRecordView` 同步加 `created_at: datetime | None`，`record_of` 里透传。

`instantiate` 落 record 处加 `created_at=now_utc()`（`now_utc` 从 `ctx_weft.core.utils.clock` 导入，仓内既有用法）。`load` 不传，保持 `None`。

`runtime.list_agents` 改签名并填字段：

```python
    def list_agents(
        self,
        *,
        session_id: str | None = None,
        parent_agent_id: str | None = None,
        include_terminated: bool = False,
    ) -> "list[AgentSummary]":
        """列出 agent（spec 5；2026-09-04 spec §8 放宽 session_id）——host 的发现入口。

        三个过滤都可选、可叠加。`session_id` 不传 = 跨 session 列出全部登记 agent
        （`agent_id` 全局唯一，按 session 分片只是历史惯性）；传了则只列该 session。
        `parent_agent_id` 只返回其**直接**子 agent（不展开子孙——层级关系不在接口层
        嵌套，调用方按 `parent_agent_id` 自行还原成树）。`include_terminated` 默认
        False，避免列表随时间无限膨胀。

        数据源用 `AgentLifecycleManager` 自己的记录（经 `record_of`），不用
        `SessionRegistry.agent_ids_of`（成员登记表）：后者只增不减、与 session 同寿命，
        而 ALM 的 `release_session` 会真正摘除记录。以 ALM 的内存现实为准，不会把
        已经不存在于内存里的 agent 报告出去。
        """
        reg = self._agent_lifecycle_manager
        ids = (reg.agent_ids_of_session(session_id) if session_id is not None
               else reg.all_agent_ids())
        out: list[AgentSummary] = []
        for aid in ids:
            rec = reg.record_of(aid)
            if rec is None:
                continue
            if parent_agent_id is not None and rec.parent_agent_id != parent_agent_id:
                continue
            if not include_terminated and rec.status == "terminated":
                continue
            out.append(AgentSummary(
                agent_id=aid,
                parent_agent_id=rec.parent_agent_id,
                status=rec.status,
                current_task_id=rec.current_task_id,
                spawn_depth=rec.spawn_depth,
                created_at=rec.created_at,
            ))
        return out
```

ALM 加 `all_agent_ids()`：

```python
    def all_agent_ids(self) -> list[str]:
        """全部已登记 agent 的 id——`list_agents()` 不传 session_id 时的数据源。"""
        return list(self._agents)
```

`get_agent` 同样补 `created_at=rec.created_at`。

**改签名的连带**：`list_agents(session_id)` 的位置参数调用方全部改关键字。
Run 先定位：`grep -rn "list_agents(" src/ tests/`

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_runtime_agent_api.py -q`
Expected: PASS

- [ ] **Step 5: 全量回归 + lint**

Run: `python -m pytest -q 2>&1 | tail -12`
Expected: 失败集合与基线逐字相同。

Run: `python -m ruff check --output-format=concise src/ctx_weft/core/orchestrator/lifecycle/agent_manager.py src/ctx_weft/core/runtime.py tests/unit/test_runtime_agent_api.py | grep -v 'RUF00[23]'`
Expected: 空。

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "feat(api): agent 视图补 created_at，list_agents 的 session_id 降为可选过滤

created_at 字段 2026-09-03 就声明了但从不填值；恢复路径留 None 而不是
硬造一个恢复时刻。list_agents 不传 session_id 则跨 session 列全部——
agent_id 全局唯一，按 session 分片是历史惯性。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Phase B · run_id 与句柄

本阶段有破坏性变更。顺序不可颠倒：先把句柄从 run_id 上摘下来（Task 5），才能删掉 `_default_run_id`（Task 6）而不留一个没人用的 run_id。

### Task 5: `RunHandle` → `TurnHandle`

**Files:**
- Modify: `src/ctx_weft/core/runtime.py:277-320`（类定义）、`:3097-3112`（`_execute_task` 构造）、`:1194`（`start_session` 构造）
- Modify: `src/ctx_weft/__init__.py`
- Test: `tests/unit/test_turn_handle.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `EventFilter.agent_id`
- Produces:
  ```python
  @dataclass
  class TurnHandle:
      session_id: str
      agent_id: str
      task_id: str
      template_id: str
      event_bus: EventBus
      _state: LoopState | None = None

      async def events(self) -> AsyncIterator[Event]: ...
      async def wait_for_finish(self, timeout: float = 300.0) -> LoopState | None: ...
  ```
  Task 6 依赖它已不含 run_id；Task 10 依赖它是 `send_message` 的返回类型。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_turn_handle.py
"""TurnHandle：句柄以 agent + task 为轴（2026-09-04 spec §3）。

run 是引擎内部一轮循环的相关性 id，句柄的职责是「指着一个外部可寻址的对象」。
agent + task 已经够定位，events() 与 wait_for_finish() 都不需要 run_id——
把它放进句柄只会多一个无法诚实填写的字段。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from ctx_weft.core.runtime import SessionStartParams, TurnHandle
from ctx_weft.protocols.events import Event
from ctx_weft.providers.events.bus.in_process.bus import InProcessEventBus
from tests.integration.test_minimal_loop import (
    InlineAgentTemplateProvider,
    make_runtime,
)

pytestmark = pytest.mark.asyncio


def _ev(**kw) -> Event:
    base = dict(
        id="evt_1", run_id="run_1", sequence=0, session_id="s1",
        type="TaskStarted", timestamp=datetime.now(UTC), origin="runtime",
        agent_id="agt_1", task_id="tsk_1",
    )
    base.update(kw)
    return Event(**base)


async def test_handle_has_no_run_id_field():
    """结构性守卫：run_id 不在对外句柄上。"""
    assert "run_id" not in TurnHandle.__dataclass_fields__


async def test_events_filters_by_agent_and_task():
    bus = InProcessEventBus()
    h = TurnHandle(session_id="s1", agent_id="agt_1", task_id="tsk_1",
                   template_id="tpl", event_bus=bus)
    got: list[str] = []

    async def _consume():
        async for ev in h.events():
            got.append(ev.id)
            if len(got) == 2:
                return

    t = asyncio.create_task(_consume())
    await asyncio.sleep(0)
    await bus.emit(_ev(id="e1"))
    await bus.emit(_ev(id="e2", agent_id="agt_2"))          # 别的 agent
    await bus.emit(_ev(id="e3", task_id="tsk_2"))           # 同 agent 别的 task
    await bus.emit(_ev(id="e4"))
    await asyncio.wait_for(t, timeout=2.0)

    assert got == ["e1", "e4"]


async def test_wait_for_finish_returns_on_task_terminal_event():
    bus = InProcessEventBus()
    h = TurnHandle(session_id="s1", agent_id="agt_1", task_id="tsk_1",
                   template_id="tpl", event_bus=bus, _state=None)
    waiter = asyncio.create_task(h.wait_for_finish(timeout=2.0))
    await asyncio.sleep(0)
    await bus.emit(_ev(id="e1", type="TaskFinished"))
    await asyncio.wait_for(waiter, timeout=2.0)


async def test_wait_for_finish_ignores_run_finished():
    """RunFinished 不再是判据——一轮 run 结束不等于这条 task 结束（可能还要 finalize）。"""
    bus = InProcessEventBus()
    h = TurnHandle(session_id="s1", agent_id="agt_1", task_id="tsk_1",
                   template_id="tpl", event_bus=bus)
    waiter = asyncio.create_task(h.wait_for_finish(timeout=0.3))
    await asyncio.sleep(0)
    await bus.emit(_ev(id="e1", type="RunFinished"))
    await asyncio.wait_for(waiter, timeout=2.0)   # 靠超时返回，不是靠 RunFinished


async def test_start_session_returns_turn_handle():
    rt = make_runtime(agent_provider=InlineAgentTemplateProvider())
    h = await rt.start_session(SessionStartParams.create(
        template_id="echo", user_prompt="hi", context_limit=8000,
    ))
    assert isinstance(h, TurnHandle)
    assert h.agent_id and h.task_id and h.session_id and h.template_id
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_turn_handle.py -q`
Expected: FAIL — `ImportError: cannot import name 'TurnHandle'`

- [ ] **Step 3: 实现**

`runtime.py` 把 `RunHandle` 整个换掉（**改名 + 删字段 + 换判据**，不留别名）：

```python
# ── TurnHandle ────────────────────────────────────────────────────────────────


@dataclass
class TurnHandle:
    """一次外部交互的句柄：**agent + task 两个轴**（2026-09-04 spec §3）。

    四个身份字段恒非空：

    - ``agent_id`` —— 被寻址的 agent。`start_session` 给的是该 session 的 **root
      agent**（`session.root_agent_id`，在 `SESSION_CREATED` 之前铸好，见
      `SessionRegistry.create_session` / `resume_session`）；`send_message` 给的是
      调用方点名的那个；`run_single_task` 给的是执行那一条 task 的 agent。
      直接传给 `send_message(agent_id, ...)` 或 `get_agent(agent_id)`。
    - ``task_id`` —— 这次交互落到的 task。

    **不含 ``run_id``。** run 是引擎内部一轮循环的相关性 id，句柄不需要它：
    `events()` 按 agent + task 订阅，`wait_for_finish()` 等 task 终态。host 若要按轮
    聚合，每条事件的信封里都带 `run_id`，直接读。放进句柄反而会多一个无法诚实填写的
    字段——`send_message` 的「注入且不重排」分支在返回那一刻确实还没有新一轮。
    """

    session_id: str
    agent_id: str
    task_id: str
    template_id: str
    event_bus: EventBus
    _state: LoopState | None = None

    async def events(self) -> AsyncIterator[Event]:
        from ctx_weft.protocols.events import EventFilter
        async for ev in self.event_bus.stream(
            EventFilter(agent_id=self.agent_id, task_id=self.task_id)
        ):
            yield ev

    async def wait_for_finish(self, timeout: float = 300.0) -> LoopState | None:
        """阻塞到该 task 进终态或超时。

        判据是 **task 终态事件**，不是 `RunFinished`：一轮 run 结束不等于这条 task
        结束（还可能有 finalize、还可能被重排再跑一轮）。四个终态事件与
        `TERMINAL_TASK_STATUSES` 同源，外加 `TaskFinalized`——它是 finalize 阶段的
        收尾信号，落在 `TaskFinished` 之后，等它才不会返回过早。
        """
        from ctx_weft.protocols.events import EventFilter
        terminal = {
            EventType.TASK_FINISHED, EventType.TASK_FAILED,
            EventType.TASK_CANCELED, EventType.TASK_FINALIZED,
        }
        try:
            async with asyncio.timeout(timeout):
                async for ev in self.event_bus.stream(
                    EventFilter(agent_id=self.agent_id, task_id=self.task_id)
                ):
                    if ev.type in terminal:
                        return self._state
        except TimeoutError:
            pass
        return self._state
```

`_execute_task` 的构造改成：

```python
        handle = TurnHandle(
            session_id=session.id,
            agent_id=agent.id,
            task_id=task.id,
            template_id=template.id,
            event_bus=self._event_bus,
            _state=state,
        )
```

`start_session` 的构造改成（`run_id` 这一行**暂时保留**，Task 6 才删——它此刻仍被
`default_run_id=run_id` 用着）：

```python
        handle = TurnHandle(
            session_id=session.id,
            agent_id=session.root_agent_id or "",
            task_id=root_task.id,
            template_id=params.template_id,
            event_bus=self._event_bus,
        )
```

全仓改名：`grep -rln "RunHandle" src/ tests/` 逐个把 `RunHandle` 换成 `TurnHandle`，
并删掉所有 `handle.run_id` 的读法（改读事件信封或直接删掉那行断言）。
`src/ctx_weft/__init__.py` 的导入与 `__all__` 同步改。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_turn_handle.py -q`
Expected: PASS（5 passed）

- [ ] **Step 5: 全量回归 + lint**

Run: `python -m pytest -q 2>&1 | tail -12`
Expected: 失败集合与基线逐字相同。

Run: `python -m ruff check --output-format=concise src/ctx_weft/core/runtime.py src/ctx_weft/__init__.py tests/unit/test_turn_handle.py | grep -v 'RUF00[23]'`
Expected: 空。

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "refactor(api)!: RunHandle 换轴成 TurnHandle（agent + task），去掉 run_id

events() 按 agent+task 订阅，wait_for_finish() 等 task 终态而非 RunFinished。
run 是引擎内部一轮循环的相关性 id，host 要按轮聚合从事件信封里读。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: 删除 `_default_run_id` —— 一轮一个 run_id

**Files:**
- Modify: `src/ctx_weft/core/runtime.py:1187`（`start_session` 预铸）、`:1215`、`:1422-1448`（`_make_task_runner`）、`:1608`（`recover_session`）、`:3123-3160`（`_SessionTaskRunner.__init__`）、`:3237`
- Test: `tests/unit/test_run_id_per_turn.py`（新建）

**Interfaces:**
- Consumes: Task 5 的 `TurnHandle`（已不含 run_id，`start_session` 因此不再需要预铸）
- Produces: 两条不变式——`(run_id, sequence)` 全局唯一；每个 run_id 恰有一对 `RunStarted`/`RunFinished`。Task 7/8 复用同一组断言 helper。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_run_id_per_turn.py
"""一轮一个 run_id（2026-09-04 spec §4）。

改动前：start_session 预铸一个 run_id 当 `_SessionTaskRunner._default_run_id`，
`assemble` 的非 subagent 分支把它给每一条任务用；而每次 `_execute_task` 都新建
LoopState、sequence_counter 从 0 起。结果同一个 run_id 下跨轮撞号、出现多对
RunStarted/RunFinished。
"""

from __future__ import annotations

import collections

import pytest

from ctx_weft.core.runtime import SessionStartParams
from ctx_weft.protocols.events import EventType
from tests.integration.test_minimal_loop import (
    InlineAgentTemplateProvider,
    make_runtime,
)

pytestmark = pytest.mark.asyncio


def assert_no_duplicate_sequence(events):
    """(run_id, sequence) 唯一。run 外的事件恒 sequence=0，不参与。"""
    seen = collections.defaultdict(set)
    dupes = []
    for e in events:
        if e.run_id is None:
            continue
        if e.sequence in seen[e.run_id]:
            dupes.append(f"{e.run_id}:{e.sequence}:{e.type}")
        seen[e.run_id].add(e.sequence)
    assert dupes == [], f"(run_id, sequence) 撞号: {dupes}"


def assert_every_run_has_one_start_and_finish(events):
    """每个 run_id 恰有一对 RunStarted / RunFinished。"""
    starts = collections.Counter(
        e.run_id for e in events if e.type == EventType.RUN_STARTED)
    finishes = collections.Counter(
        e.run_id for e in events if e.type == EventType.RUN_FINISHED)
    seen = {e.run_id for e in events if e.run_id is not None}
    problems = []
    for rid in sorted(seen):
        if starts[rid] != 1 or finishes[rid] != 1:
            problems.append(f"{rid}: {starts[rid]} started / {finishes[rid]} finished")
    assert problems == [], f"run 起止不成对: {problems}"


async def _two_turn_session():
    """跑两轮对话，收集全部事件。"""
    rt = make_runtime(agent_provider=InlineAgentTemplateProvider())
    seen = []
    rt.event_bus.subscribe(None, lambda ev: seen.append(ev) or None)

    h = await rt.start_session(SessionStartParams.create(
        template_id="echo", user_prompt="hi", context_limit=8000,
    ))
    await h.wait_for_finish(timeout=20.0)
    h2 = await rt.send_message(h.agent_id, "second turn")
    await h2.wait_for_finish(timeout=20.0)
    return seen


async def test_two_turns_do_not_share_a_run_id():
    events = await _two_turn_session()
    run_ids = {e.run_id for e in events if e.run_id is not None}
    starts = [e for e in events if e.type == EventType.RUN_STARTED]
    assert len(starts) >= 2, "两轮至少两个 run"
    assert len(run_ids) >= 2, f"两轮共用了 run_id: {run_ids}"


async def test_sequence_is_unique_per_run():
    assert_no_duplicate_sequence(await _two_turn_session())


async def test_runs_are_paired():
    assert_every_run_has_one_start_and_finish(await _two_turn_session())


async def test_session_task_runner_has_no_default_run_id():
    """结构性守卫：这个字段不该再存在。"""
    from ctx_weft.core.runtime import _SessionTaskRunner
    import inspect
    assert "default_run_id" not in inspect.signature(_SessionTaskRunner.__init__).parameters
```

> `_two_turn_session` 里的 `send_message` 返回 `TurnHandle` 是 Task 10 才落地的。
> 本 Task 执行时它还返回 `str`——把那两行临时换成：
> ```python
> task_id = await rt.send_message(h.agent_id, "second turn")
> await asyncio.sleep(1.0)   # 让第二轮跑起来
> ```
> Task 10 完成后回来把它改回 `TurnHandle` 写法，并在那个 Task 的验收里重跑本文件。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_run_id_per_turn.py -q`
Expected: FAIL —— `test_two_turns_do_not_share_a_run_id` 报「两轮共用了 run_id」；
`test_session_task_runner_has_no_default_run_id` 报该参数仍在。

- [ ] **Step 3: 实现**

1. `_SessionTaskRunner.__init__` 删掉 `default_run_id: str` 参数与 `self._default_run_id` 字段。
2. `assemble` 的 `case _` 分支：`run_id=self._default_run_id` → `run_id=generate_id("run")`。
   （subagent 分支本来就是 `generate_id("run")`，不动。）
3. `_make_task_runner` 删掉 `default_run_id` 形参与透传。
4. `start_session`：删掉 `run_id = generate_id("run")` 这一行与 `default_run_id=run_id` 实参。
5. `recover_session`（`:1608`）：删掉 `default_run_id=generate_id("run")` 实参。
6. `_SessionTaskRunner` 类 docstring 里提到 `default_run_id` 的句子一并删。

**不动的**：`_execute_task(run_id=...)` 形参、`_relaunch_task_recap`（`:1758`）里
`generate_id("run")` 的直接调用、`compact_session`（`:1915`）自铸的那个——它们本来就是
一轮一个。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_run_id_per_turn.py -q`
Expected: `test_two_turns_do_not_share_a_run_id` / `test_session_task_runner_has_no_default_run_id` PASS。

`test_sequence_is_unique_per_run` 与 `test_runs_are_paired` **此时可能仍 FAIL** —— 那是
Task 7（background observe 快照共号）与 Task 8（recognize_intent 孤儿 run）的责任。
若失败，确认失败原因确实指向 `BackgroundObserve` / `RecognizeIntent` 相关的 run_id，
用 `@pytest.mark.xfail(reason="Task 7/8 未完成", strict=False)` 临时标记这两条，并在
Task 8 的 Step 4 里去掉标记。

- [ ] **Step 5: 全量回归 + lint**

Run: `python -m pytest -q 2>&1 | tail -12`
Expected: 失败集合与基线逐字相同。

**重点复核**：`grep -rn "default_run_id" src/ tests/` 必须无输出。

Run: `python -m ruff check --output-format=concise src/ctx_weft/core/runtime.py tests/unit/test_run_id_per_turn.py | grep -v 'RUF00[23]'`
Expected: 空。

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "fix(loop)!: 删掉 _default_run_id，一轮一个 run_id

改动前一个 owner-TM 下所有根 scope 轮次共用一个 run_id、各自从 0 编号，
(run_id, sequence) 跨轮撞号且一个 run_id 下多对 RunStarted/RunFinished。
run_id 现在只在 assemble 里铸，两个分支无例外。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: `background_observe` 快照另起 run_id 并补起止

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/background_observe.py`（`launch_background_observe` 的 `dataclasses.replace` 快照处、`:399` 附近）
- Test: `tests/unit/test_run_id_per_turn.py`（追加）

**Interfaces:**
- Consumes: Task 6 的两条不变式与断言 helper
- Produces: background observe 子 run 有独立 run_id 与配对的 `RunStarted`/`RunFinished`

**背景（原批次二 Task 5 的 A4）**：`launch_background_observe` 用
`dataclasses.replace(state)` 做快照——`run_id` 相同，但 `sequence_counter` 是一份独立的
`int` 副本（普通不可变字段）。此后两边各自 `+= 1`，后台 recap 那几类事件与主 run 重号。
连带 `RunFinished.total_events` 取主 run 的 counter，不含后台事件。

- [ ] **Step 1: 写失败测试**

追加到 `tests/unit/test_run_id_per_turn.py`：

```python
async def test_background_observe_does_not_reuse_main_run_id():
    """后台 recap 是独立一轮，不该借主 run 的号（原批次二 A4）。"""
    events = await _background_observe_session()
    bg = [e for e in events if (e.origin or "").endswith("background_observe")]
    assert bg, "这个夹具没触发 background observe——先修夹具再断言"
    main_runs = {e.run_id for e in events if (e.origin or "") == "loop.act"}
    bg_runs = {e.run_id for e in bg}
    assert not (main_runs & bg_runs), f"后台与主 run 共号: {main_runs & bg_runs}"


async def test_background_observe_run_is_paired():
    assert_every_run_has_one_start_and_finish(await _background_observe_session())
```

`_background_observe_session` 要自己写：**先读 `tests/unit/test_background_observe_wiring.py`**，
照它的搭台手法跑一个会触发 background observe 的 run，把 `rt.event_bus.subscribe(None, ...)`
收集到的全部事件返回。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_run_id_per_turn.py -q -k background`
Expected: FAIL — 后台与主 run 共号。

- [ ] **Step 3: 实现**

在 `launch_background_observe` 做快照的地方，`dataclasses.replace(state, ...)` 额外覆盖两个字段：

```python
    bg_run_id = generate_id("run")
    snapshot = dataclasses.replace(
        state,
        run_id=bg_run_id,        # A4：独立一轮，不借主 run 的号
        sequence_counter=0,      # 新 run 从 0 起，与主 run 的计数器彻底分家
        origin=EventOrigin.LOOP_BACKGROUND_OBSERVE,
    )
```

并在后台协程的起止处补配对事件（用与主 run 同一个 `make_event` 通路，`origin` 取
`EventOrigin.LOOP_BACKGROUND_OBSERVE`）：协程开跑前发 `RUN_STARTED`，`finally` 里发
`RUN_FINISHED`。`total_events` 取快照自己的 `sequence_counter`。

实施时**先读该文件现有的发射写法**，照抄它的 `make_event` 调用形状，不要新造一套。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_run_id_per_turn.py tests/unit/test_background_observe_wiring.py tests/unit/test_background_observe.py -q`
Expected: PASS

- [ ] **Step 5: 全量回归 + lint**

Run: `python -m pytest -q 2>&1 | tail -12`
Expected: 失败集合与基线逐字相同。

Run: `python -m ruff check --output-format=concise src/ctx_weft/core/loop/steps/background_observe.py tests/unit/test_run_id_per_turn.py | grep -v 'RUF00[23]'`
Expected: 空。

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "fix(loop): background observe 快照另起 run_id 并补 RunStarted/RunFinished

dataclasses.replace 的 sequence_counter 是独立 int 副本，与主 run 各自
自增导致重号（原批次二 A4）。快照现在是完整独立的一轮。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: `recognize_intent` 补 run 起止

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/recognize_intent.py:44`
- Test: `tests/unit/test_run_id_per_turn.py`（追加）

**Interfaces:**
- Consumes: Task 6 的断言 helper
- Produces: `recognize_intent` 的 run 有配对起止（原批次二 C5）

- [ ] **Step 1: 写失败测试**

追加到 `tests/unit/test_run_id_per_turn.py`：

```python
async def test_recognize_intent_run_is_paired():
    """它自造 run_id 却不发 RunStarted/RunFinished——host 看到凭空出现又消失的 run（原批次二 C5）。"""
    events = await _recognize_intent_session()
    ri = [e for e in events if (e.origin or "") == "loop.recognize_intent"]
    assert ri, "这个夹具没触发 recognize_intent——先修夹具再断言"
    assert_every_run_has_one_start_and_finish(events)
```

`_recognize_intent_session` 要自己写：**先读 `tests/unit/` 下已有的 recognize_intent 测试**
（`grep -rln recognize_intent tests/unit/`），照它搭台。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_run_id_per_turn.py -q -k recognize`
Expected: FAIL — 该 run_id 无 RunStarted / RunFinished。

- [ ] **Step 3: 实现**

在 `recognize_intent.py` 自造 run_id 的那段前后补配对事件，写法与 Task 7 同源
（照抄该文件已有的 `make_event` 调用形状，`origin` 取 `EventOrigin.LOOP_RECOGNIZE_INTENT`）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_run_id_per_turn.py -q`
Expected: 全部 PASS。**同时去掉 Task 6 Step 4 里临时加的 `xfail` 标记**并确认那两条真绿。

- [ ] **Step 5: 全量回归 + lint**

Run: `python -m pytest -q 2>&1 | tail -12`
Expected: 失败集合与基线逐字相同。

Run: `python -m ruff check --output-format=concise src/ctx_weft/core/loop/steps/recognize_intent.py tests/unit/test_run_id_per_turn.py | grep -v 'RUF00[23]'`
Expected: 空。

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "fix(loop): recognize_intent 补 RunStarted/RunFinished 配对

它自造 run_id 却没有起止事件，host 会看到凭空出现又凭空消失的 run
（原批次二 C5）。至此「每个 run_id 恰有一对起止」全仓成立。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: `compact_session` → `compact_agent` + `CompactReceipt`

**Files:**
- Modify: `src/ctx_weft/core/runtime.py:1806-1964`
- Modify: `src/ctx_weft/core/models/agent.py`（放 `CompactReceipt`）或 `src/ctx_weft/protocols/agent.py`——**放 protocols**，它是 host-facing 返回类型
- Test: `tests/unit/test_compact_agent.py`（新建）

**Interfaces:**
- Consumes: Task 3 的 `record_of`
- Produces:
  ```python
  @dataclass(frozen=True)
  class CompactReceipt:
      session_id: str
      agent_id: str
      task_id: str
      task_id_is_transient: bool
  ```
  `CtxWeftRuntime.compact_agent(agent_id: str, *, task_id: str = "") -> CompactReceipt`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_compact_agent.py
"""compact 主键从 session 翻转到 agent（2026-09-04 spec §7.3）。

压缩折的是 agent 层的 dispatch log，本来就是 agent 粒度的操作；
session 只是从 agent 记录反查出来的。
"""

from __future__ import annotations

import pytest

from ctx_weft.protocols.agent import CompactReceipt
from tests.integration.test_minimal_loop import (
    InlineAgentTemplateProvider,
    make_runtime,
)
from ctx_weft.core.runtime import SessionStartParams

pytestmark = pytest.mark.asyncio


async def _idle_session():
    rt = make_runtime(agent_provider=InlineAgentTemplateProvider())
    h = await rt.start_session(SessionStartParams.create(
        template_id="echo", user_prompt="hi", context_limit=8000,
    ))
    await h.wait_for_finish(timeout=20.0)
    return rt, h


async def test_compact_agent_returns_receipt():
    rt, h = await _idle_session()
    r = await rt.compact_agent(h.agent_id)
    assert isinstance(r, CompactReceipt)
    assert r.agent_id == h.agent_id
    assert r.session_id == h.session_id


async def test_transient_task_id_is_flagged():
    """不传 task_id 时返回的是内存载体 id，事件库里查不到——这条此前只写在 docstring 里。"""
    rt, h = await _idle_session()
    r = await rt.compact_agent(h.agent_id)
    assert r.task_id_is_transient is True


async def test_supplied_task_id_is_not_transient():
    rt, h = await _idle_session()
    r = await rt.compact_agent(h.agent_id, task_id=h.task_id)
    assert r.task_id == h.task_id
    assert r.task_id_is_transient is False


async def test_unknown_agent_raises():
    from ctx_weft.core.models.errors import AgentNotFound
    rt, _ = await _idle_session()
    with pytest.raises(AgentNotFound):
        await rt.compact_agent("agt_nope")


async def test_compact_session_is_gone():
    """不留 shim（spec §1）。"""
    rt, _ = await _idle_session()
    assert not hasattr(rt, "compact_session")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_compact_agent.py -q`
Expected: FAIL — `ImportError: cannot import name 'CompactReceipt'`

- [ ] **Step 3: 实现**

`src/ctx_weft/protocols/agent.py` 末尾加：

```python
@dataclass(frozen=True)
class CompactReceipt:
    """`compact_agent` 的回执。

    `task_id_is_transient=True` 表示 `task_id` 是一个**只用于圈定折叠范围的内存
    载体 id**，事件库里没有对应记录——调用方不要拿它去查。改动前这条约定只活在
    `compact_session` 的 docstring 里，调用方只能靠「我传没传 task_id」自己反推。
    """

    session_id: str
    agent_id: str
    task_id: str
    task_id_is_transient: bool
```

`runtime.py` 把 `compact_session` 改成：

```python
    async def compact_agent(
        self, agent_id: str, *, task_id: str = "",
    ) -> "CompactReceipt":
        """对一个**空闲** agent 的记忆跑一次一次性的纯压缩操作。

        折叠该 agent 层的 dispatch log。传真实 `task_id` 可让那条 task 的 task 层也
        进入折叠范围。该 agent 所在 session 正在跑则抛 `SessionBusyError`。

        `session_id` 从 agent 记录反查（2026-09-04 spec §7.3：压缩本来就是 agent
        粒度的操作，改动前把它挂在 session 上是主次颠倒）。未登记的 `agent_id` 抛
        `AgentNotFound`。

        直接调 `CompactStep.execute`（不走 step driver），但发一对配套的
        RunStarted/RunFinished（总账 C5：孤儿 run_id 会让 host 困惑）——会话投影状态
        不受影响，RunStarted/RunFinished 与 MemoryCompactStarted/MemoryCompacted
        一样都是 reducer no-op。

        并发注意：压缩在途时的 `pause_session` 不会被中途响应——`CompactStep` 不轮询
        pause token。
        """
        rec = self._agent_lifecycle_manager.record_of(agent_id)
        if rec is None:
            raise AgentNotFound(f"unknown agent: {agent_id}")
        session_id = rec.session_id
        transient = not task_id
        ...  # 原 compact_session 的函数体，把 agent_id / session_id 换成上面解出来的
        return CompactReceipt(
            session_id=session_id, agent_id=agent_id,
            task_id=effective_task_id, task_id_is_transient=transient,
        )
```

**函数体照搬**：原 `compact_session` 里「默认取 session root agent」那段逻辑删掉
（现在 agent 是入参），其余不动。原来返回 `{"session_id": ..., "agent_id": ..., "task_id": ...}`
的那行换成上面的 `CompactReceipt`。

全仓改调用方：`grep -rn "compact_session" src/ tests/`，逐个改成 `compact_agent`。
调用方原来传 `session_id` 的，改成传该 session 的 root agent id（测试里一般是
`handle.agent_id`）。读返回值 `["task_id"]` 的改成 `.task_id`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_compact_agent.py -q`
Expected: PASS（5 passed）

- [ ] **Step 5: 全量回归 + lint**

Run: `python -m pytest -q 2>&1 | tail -12`
Expected: 失败集合与基线逐字相同。**特别复核** `tests/integration/test_compact_flow_e2e.py`
—— 它有一条先行失败，改完之后失败的**还是同一条**、不能多。

Run: `python -m ruff check --output-format=concise src/ctx_weft/core/runtime.py src/ctx_weft/protocols/agent.py tests/unit/test_compact_agent.py | grep -v 'RUF00[23]'`
Expected: 空。

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "refactor(api)!: compact_session → compact_agent，返回 CompactReceipt

压缩折的是 agent 层 dispatch log，主键翻转回 agent；session 从记录反查。
CompactReceipt.task_id_is_transient 把原本只写在 docstring 里的
「这个 id 查不到」约定变成可判断的字段。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: `send_message` 返回 `TurnHandle`

**Files:**
- Modify: `src/ctx_weft/core/runtime.py:2048-2086`（`send_message`）、`:2102`（`_inject_user_turn`）、`:2188`（`_start_task_for_agent`）
- Test: `tests/unit/test_runtime_agent_api.py`（追加）、`tests/unit/test_run_id_per_turn.py`（回填 Task 6 的临时写法）

**Interfaces:**
- Consumes: Task 5 的 `TurnHandle`、Task 3 的 `record_of`
- Produces: `send_message(agent_id, content, *, session_id=None) -> TurnHandle`

- [ ] **Step 1: 写失败测试**

追加到 `tests/unit/test_runtime_agent_api.py`：

```python
# ── 2026-09-04 spec §3.3：send_message 返回 TurnHandle ─────────────────────


async def test_send_message_returns_turn_handle():
    from ctx_weft.core.runtime import TurnHandle

    rt = _rt()
    h = await rt.start_session(SessionStartParams.create(
        template_id="echo", user_prompt="hi", context_limit=8000,
    ))
    await h.wait_for_finish(timeout=20.0)

    h2 = await rt.send_message(h.agent_id, "again")
    assert isinstance(h2, TurnHandle)
    assert h2.agent_id == h.agent_id
    assert h2.session_id == h.session_id
    assert h2.task_id            # 恒非空
    assert h2.template_id        # 恒非空


async def test_new_task_branch_yields_a_different_task_id():
    """上一轮已终态 → 新建 task。调用方比对 task_id 就知道是新一轮还是并进旧的。"""
    rt = _rt()
    h = await rt.start_session(SessionStartParams.create(
        template_id="echo", user_prompt="hi", context_limit=8000,
    ))
    await h.wait_for_finish(timeout=20.0)
    h2 = await rt.send_message(h.agent_id, "again")
    assert h2.task_id != h.task_id


async def test_inject_branch_reuses_the_live_task_id():
    """注入分支：消息并进仍在活的那条 task，task_id 与它相同。"""
    rt = _rt()
    _plant_live_task(rt, "agt_1", "tsk_live",
                     task_status="AWAITING_HUMAN", agent_status="waiting_human")
    h = await rt.send_message("agt_1", "外部消息")
    assert h.task_id == "tsk_live"
    assert h.agent_id == "agt_1"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_runtime_agent_api.py -q -k turn_handle`
Expected: FAIL — `AssertionError: isinstance(str, TurnHandle)`

- [ ] **Step 3: 实现**

`_inject_user_turn` 与 `_start_task_for_agent` 都改成返回 `task_id`（前者现在返回
`None`，加一个 `return target.id`；后者已经返回 `task.id`，不动）。

`send_message` 末尾把 task_id 包成句柄：

```python
    async def send_message(
        self,
        agent_id: str,
        content: "str | list[ContentPart]",
        *,
        session_id: str | None = None,
    ) -> "TurnHandle":
        """向指定 agent 发一条外部消息，返回这次交互的 `TurnHandle`（spec §4.1；
        2026-09-04 spec §3.3）——agent-centric 的核心入口：外部消息按 agent 显式寻址，
        不再隐式挂「当前唯一活跃 task」。

        守卫：不存在 / `terminated` / `running` 一律抛错，**不排队**
        （`AgentLifecycleManager.assert_can_receive`）；调用方自行重试，或先
        pause/cancel。`session_id` 只用于提前发现「这个 agent 不属于该 session」这类
        误用，**不参与路由**——`agent_id` 全局唯一，路由永远只看 `current_task_id`。

        路由三条路径（spec §4.2）：

        - `current_task_id` 已终态或为空 —— 新建 task 挂给该 agent
          （`_start_task_for_agent`，走既有 `push_task` 通路）。
        - 未终态、且不是「SUSPENDED 等子任务」—— 注入并重排该活 task。
        - 未终态、且 `_suspended_on_live_children` —— 只把消息写进对话，
          `_try_resume_parent` 在子任务收尾时自然唤醒它。

        三条都返回句柄，`task_id` 恒非空。调用方要区分「开了新一轮」还是「并进旧的」，
        比对返回的 `task_id` 与调用前 `get_agent(agent_id).current_task_id` 即可——
        句柄里不放这个布尔，也不放 run_id（第三条路径在返回那一刻还没有新一轮，
        见 2026-09-04 spec §3.2）。
        """
        reg = self._agent_lifecycle_manager
        reg.assert_can_receive(agent_id)
        rec = reg.record_of(agent_id)
        if rec is None:
            raise AgentNotFound(f"unknown agent: {agent_id}")
        if session_id is not None and session_id != rec.session_id:
            raise ValueError(
                f"agent {agent_id} belongs to session {rec.session_id!r}, not {session_id!r}"
            )

        current = rec.current_task_id
        if current and not self._task_is_terminal(rec.session_id, current):
            task_id = await self._inject_user_turn(
                current, content, session_id=rec.session_id)
        else:
            task_id = await self._start_task_for_agent(agent_id, content)

        return TurnHandle(
            session_id=rec.session_id,
            agent_id=agent_id,
            task_id=task_id,
            template_id=rec.template_id,
            event_bus=self._event_bus,
        )
```

全仓改调用方：`grep -rn "send_message(" src/ tests/`（37 处测试）。读返回值当 task_id
用的，改成 `.task_id`。

**回填 Task 6 的临时写法**：`tests/unit/test_run_id_per_turn.py` 的 `_two_turn_session`
改回：

```python
    h2 = await rt.send_message(h.agent_id, "second turn")
    await h2.wait_for_finish(timeout=20.0)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_runtime_agent_api.py tests/unit/test_run_id_per_turn.py -q`
Expected: PASS

- [ ] **Step 5: 全量回归 + lint**

Run: `python -m pytest -q 2>&1 | tail -12`
Expected: 失败集合与基线逐字相同。

Run: `python -m ruff check --output-format=concise src/ctx_weft/core/runtime.py tests/unit/test_runtime_agent_api.py tests/unit/test_run_id_per_turn.py | grep -v 'RUF00[23]'`
Expected: 空。

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "refactor(api)!: send_message 返回 TurnHandle 而非裸 task_id

三条路径（新建 / 注入重排 / 注入不重排）统一形状，task_id 恒非空。
调用方比对返回 task_id 与调用前 current_task_id 即可区分是否开了新一轮。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Phase C · 恢复换轴

顺序不可颠倒：先让 `AGENT_*` 现状广播落地（Task 11），才能停发 `TASK_QUEUE_*`（Task 12）——
否则中间会有一个「崩溃后 host 投影收不到任何恢复信号」的窗口。

### Task 11: `recover()` 装填 ALM + 恢复期 `AGENT_*` 现状广播

**Files:**
- Modify: `src/ctx_weft/core/runtime.py:2560-2599`（`recover`）
- Modify: `src/ctx_weft/core/orchestrator/lifecycle/agent_manager.py`（`load` 末尾广播）
- Test: `tests/unit/test_recover_loads_agents.py`（新建）

**Interfaces:**
- Consumes: Task 3 的 `record_of`
- Produces: `recover() -> int` 返回**恢复的 agent 数**；`ALM.load()` 装填后按现状发
  `AGENT_IDLE` / `AGENT_WAITING_HUMAN` / `AGENT_INTERRUPTED`。Task 12 依赖这个广播已存在。

**背景**：`recover()` 现在每个 session 只做 `rebuild_hitl` + `SessionRegistry.register_session`，
**从不调 `ALM.load()`**（全仓仅 `recover_session` 一处调）。后果是重启后 `list_agents` 返回空、
`get_agent` 抛 `AgentNotFound`、`send_message` / `cancel_agent` / `pause_agent` 全部失败，
直到某条 HITL 冷应答或 `/resume` 恰好走过那条路。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_recover_loads_agents.py
"""冷启动装填 ALM + 恢复期 AGENT_* 现状广播（2026-09-04 spec §6.3 / §6.4）。

「恢复不是一种状态」的纪律保持：发的是从事件折出来的**现状**，不是新状态，
不引入 RECOVERING 之类的值域。
"""

from __future__ import annotations

import pytest

from ctx_weft.protocols.events import EventType

pytestmark = pytest.mark.asyncio


async def _crashed_session(rt):
    """跑一轮把事件写进 store，然后把进程内内存态清空模拟重启。

    实施时**先读 `tests/unit/test_recover_routing.py`**——它已经有一套「造一个
    崩溃会话的事件流再 recover」的搭台手法，照抄，不要新造。
    返回 (session_id, root_agent_id)。
    """
    raise NotImplementedError("照 test_recover_routing.py 的手法实现")


async def test_recover_populates_agent_registry():
    """核心回归：重启后 list_agents 立刻看得见，不必等某条冷应答。"""
    rt = ...  # 照 test_recover_routing.py 建 runtime
    session_id, root_agent_id = await _crashed_session(rt)

    assert rt.list_agents(session_id=session_id) == []      # 装填前
    await rt.recover()
    ids = {a.agent_id for a in rt.list_agents(session_id=session_id)}
    assert root_agent_id in ids


async def test_recover_returns_agent_count_not_session_count():
    """报告单位换成 agent（spec §6.2）。"""
    rt = ...
    await _crashed_session(rt)
    n = await rt.recover()
    assert n == len(rt.list_agents())


async def test_get_agent_works_right_after_recover():
    rt = ...
    session_id, root_agent_id = await _crashed_session(rt)
    await rt.recover()
    d = rt.get_agent(root_agent_id)
    assert d.session_id == session_id


async def test_load_broadcasts_current_status():
    """装填完按折出来的现状发 AGENT_*，host 投影因此不会停在崩溃前的状态。"""
    seen = []
    rt = ...
    rt.event_bus.subscribe(None, lambda ev: seen.append(ev) or None)
    await _crashed_session(rt)
    await rt.recover()

    agent_events = [e.type for e in seen if str(e.type).startswith("Agent")]
    assert any(t in agent_events for t in (
        EventType.AGENT_IDLE, EventType.AGENT_WAITING_HUMAN, EventType.AGENT_INTERRUPTED,
    )), f"恢复期没有 AGENT_* 现状广播: {agent_events}"


async def test_broadcast_carries_the_folded_status_not_a_default():
    """有未决 HITL 的 agent 恢复后必须是 waiting_human，不是被字段默认值重置成 idle。"""
    rt = ...
    session_id, root_agent_id = await _crashed_session(rt)   # 该夹具留一条未决 HITL
    await rt.recover()
    assert rt.get_agent(root_agent_id).status == "waiting_human"
```

> 上面五条的搭台部分（`rt = ...` 与 `_crashed_session`）**必须**照
> `tests/unit/test_recover_routing.py` 已有的写法补全——那个文件已经解决了
> 「造一个可 recover 的事件流」这个问题，重造一套只会两边漂移。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_recover_loads_agents.py -q`
Expected: FAIL — `recover()` 之后 `list_agents` 仍是空。

- [ ] **Step 3: 实现**

**(a) `ALM.load()` 末尾广播现状。** 在 `load` 的 `return n` 之前插入：

```python
        # 恢复期现状广播（2026-09-04 spec §6.4）：装填完把折出来的状态照实发一遍，
        # 取代此前由 runtime 代 TaskManager 发的 TASK_QUEUE_*（那条信号的消费者
        # SessionRegistry 早已不订阅它）。
        #
        # 「恢复不是一种状态」：这里发的是**现状**不是新状态，不引入 RECOVERING
        # 之类的值域，也不经状态机——`apply_input` 是给真实转移用的，这里是把已经
        # 成立的事实广播出去，走同一个发射点（ALM 是 AGENT_* 的唯一发射者）。
        #
        # `terminated` 不发：它是粘滞终态，host 投影里本就已经是终态，重发没有信息。
        for av in agent_views.values():
            event_type = _RECOVERY_BROADCAST_BY_STATUS.get(av.status)
            if event_type is None:
                continue
            await emit_event(
                self.event_bus, event_type,
                session_id=session_id, tenant_id=tenant_id,
                origin=_ORIGIN, task_id=av.current_task_id, agent_id=av.id,
                payload={"reason": "recovered"},
            )
```

模块级加映射表（放在 `_INPUT_BY_EVENT` 附近）：

```python
#: 恢复期现状广播的状态 → 事件映射。`terminated` 刻意不在表里（粘滞终态，重发无信息）；
#: `running` 也不在——进程刚起来没有任何 run 在跑，把折出来的 `running` 照发会让 host
#: 以为有活在跑。它在事件流里的真实含义是「崩溃时正在跑」，恢复后等 /resume 重新派发。
_RECOVERY_BROADCAST_BY_STATUS: ClassVar[dict[str, EventType]] = {
    "idle": EventType.AGENT_IDLE,
    "waiting_human": EventType.AGENT_WAITING_HUMAN,
    "interrupted": EventType.AGENT_INTERRUPTED,
}
```

（`emit_event` / `_ORIGIN` 照该文件已有的发射写法，不新造。）

**(b) `recover()` 装填 ALM 并按 agent 计数。**

```python
    async def recover(self) -> int:
        """重启后恢复每一个仍活跃的 session（有 SessionCreated、无终态事件）。

        决定**在 core 内据事件做出**（不查 host 投影、不做全量重放）：内存
        `HitlRegistry` 被装填（`/hitl/pending` 与应答端点因此可用），session 登记进
        `SessionRegistry`，**agent 记录装填进 `AgentLifecycleManager`**——这一步是
        2026-09-04 spec §6.3 补上的：此前只有 `recover_agent` 会装填 ALM，重启后
        `list_agents` / `get_agent` / `send_message` 在第一条冷应答到来之前全部是瞎的。

        装填之后由 `ALM.load()` 按折出来的现状发 `AGENT_*`（spec §6.4），host 投影
        因此不会停在崩溃前的状态。

        启动时**什么都不 drain、不跑**：等人应答的会话继续等，被打断的等 `/resume`。
        在 app lifespan 里、注册完 providers 之后、开始服务之前调用。

        **返回恢复的 agent 数**（spec §6.2：报告单位换成 agent），不是 session 数。
        """
        try:
            session_ids = await self.event_store.list_active_session_ids()
        except NotImplementedError:
            logger.warning("Recovery: EventStore does not support list_active_session_ids — skipped")
            return 0

        total_agents = 0
        for session_id in session_ids:
            try:
                # tenant 必须先解出来：`_task_managers` 此刻恒为空，`_tenant_for_session`
                # 会落到读事件日志那条路（SESSION_CREATED 首条即含真 tenant）；
                # `register_session` 与 ALM 装填都要用同一个值，否则由它们派生的事件
                # 会落错租户（总账 A5）。
                tenant_id = await self._tenant_for_session(session_id)
                await self.rebuild_hitl(session_id)
                self._session_registry.register_session(session_id, tenant_id=tenant_id)
                total_agents += await self._load_agents_of(session_id, tenant_id=tenant_id)
            except Exception:
                logger.exception("Recovery: failed to recover session %s", session_id)

        return total_agents
```

新增私有 helper（Task 13 的 `rebuild_agent` 会复用它，不要写两份折叠逻辑）：

```python
    async def _load_agents_of(self, session_id: str, *, tenant_id: str) -> int:
        """据事件折出该 session 的 AgentView 并喂进 ALM，返回装填条数。

        `recover()` 与 `rebuild_agent()` 共用的唯一装填路径——「恢复是喂进来、不是
        查回去」（spec §3.1），折叠逻辑只此一份。

        `fallback_template_id`：存量事件流里子 agent 没发过 AgentInstantiated，
        `AgentView.template_id` 会是空串，`ALM.load` 用这个值回落。取该 session 的
        root 模板，与 `recover_agent` 同一口径。
        """
        from ctx_weft.core.control.reducers import rebuild_view

        view = await rebuild_view(self.event_store, session_id)
        fallback = ...    # 该 session 的 root template_id，照 _recover_session_locked 的解法
        return await self._agent_lifecycle_manager.load(
            view.agents, session_id=session_id,
            tenant_id=tenant_id, fallback_template_id=fallback,
        )
```

> `rebuild_view` 是 `core.control.reducers` 的模块函数（`runtime.py:1510-1511` 已有同样的
> 局部 import 写法，照抄）。`fallback` 那一行**照 `_recover_session_locked`（`:1479` 起）
> 现有的取法照抄**——它已经做过一遍「从事件折出 `RunStateView` + 解出 session 模板」。
> 实施时把那两步抽成这个 helper，`_recover_session_locked` 改调它，两边共用，不要复制
> 两份折叠逻辑。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_recover_loads_agents.py tests/unit/test_recover_routing.py -q`
Expected: PASS

- [ ] **Step 5: 全量回归 + lint**

Run: `python -m pytest -q 2>&1 | tail -12`
Expected: 失败集合与基线逐字相同。

Run: `python -m ruff check --output-format=concise src/ctx_weft/core/runtime.py src/ctx_weft/core/orchestrator/lifecycle/agent_manager.py tests/unit/test_recover_loads_agents.py | grep -v 'RUF00[23]'`
Expected: 空。

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "fix(recover): recover() 装填 ALM 并广播 AGENT_* 现状

此前只有 recover_session 会调 ALM.load()，重启后整个 agent 面在第一条
冷应答到来之前是瞎的（list_agents 空 / get_agent 抛 AgentNotFound）。
装填后按折出来的现状发 AGENT_*，取代即将停发的 TASK_QUEUE_* 代播。
recover() 返回值语义改为 agent 数。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 12: 三个 `TASK_QUEUE_*` 停发

**Files:**
- Modify: `src/ctx_weft/protocols/events.py:254`（`L_TIER_EVENT_TYPES`）
- Modify: `src/ctx_weft/core/orchestrator/task/manager.py:1014-1055`（`announce_queue_state` / `_final_status`）
- Modify: `src/ctx_weft/core/runtime.py:1108`、`:2601-2636`（删 `_announce_queue_state_as_tm_proxy`）
- Test: `tests/unit/test_recover_loads_agents.py`（追加）

**Interfaces:**
- Consumes: Task 11 的 `AGENT_*` 现状广播（替代物必须先在）
- Produces: L 档由 17 增至 20；`announce_queue_state` 与 `_announce_queue_state_as_tm_proxy` 不复存在

**背景**：三个 `TASK_QUEUE_*` 在 core 里**已无消费者**——`SessionRegistry` 自 09-03 起
只订阅 `AGENT_INSTANTIATED` / `AGENT_SPAWNED`（`session_registry.py:98-104`）。
`_announce_queue_state_as_tm_proxy` 的存在理由是「代 TaskManager 给 SessionRegistry 发那一条
它唯一的输入」，那个消费者已经不存在，整个方法是一次空播。

- [ ] **Step 1: 写失败测试**

追加到 `tests/unit/test_recover_loads_agents.py`：

```python
# ── 2026-09-04 spec §6.4 / §9：TASK_QUEUE_* 停发 ──────────────────────────


async def test_no_task_queue_events_are_emitted():
    """三个类型进 L 档：枚举与 reducer 分支保留，但不再有发射点。"""
    seen = []
    rt = ...
    rt.event_bus.subscribe(None, lambda ev: seen.append(ev) or None)
    session_id, _ = await _crashed_session(rt)
    await rt.recover()

    queue_events = [e.type for e in seen if str(e.type).startswith("TaskQueue")]
    assert queue_events == [], f"仍在发 TASK_QUEUE_*: {queue_events}"


def test_task_queue_types_are_registered_in_l_tier():
    from ctx_weft.protocols.events import L_TIER_EVENT_TYPES
    assert {"TaskQueueBlocked", "TaskQueueInterrupted", "TaskQueueDrained"} <= L_TIER_EVENT_TYPES


def test_l_tier_has_twenty_entries():
    """17 + 3。数字写死是为了让「悄悄多停一个」这件事必须显式改测试。"""
    from ctx_weft.protocols.events import L_TIER_EVENT_TYPES
    assert len(L_TIER_EVENT_TYPES) == 20


def test_proxy_announcer_is_gone():
    from ctx_weft.core.runtime import CtxWeftRuntime
    assert not hasattr(CtxWeftRuntime, "_announce_queue_state_as_tm_proxy")


def test_task_manager_has_no_announce_queue_state():
    from ctx_weft.core.orchestrator.task.manager import TaskManager
    assert not hasattr(TaskManager, "announce_queue_state")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_recover_loads_agents.py -q -k "task_queue or l_tier or proxy or announce"`
Expected: FAIL — 仍在发；`L_TIER_EVENT_TYPES` 只有 17 条。

- [ ] **Step 3: 实现**

**(a) 登记 L 档。** `protocols/events.py` 的 `L_TIER_EVENT_TYPES` 末尾加：

```python
    # 2026-09-04（runtime 对外面 agent-centric 对齐 Task 12）：这 3 个队列级聚合信号
    # 在 core 里已无消费者——SessionRegistry 自 2026-09-03 起只订阅 AGENT_INSTANTIATED /
    # AGENT_SPAWNED，不再消费队列信号。恢复期的可观测性由 ALM 装填后的 AGENT_* 现状
    # 广播承担（spec §6.4）。枚举成员与 reducer 分支按 docs/events-v2.md §5 保留。
    "TaskQueueBlocked", "TaskQueueInterrupted", "TaskQueueDrained",
```

**(b) 删 `TaskManager.announce_queue_state`。** 它的全部作用就是发这三个事件，
停发后是纯空方法。连同它一起：

- 删 `manager.py:1072` 与 `:1096` 两个调用点。
- `_final_status()`：先 `grep -n "_final_status" src/ tests/`。若只被 `announce_queue_state`
  用，一并删；若还有别的读者，**保留**并在本 Task 记录读者是谁。

**(c) 删 `runtime._announce_queue_state_as_tm_proxy`** 整个方法（`:2601-2636`），
以及 `run_single_task` 的 `finally` 里那行 `await task_manager.announce_queue_state()`（`:1108`）。

**(d)** `grep -rn "announce_queue_state\|TaskQueue" src/ tests/` 复核残留：
- `src/` 里除 `EventType` 枚举定义、`L_TIER_EVENT_TYPES`、`reducers._apply` 的分支外应无引用。
- `tests/` 里断言这三个事件被发出的用例要改——它们断言的行为**已经被本 Task 有意删除**，
  改成断言 `AGENT_*` 现状广播（Task 11 的口径），或直接删掉那条断言并在 commit message 里写明。
  `tests/unit/test_recover_routing.py:15` 的模块 docstring 提到该方法，一并改文字。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_recover_loads_agents.py tests/unit/test_recover_routing.py -q`
Expected: PASS

- [ ] **Step 5: 全量回归 + lint**

Run: `python -m pytest -q 2>&1 | tail -12`
Expected: 失败集合与基线逐字相同。

**不变式复核**（`docs/events-v2.md` §6）——跑仓内既有的不变式测试：
Run: `python -m pytest -q -k "event_type or invariant or l_tier"`
Expected: PASS。三条不变式：全集 ≡ 实际发射 ∪ L 档；S/O/L 两两不交且并集为全集；
L 档 ∩ 实际发射 = ∅。

Run: `python -m ruff check --output-format=concise src/ctx_weft/protocols/events.py src/ctx_weft/core/orchestrator/task/manager.py src/ctx_weft/core/runtime.py | grep -v 'RUF00[23]'`
Expected: 空。

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "refactor(events)!: 三个 TASK_QUEUE_* 停发进 L 档

SessionRegistry 自 2026-09-03 降格后不再订阅队列信号，这三个类型在 core 里
已无消费者；_announce_queue_state_as_tm_proxy 代发的对象根本不存在，是一次
空播。恢复期可观测性改由 ALM 装填后的 AGENT_* 现状广播承担。
枚举值与 reducer 分支按 events-v2 §5 保留。L 档 17 → 20。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 13: `rebuild_agent` / `rebuild_all_agents`

**Files:**
- Modify: `src/ctx_weft/core/runtime.py`（`rebuild_all_pending_hitl` 附近）
- Test: `tests/unit/test_rebuild_agent.py`（新建）

**Interfaces:**
- Consumes: Task 11 的 `_load_agents_of`
- Produces: `rebuild_agent(agent_id: str) -> bool`、`rebuild_all_agents() -> int`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_rebuild_agent.py
"""agent 侧的按需自愈入口（2026-09-04 spec §6.2）。

与 HITL 的 rebuild_hitl / rebuild_all_pending_hitl 一一对称：recover() 没跑过的
进程（测试、嵌入场景）也要能把 agent 面装填回来。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_rebuild_agent_populates_one_agent():
    rt = ...
    session_id, root_agent_id = await _crashed_session(rt)   # 照 Task 11 的夹具
    assert rt.list_agents() == []
    assert await rt.rebuild_agent(root_agent_id) is True
    assert rt.get_agent(root_agent_id).session_id == session_id


async def test_rebuild_agent_unknown_returns_false():
    """事件流里也找不到这个 agent —— 不抛，返回 False，调用方自己决定怎么报。"""
    rt = ...
    await _crashed_session(rt)
    assert await rt.rebuild_agent("agt_nope") is False


async def test_rebuild_agent_is_idempotent():
    rt = ...
    _, root_agent_id = await _crashed_session(rt)
    assert await rt.rebuild_agent(root_agent_id) is True
    assert await rt.rebuild_agent(root_agent_id) is True
    assert len([a for a in rt.list_agents() if a.agent_id == root_agent_id]) == 1


async def test_rebuild_all_agents_returns_total():
    rt = ...
    await _crashed_session(rt)
    n = await rt.rebuild_all_agents()
    assert n == len(rt.list_agents())
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_rebuild_agent.py -q`
Expected: FAIL — `AttributeError: 'CtxWeftRuntime' object has no attribute 'rebuild_agent'`

- [ ] **Step 3: 实现**

```python
    async def rebuild_agent(self, agent_id: str) -> bool:
        """据事件把**单个** agent 装填进 ALM；找不到返回 False。

        `rebuild_hitl` 的 agent 侧对应物（2026-09-04 spec §6.2）。用于 `recover()`
        没跑过的进程（测试、嵌入场景），或运行期发现某个 agent 记录缺失时的按需自愈。

        **实现是 `_load_agents_of` 的一次调用后再查一次**，不另写折叠逻辑：
        `agent_id` 全局唯一但事件按 session 分区存（spec §6.1），要定位它就得先知道
        它属于哪个 session——已在内存里的直接读 `record_of`，不在内存里的扫活跃
        session 逐个装填（`rebuild_all_agents` 的路径），装完再查一次。

        幂等：`ALM.load` 对同一批 `AgentView` 重复调用只是覆盖同值。
        """
        rec = self._agent_lifecycle_manager.record_of(agent_id)
        if rec is not None:
            return True
        await self.rebuild_all_agents()
        return self._agent_lifecycle_manager.record_of(agent_id) is not None

    async def rebuild_all_agents(self) -> int:
        """据事件把**所有 active session** 的 agent 装填进 ALM，返回总条数。

        `rebuild_all_pending_hitl` 的 agent 侧对应物。不发中断、不 drain、不派发任何
        任务——与 `recover()` 的「启动时 nothing runs」同一纪律，区别只是它不碰 HITL。
        """
        try:
            session_ids = await self.event_store.list_active_session_ids()
        except NotImplementedError:
            return 0
        total = 0
        for sid in session_ids:
            try:
                tenant_id = await self._tenant_for_session(sid)
                total += await self._load_agents_of(sid, tenant_id=tenant_id)
            except Exception:
                logger.exception("rebuild_all_agents: failed for session %s", sid)
        return total
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_rebuild_agent.py -q`
Expected: PASS（4 passed）

- [ ] **Step 5: 全量回归 + lint**

Run: `python -m pytest -q 2>&1 | tail -12`
Expected: 失败集合与基线逐字相同。

Run: `python -m ruff check --output-format=concise src/ctx_weft/core/runtime.py tests/unit/test_rebuild_agent.py | grep -v 'RUF00[23]'`
Expected: 空。

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "feat(recover): 加 rebuild_agent / rebuild_all_agents 自愈入口

与 rebuild_hitl / rebuild_all_pending_hitl 一一对称。装填逻辑复用
_load_agents_of，不写第二份折叠。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 14: 删 `session_status_after_recover` 与 `core/hitl/status.py`

**Files:**
- Modify: `src/ctx_weft/core/runtime.py:2734-2753`
- Delete: `src/ctx_weft/core/hitl/status.py`
- Test: `tests/unit/test_hitl_agent_filter.py`（追加）

**Interfaces:**
- Consumes: Task 2 的 `HitlRequestView.delivery`
- Produces: 无（纯删除）

**背景**：它返回的 `"PAUSED"` / `"PAUSED_HITL"` 不是任何一个状态机的值域，算的是
「未决 HITL 的 delivery 性质」。会话状态机 2026-09-02 已删，这个 session 级派生串没有
存在理由。`delivery` 一暴露（Task 2），host 直接按原始事实判即可。

- [ ] **Step 1: 写失败测试**

追加到 `tests/unit/test_hitl_agent_filter.py`：

```python
# ── 2026-09-04 spec §6.5：派生的 session 级状态串删除 ──────────────────────


def test_session_status_after_recover_is_gone():
    from ctx_weft.core.runtime import CtxWeftRuntime
    assert not hasattr(CtxWeftRuntime, "session_status_after_recover")
    assert not hasattr(CtxWeftRuntime, "_derive_paused_status")


def test_hitl_status_module_is_gone():
    import importlib
    import pytest
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("ctx_weft.core.hitl.status")


def test_host_can_derive_the_same_thing_from_delivery():
    """删掉的那个判据，host 用 delivery 自己就能算——这才是删它的前提。"""
    from ctx_weft.protocols.hitl import UserTurnDelivery

    reg = HitlRegistry()
    _open(reg, "h1", session_id="s1", agent_id="agt_1", delivery=UserTurnDelivery())
    views = [r.to_view() for r in reg.list_pending(session_id="s1")]
    only_user_turns = all(isinstance(v.delivery, UserTurnDelivery) for v in views)
    assert only_user_turns is True          # 等价于旧的 "PAUSED"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_hitl_agent_filter.py -q -k "gone or derive"`
Expected: FAIL — 两个属性都还在，模块可导入。

- [ ] **Step 3: 实现**

1. 删 `runtime.py` 的 `session_status_after_recover` 与 `_derive_paused_status` 两个方法。
2. 删 `src/ctx_weft/core/hitl/status.py` 整个文件。
3. 删 `runtime.py` 里 `from ctx_weft.core.hitl.status import paused_status_for` 这行 import。
4. `grep -rn "session_status_after_recover\|paused_status_for\|hitl.status" src/ tests/` 复核：
   测试里调它的（约 18 处）**改成断言 delivery**，或直接删掉那条断言。它断言的行为已被
   本 Task 有意删除，不是回归。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_hitl_agent_filter.py -q`
Expected: PASS

- [ ] **Step 5: 全量回归 + lint**

Run: `python -m pytest -q 2>&1 | tail -12`
Expected: 失败集合与基线逐字相同。

Run: `python -m ruff check --output-format=concise src/ctx_weft/core/runtime.py | grep -v 'RUF00[23]'`
Expected: 空（特别盯 `F401`——删 import 后的残留）。

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "refactor(api)!: 删 session_status_after_recover 与 core/hitl/status.py

它返回的 PAUSED / PAUSED_HITL 不是任何状态机的值域，算的是 delivery 的
性质；会话状态机 2026-09-02 已删。delivery 现在是 HitlRequestView 的契约
字段，host 按原始事实自判，不需要 core 再给一个派生的 session 级串。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 15: `recover_session` → `recover_agent`

**Files:**
- Modify: `src/ctx_weft/core/runtime.py:1449-1478`（签名与 docstring）、`:2267`（`_resume_after_hitl` 调用点）
- Modify: 全仓调用方（91 处测试引用）
- Test: 既有测试改名后即为验收

**Interfaces:**
- Consumes: Task 3 的 `record_of`
- Produces: `recover_agent(agent_id: str, *, user_reply=None, resumed_task_id=None, hitl_id="") -> None`

**这是一次机械改名 + 主键翻转**，内部实现（per-session 锁、owner TM 复用、
`_recover_session_locked`）**一行不动**——session 仍是串行化与资源回收的单位，
那是实现事实（spec §6.1）。

- [ ] **Step 1: 写失败测试**

在 `tests/unit/test_recover_routing.py` 追加：

```python
# ── 2026-09-04 spec §6.2：恢复入口换轴 ─────────────────────────────────────


def test_recover_session_is_gone():
    from ctx_weft.core.runtime import CtxWeftRuntime
    assert not hasattr(CtxWeftRuntime, "recover_session")


async def test_recover_agent_resolves_session_from_the_record():
    """调用方只给 agent_id，session 由 ALM 记录反查——这就是「换轴」的全部含义。"""
    rt = ...
    session_id, root_agent_id = await _crashed_session(rt)
    await rt.recover()
    await rt.recover_agent(root_agent_id)          # 不传 session_id 也能跑通
    assert rt.get_agent(root_agent_id).session_id == session_id


async def test_recover_agent_unknown_raises():
    from ctx_weft.core.models.errors import AgentNotFound
    import pytest
    rt = ...
    with pytest.raises(AgentNotFound):
        await rt.recover_agent("agt_nope")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_recover_routing.py -q -k "recover_agent or recover_session_is_gone"`
Expected: FAIL — `recover_agent` 不存在。

- [ ] **Step 3: 实现**

```python
    async def recover_agent(
        self,
        agent_id: str,
        *,
        user_reply: "PendingHitl | None" = None,
        resumed_task_id: str | None = None,
        hitl_id: str = "",
    ) -> None:
        """续跑一个 agent：复用活 owner TM，或据事件重建后 drain。

        **主键是 agent**（2026-09-04 spec §6.2）；`session_id` 由 ALM 记录反查。
        session 仍是串行化与资源回收的单位——per-session 锁、owner TM 复用这些
        **实现事实**一行未改，它只是不再是对外的语义单位（spec §6.1）。

        单 owner 架构：若该 agent 所在 session 已有**存活的 owner TM** 且拥有被应答的
        `resumed_task_id`，就把应答作为消息投递给它、就地重驱
        （`_resume_in_existing_tm`），**不重建 TM**——从根上消除「多 TM 顶替 / 跨 TM
        双跑」。仅当无存活 owner（真崩溃冷启动 / `/resume` / 活 TM 不含该 task）才从
        事件日志重建。

        不收 llm_account/llm_model：续跑路径一概不碰模型。换模型走 `set_agent_llm` /
        `set_session_llm`，registry 是模型选择的唯一住所，续跑只负责把已经存在的选择
        重新派发出去。

        `hitl_id`：冷 HITL 应答触发的续跑才有意义——`_resume_after_hitl` 总是传
        `req.id`。纯 `/resume`（无 hitl 语境）留空。

        未登记的 `agent_id` 抛 `AgentNotFound`——冷启动后要先 `recover()` 或
        `rebuild_agent()` 把记录装填进来（「恢复是喂进来、不是查回去」）。
        """
        rec = self._agent_lifecycle_manager.record_of(agent_id)
        if rec is None:
            raise AgentNotFound(f"unknown agent: {agent_id}")
        session_id = rec.session_id
        lock = self._resume_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            await self._recover_session_locked(
                session_id, user_reply=user_reply,
                resumed_task_id=resumed_task_id, hitl_id=hitl_id,
            )
```

`_resume_after_hitl`（`:2267`）里的调用改成 `await self.recover_agent(req.agent_id, ...)`。

**`_recover_session_locked` 保留原名**——它是私有的、确实以 session 为单位工作，
改名只会掩盖「串行化的对象是 TM」这个实现事实。

全仓改调用方：`grep -rn "recover_session" src/ tests/`（91 处）。测试里传 `session_id`
的改成传该 session 的 root agent id。**若某个测试确实只有 session_id 没有 agent_id**，
先 `rt.list_agents(session_id=sid)` 取 root（`parent_agent_id is None` 的那个）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_recover_routing.py tests/unit/test_runtime_hitl_wiring.py -q`
Expected: PASS

- [ ] **Step 5: 全量回归 + lint**

Run: `python -m pytest -q 2>&1 | tail -12`
Expected: 失败集合与基线逐字相同。

**复核**：`grep -rn "def recover_session\|\.recover_session(" src/ tests/` 必须无输出。

Run: `python -m ruff check --output-format=concise src/ctx_weft/core/runtime.py | grep -v 'RUF00[23]'`
Expected: 空。

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "refactor(api)!: recover_session → recover_agent，主键换成 agent

session 由 ALM 记录反查。per-session 锁与 owner TM 复用一行未改——它们是
实现事实，只是不再是对外的语义单位。_recover_session_locked 保留原名。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Phase D · 会话级 API 与收口

### Task 16: `pause_task` 内部化 + `pause_session` 具名化

**Files:**
- Modify: `src/ctx_weft/core/runtime.py:669-723`（`pause_session` / `pause_task`）
- Test: `tests/unit/test_runtime_pause_wiring.py`（追加）

**Interfaces:**
- Consumes: 无
- Produces: `_pause_task(session_id, task_id) -> bool`（私有）；`pause_session` 行为不变

**关键约束：语义一个字不能变。** `pause_session` 现在对非 root agent 做的是**取消它们的
在途 run**——agent 经 `TASK_CANCELED` → ALM 的 `AgentInput.SETTLED` 落回 `idle`，仍然活着、
仍可被 `send_message` 寻址。**这里刻意不用 `cancel_agent`**：那会把它们推到 `terminated`，
是语义变更（2026-09-04 spec §7.1）。本 Task 的收敛只落在「把内联遍历 `_run_tokens` 的
循环体换成对两个具名原语的调用」。

- [ ] **Step 1: 写失败测试**

追加到 `tests/unit/test_runtime_pause_wiring.py`：

```python
# ── 2026-09-04 spec §7.1 / §8：pause_task 内部化，pause_session 语义不变 ──


def test_pause_task_is_no_longer_public():
    from ctx_weft.core.runtime import CtxWeftRuntime
    assert not hasattr(CtxWeftRuntime, "pause_task")
    assert hasattr(CtxWeftRuntime, "_pause_task")


async def test_pause_session_leaves_non_root_agents_alive():
    """回归护栏：非 root agent 被取消的是 run，不是 agent 本身。

    这条在改动前就该是绿的——它锁死的正是「不要顺手改成 cancel_agent」。
    """
    rt = ...   # 建一个 root + 一个子 agent 都在跑的会话
    root_id, child_id = ...

    await rt.pause_session(session_id)

    assert rt.get_agent(child_id).status != "terminated"
    # 仍可寻址：terminated 的 agent 会被 assert_can_receive 拒绝
    rt._agent_lifecycle_manager.assert_can_receive(child_id)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_runtime_pause_wiring.py -q -k "no_longer_public or leaves_non_root"`
Expected: 第一条 FAIL（`pause_task` 仍是公开的）；第二条**应该 PASS**——它是护栏，
改动前后都必须绿。若它一开始就红，说明现状与 spec §7.1 的描述不符，**停下来报告**。

- [ ] **Step 3: 实现**

1. `pause_task` 改名为 `_pause_task`，docstring 首行改成
   「**内部原语**：定向暂停指定在途 task 的 run……`pause_agent` 与 `pause_session` 是
   它仅有的两个调用方（2026-09-04 spec §8）。」
2. `pause_session` 的 run token 遍历循环体换成具名调用：

```python
        for task_id, tokens in list(per.items()):
            if root_agent and tm is not None and tm.running_agent_of(task_id) == root_agent:
                self._pause_task(session_id, task_id)
                # 在途 root run 即唯一续跑点：认领名额，闩锁窗口内此后派发的 root scope
                # 任务（如被 _try_resume_parent 重排的 SUSPENDED root 任务）born-cancel。
                self._pause_claimed.add(session_id)
            else:
                # 非 root：取消它的**在途 run**，不是取消这个 agent——它经 TASK_CANCELED
                # → AgentInput.SETTLED 落回 idle，仍然活着、仍可被 send_message 寻址。
                # 刻意不用 cancel_agent：那会把它推到 terminated，是语义变更
                # （2026-09-04 spec §7.1）。
                self._cancel_run_token(session_id, task_id)
```

3. `pause_agent` 里的 `self.pause_task(...)` 改成 `self._pause_task(...)`。
4. `grep -rn "\.pause_task(" src/ tests/` 逐个改；测试里直接调公开 `pause_task` 的，
   改成调 `_pause_task`（它们测的是内部原语，这是合理的）或改用 `pause_agent`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_runtime_pause_wiring.py tests/unit/test_agent_cascade.py -q`
Expected: PASS

- [ ] **Step 5: 全量回归 + lint**

Run: `python -m pytest -q 2>&1 | tail -12`
Expected: 失败集合与基线逐字相同。

Run: `python -m ruff check --output-format=concise src/ctx_weft/core/runtime.py | grep -v 'RUF00[23]'`
Expected: 空。

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "refactor(api)!: pause_task 内部化，pause_session 改用具名原语

task 不再出现在对外接口上。pause_session 语义一个字未变——非 root agent
被取消的仍是 run 而非 agent（用 _cancel_run_token 不用 cancel_agent），
只是消掉了 runtime 内联遍历 _run_tokens 的写法。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 17: `cancel_session` 退成三步

**Files:**
- Modify: `src/ctx_weft/core/runtime.py:724-770`
- Test: `tests/unit/test_cancel_end_to_end.py`（追加）

**Interfaces:**
- Consumes: 无
- Produces: `cancel_session` 行为不变，实现从五段收成三步

**与 `pause_session` 的差别在于终局意图**：会话取消要的就是全部 agent 进 `terminated`，
`cancel_agent` 正是唯一的 agent 终态入口，这里用它是**语义相符**而非语义变更。

- [ ] **Step 1: 写失败测试**

追加到 `tests/unit/test_cancel_end_to_end.py`：

```python
# ── 2026-09-04 spec §7.2：cancel_session 收成三步 ──────────────────────────


async def test_cancel_session_terminates_every_agent():
    """回归护栏：行为不变。改动前就该绿。"""
    rt = ...
    session_id, root_id, child_id = ...    # root + 子 agent 都在跑

    await rt.cancel_session(session_id)

    for aid in (root_id, child_id):
        assert rt.get_agent(aid).status == "terminated" or rt.record_gone(aid)


async def test_cancel_session_no_longer_walks_run_tokens_itself():
    """结构性守卫：那圈自己遍历 _run_tokens 拍 cancel 的代码应当消失。

    cancel_agent 对 running 目标内部就会调 _cancel_run_token，覆盖同一批在途 run。
    """
    import inspect
    from ctx_weft.core.runtime import CtxWeftRuntime
    src = inspect.getsource(CtxWeftRuntime.cancel_session)
    assert "tokens.cancel.cancel()" not in src
```

> `record_gone` 是伪代码——实施时按实际写：`cancel_session` 对已空闲会话会走
> `_release_session`，届时 `get_agent` 会抛 `AgentNotFound`。用
> `pytest.raises(AgentNotFound)` 或先断言 `AgentTerminated` 事件发出过。
> **先读该文件已有的 cancel 断言手法再定**。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_cancel_end_to_end.py -q -k "no_longer_walks"`
Expected: FAIL — 那段代码还在。

- [ ] **Step 3: 实现**

`cancel_session` 函数体收成：

```python
        # ① 未决的 ask_user 一并终局，且**先于**下面 cancel_all 触发的会话终态——
        # 与熔断 trip 序列同一条纪律（HITL 终局须先于会话终态）。不终局的代价在重启后：
        # rebuild_hitl 按「有 HitlOpened 无终局事件」折 pending，会把已取消会话的
        # 提问当未决恢复出来（总账 A10）。
        await self._cancel_session_hitl(session_id, message=CancelReason.USER_CANCEL)

        # ② 清队列。
        if task_manager is not None:
            await task_manager.cancel_all(reason=CancelReason.USER_CANCEL)

        # ③ 逐个 agent 终态化。cancel_agent 是唯一的 agent 终态入口，它对 running
        # 目标内部会调 _cancel_run_token——在途 run 的协作取消由它覆盖，runtime 不再
        # 自己遍历 _run_tokens（2026-09-04 spec §7.2）。
        #
        # 必须在 `_release_session` **之前**：那一步会把 agent record 从 registry 摘掉，
        # 届时 cancel_agent 查无此 agent，只能静默跳过、发不出 AgentTerminated。
        for aid in list(self._agent_lifecycle_manager.agent_ids_of_session(session_id)):
            await self.cancel_agent(aid, reason="session_canceled")

        if idle:
            # 已暂停/中断（无在跑 task）的会话被取消：cancel_all 不经 _fire_session_done，
            # _on_done 不会触发，故显式回收 runtime 侧 per-session 状态（含较重的
            # TaskManager），避免滞留。
            self._release_session(session_id)
        return True
```

删掉的是原来那段 `for tokens in per.values(): tokens.cancel.cancel()`，以及 R23 那段
解释「为什么要在既有机制之外额外调 cancel_agent」的补丁注释——现在 `cancel_agent`
就是主路径，不需要解释它为什么是个补丁。

`per` 与 `idle` 的计算保留（`idle` 判定必须在 `cancel_all` 之前做）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_cancel_end_to_end.py tests/unit/test_cancel_session_hitl.py tests/unit/test_cancel_closure.py tests/unit/test_agent_cascade.py -q`
Expected: PASS

- [ ] **Step 5: 全量回归 + lint**

Run: `python -m pytest -q 2>&1 | tail -12`
Expected: 失败集合与基线逐字相同。

Run: `python -m ruff check --output-format=concise src/ctx_weft/core/runtime.py | grep -v 'RUF00[23]'`
Expected: 空。

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "refactor(api): cancel_session 收成三步，执行部分建在 cancel_agent 上

删掉自己遍历 _run_tokens 拍 cancel 的那一圈（cancel_agent 内部已覆盖同一批
在途 run），R23 那段解释补丁的注释随之消失——cancel_agent 现在是主路径。
两条顺序纪律不变：HITL 终局先于一切副作用；cancel_agent 先于 _release_session。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 18: SDK 导出面补全

**Files:**
- Modify: `src/ctx_weft/__init__.py`
- Test: `tests/unit/test_public_exports.py`（新建）

**Interfaces:**
- Consumes: 前面全部任务定义的类型
- Produces: 从 `ctx_weft` 顶层可导入 agent-centric 的全部对外类型

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_public_exports.py
"""SDK 公开面（2026-09-04 spec §10）。

改动前只导出 CtxWeftRuntime / RunHandle / SessionStartParams，agent-centric 的
类型一个都不在顶层，host 必须深挖 ctx_weft.protocols.*。
"""

from __future__ import annotations

import ctx_weft


def test_agent_centric_types_are_exported():
    for name in (
        "CtxWeftRuntime", "TurnHandle", "SessionStartParams",
        "AgentSummary", "AgentDetail", "CompactReceipt",
        "HitlReply", "HitlRequestView",
        "AgentNotFound", "AgentNotRunningError",
    ):
        assert hasattr(ctx_weft, name), f"{name} 不在顶层导出面上"
        assert name in ctx_weft.__all__, f"{name} 不在 __all__ 里"


def test_run_handle_is_gone():
    """不留 shim（spec §1）。"""
    assert not hasattr(ctx_weft, "RunHandle")


def test_all_entries_are_importable():
    """__all__ 里不能有写错的名字。"""
    for name in ctx_weft.__all__:
        assert hasattr(ctx_weft, name), f"__all__ 里的 {name} 实际不存在"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_public_exports.py -q`
Expected: FAIL — `AgentSummary 不在顶层导出面上`

- [ ] **Step 3: 实现**

```python
"""ctx-weft: Agent Runtime SDK."""

from ctx_weft.core.models.errors import AgentNotFound, AgentNotRunningError
from ctx_weft.core.models.task import (
    CompactTaskSettings,
    MetadataFillerTaskSettings,
    NormalTaskSettings,
    TaskSettings,
)
from ctx_weft.core.registry import ProviderRegistry
from ctx_weft.core.runtime import CtxWeftRuntime, SessionStartParams, TurnHandle
from ctx_weft.protocols.agent import AgentDetail, AgentSummary, CompactReceipt
from ctx_weft.protocols.events import EventStore
from ctx_weft.protocols.hitl import HitlReply, HitlRequestView
from ctx_weft.providers.events import InMemoryEventStore

__all__ = [
    # 运行时与入参
    "CtxWeftRuntime",
    "ProviderRegistry",
    "SessionStartParams",
    "TurnHandle",
    # agent 发现与回执
    "AgentDetail",
    "AgentSummary",
    "CompactReceipt",
    # HITL
    "HitlReply",
    "HitlRequestView",
    # 错误
    "AgentNotFound",
    "AgentNotRunningError",
    # 事件
    "EventStore",
    "InMemoryEventStore",
    # task 配置
    "TaskSettings",
    "NormalTaskSettings",
    "CompactTaskSettings",
    "MetadataFillerTaskSettings",
]
```

**先核实 `AgentNotRunningError` 的真实位置**：`grep -rn "class AgentNotRunningError" src/`。
若它不在 `core/models/errors.py`，按实际路径导入。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_public_exports.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: 全量回归 + lint**

Run: `python -m pytest -q 2>&1 | tail -12`
Expected: 失败集合与基线逐字相同。

Run: `python -m ruff check --output-format=concise src/ctx_weft/__init__.py tests/unit/test_public_exports.py | grep -v 'RUF00[23]'`
Expected: 空。

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "feat(sdk): 顶层导出面补全 agent-centric 类型

AgentSummary / AgentDetail / CompactReceipt / HitlReply / HitlRequestView /
AgentNotFound / AgentNotRunningError 此前一个都不在 ctx_weft 顶层，host 必须
深挖 protocols.*。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 19: 收口自查

**Files:**
- Modify: `docs/superpowers/specs/2026-09-03-agent-centric-interaction-design.md`（修订 §8）
- Test: 无新增

**Interfaces:**
- Consumes: 全部前置任务
- Produces: 一份对照 spec §2 全表的核对记录

- [ ] **Step 1: 对照 spec §2 全表逐行核对**

打开 `docs/superpowers/specs/2026-09-04-runtime-agent-centric-surface-design.md` §2，
对每一行跑一次验证：

```bash
# 应当存在
python -c "
from ctx_weft.core.runtime import CtxWeftRuntime as R
for n in ('start_session','send_message','run_single_task','cancel_agent','pause_agent',
          'resume_agent','set_agent_llm','compact_agent','pause_session','cancel_session',
          'set_session_llm','list_agents','get_agent','list_pending_hitl','reply_to_hitl',
          'recover_agent','recover','rebuild_hitl','rebuild_all_pending_hitl',
          'rebuild_agent','rebuild_all_agents','_pause_task'):
    assert hasattr(R, n), n
print('present: ok')
"

# 应当不存在
python -c "
from ctx_weft.core.runtime import CtxWeftRuntime as R
for n in ('compact_session','recover_session','session_status_after_recover',
          '_derive_paused_status','pause_task','_announce_queue_state_as_tm_proxy'):
    assert not hasattr(R, n), n
print('absent: ok')
"
```

Expected: 两条都打印 `ok`。

- [ ] **Step 2: 残留符号复核**

```bash
grep -rn "RunHandle\|default_run_id\|announce_queue_state\|paused_status_for" src/ tests/
grep -rn "_agents\[\|_agents.get(" src/ctx_weft/core/runtime.py
```

Expected: 全部无输出。

- [ ] **Step 3: 修订上游 spec §8**

在 `docs/superpowers/specs/2026-09-03-agent-centric-interaction-design.md` 的
`## 8. Session 级 API 的定位` 一节末尾追加：

```markdown
> **2026-09-04 修订**：本节「不新增独立的 session 级执行逻辑」这句对 `pause_session`
> 不成立且不应成立——它的语义（弃排队 + 只留 root 那一轮当续跑点）与 `pause_agent`
> 相反，不是它的广播。见 `docs/superpowers/specs/2026-09-04-runtime-agent-centric-surface-design.md`
> §7.1。`cancel_session` / `set_session_llm` 仍按本节描述，执行部分建在 agent 级接口上。
```

- [ ] **Step 4: 全量回归**

Run: `python -m pytest -q 2>&1 | tail -15`
Expected: 失败集合与 Task 0 基线**逐个 id 相同**，通过数 **≥** 基线记录的 K。

若通过数少于基线：**停下来**，用 `git log --oneline` 找出是哪个 Task 引入的回归，
不要在这一步「顺手补一下」。

- [ ] **Step 5: lint 全量**

Run: `python -m ruff check --output-format=concise src tests | grep -v 'RUF00[23]'`
Expected: 与改动前相比不新增（改动前的基线可用 `git stash` 后跑一次对比）。

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "docs(spec): 修订 09-03 spec §8 关于 pause_session 的定位

pause_session 的语义与 pause_agent 相反，不是它的广播——原文那句
「不新增独立的 session 级执行逻辑」对它不成立。cancel_session /
set_session_llm 仍按原文。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## 自查（计划作者已跑过，执行者不必重复）

**1. Spec 覆盖**

| spec 节 | 覆盖它的 Task |
|---|---|
| §2 目标接口全表 | Task 19 逐行核对 |
| §3 TurnHandle | Task 5（类型）、Task 10（send_message 返回它） |
| §4 run_id 一轮一个 | Task 6（删 `_default_run_id`）、Task 7（A4）、Task 8（C5） |
| §5.1 EventFilter.agent_id | Task 1 |
| §5.2 HITL delivery + agent 过滤 | Task 2 |
| §5.3 record_of 消穿透 | Task 3 |
| §5.4 created_at | Task 4 |
| §6.1 换轴边界（EventStore 不动） | Task 15（只改语义层，`_recover_session_locked` 保留原名） |
| §6.2 recover_agent / 报告单位 | Task 11（计数）、Task 15（改名） |
| §6.3 冷启动装填 | Task 11 |
| §6.4 TASK_QUEUE_* 停发 + AGENT_* 广播 | Task 11（广播）、Task 12（停发） |
| §6.5 删 session_status_after_recover | Task 14 |
| §7.1 pause_session | Task 16 |
| §7.2 cancel_session | Task 17 |
| §7.3 compact_agent + CompactReceipt | Task 9 |
| §7.4 set_session_llm 不变 | 无需 Task |
| §8 寻址口径与封装 | Task 3（封装）、Task 4（list_agents）、Task 16（pause_task） |
| §9 事件变更 | Task 12（L 档）、Task 7/8（新增配对发射） |
| §10 破坏性变更 + 导出面 | Task 18 |
| §11 组件职责对照 | 全部 Task 的结果 |
| §12 遗留细节 | Task 0（批次二交接）、Task 5（`TaskFinalized` 计入 `wait_for_finish`）、Task 13（`rebuild_agent` 复用 `_load_agents_of`）、Task 11（广播归属 ALM）、Task 3（`AgentRecordView` 不与 `AgentDetail` 合并，附理由） |

无遗漏。

**2. 类型一致性**

- `TurnHandle` 的四个身份字段在 Task 5 定义、Task 10 构造、Task 19 核对——名字一致。
- `AgentRecordView` 在 Task 3 定义（8 字段），Task 4 加 `created_at`（9 字段），
  Task 9/10/11/15 只读它的 `session_id` / `template_id` / `current_task_id` / `status`。
- `CompactReceipt` 在 Task 9 定义并只在那里构造。
- `_load_agents_of` 在 Task 11 定义，Task 13 复用——签名 `(session_id, *, tenant_id) -> int` 一致。
- `record_of` 全程返回 `AgentRecordView | None`，五个调用点都判 `None`。

**3. 已知的执行期不确定点**（不是占位符，是必须现场读代码确定的事）

这几处计划里写了「先读某文件照抄」而不是给死代码，因为照抄现有手法比我凭空写一份更可靠：

- Task 7 / 8 的 `RunStarted`/`RunFinished` 发射写法 → 照 `background_observe.py` /
  `recognize_intent.py` 各自已有的 `make_event` 调用形状。
- Task 11 / 13 / 15 的测试夹具 → 照 `tests/unit/test_recover_routing.py`。
- Task 11 的 `_rebuild_view` 与 `fallback` 取法 → 照 `_recover_session_locked` 现有那段抽出来。
- Task 2 的 `HitlRegistry.open` 调用形状 → 照 `core/hitl/registry.py:128` 的真实签名。
- Task 17 的「agent 已被 release 后怎么断言终态」 → 照 `test_cancel_end_to_end.py` 已有手法。
