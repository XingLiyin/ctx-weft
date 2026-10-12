# core/orchestrator 解耦重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `src/ctx_weft/core/orchestrator`（13 文件 / 4389 行）从「三样不相干东西共用一个包名 + 一个 1575 行 god object」还原成「调度内核 + 生命周期注册表」两件事，且**零行为变更**。

**Architecture:** 三步走。① 机械收口（重复字面量、重复代码块、私有穿透）；② 按消费者拆包——`capability_*` 四件套的唯一消费者是 `core/loop/`，搬去 `core/capabilities/`；③ 从 `TaskManager` 剥出两块与调度无关的策略（熔断器、reopen prompt 改写），沿用仓内既有的 `task_disposition.py` 范式：**纯函数算「该做什么」，TaskManager 只负责「执行 + 发事件」**。

**Tech Stack:** Python 3.11 / dataclasses / asyncio / pytest（`asyncio_mode = "auto"`）/ ruff（line-length 100）

**Spec:** 无独立 spec。诊断结论内联于本文档 §诊断依据；每条都在计划中给出了确凿的文件:行号证据。

---

## Global Constraints

以下约束适用于**每一个** Task，不再逐条重复：

1. **零行为变更。** 本次重构不修 bug、不改语义、不调事件顺序、不动 payload 字段。任何一处「顺手改好」都必须退回，另开工单。判据是全量测试逐条绿。
2. **不留 shim。** 搬走的模块不在原位置保留转发。旧路径一次改干净（`src` + `tests` 全量）。
3. **验证命令固定为** `python -m pytest -q`（`pyproject.toml:37` `testpaths = ["tests"]`）。
   ⚠️ **基线不是全绿的**——2026-09-03 在 `feat/multimodal` 上实测有 **8 个先行失败**（清单见 Task 0）。
   因此判据不是「全绿」，而是「**失败集合逐个 id 不变，且通过数只增不减**」。任何一个新出现的红都必须
   当场停下回退，绝不允许「顺手修一下」或「这个本来就不稳」。
4. **风格约束**：ruff `line-length = 100`（`pyproject.toml:40-46`）。
   ⚠️ **全仓 ruff 有 ~24000 条既存告警，几乎全是 `RUF002`/`RUF003`**——它在抱怨中文注释里的
   全角标点。所以判据**不是**「无 error」，而是「**除 RUF002/RUF003 外不新增**」：
   ```bash
   python -m ruff check --output-format=concise <改过的文件> | grep -v 'RUF00[23]'
   ```
   Expected: 空。特别要盯 `F401`（搬走符号后残留的未用 import）。
5. **注释纪律**：搬动代码时**原样搬运注释**，不删不改。注释治理集中在 Task 7，与结构改动隔离，让每个 diff 都只有一种噪音。
6. **提交粒度**：每个 Task 一次 commit，commit message 用中文，格式沿用仓内既有风格（`refactor: ...` / `refactor(orchestrator): ...`）。commit 尾部附：
   ```
   Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
   ```
7. **不动的东西**（已复核，有书面理由，本次明确不碰）：
   - `core/errors.py:179` 函数内 import `task_disposition.RunOutcome`——该处 docstring 已写明「`task_disposition` 是纯 stdlib、不引任何 ctx_weft 模块，此方向无环」，是有意决策，不是疏漏。
   - `task_disposition.py` / `agent_state.py` / `task_queue.py` / `template_lookup.py` / `task_runner.py` 的内部实现——它们已经是干净的叶子模块。

---

## 诊断依据（重构的事实基础）

| # | 证据 | 处理 |
|---|---|---|
| A | `core/loop/steps/_capabilities.py:11-13`、`core/loop/capability_gateway.py:38,45`、`core/loop/steps/{act,act_guidance,observe,prepare,background_observe}.py`、`core/assembler/composer.py:110` 全部从 `core.orchestrator` 引 capability 代码；而 orchestrator 的调度半边**零引用**这四个模块 | Task 3 搬包 |
| B | `TaskManager` 1575 行、13 个 `set_*` 注入点；`_trip_failure_threshold` 单方法 144 行 8 步；`reopen_*` + 两个模块级 helper ~200 行 | Task 4/5/6 |
| C | `task_manager.py:248` `self._queue._completed.add(tid)` 直接写 `TaskQueue` 私有集合 | Task 1 |
| D | `task_manager.py:238` 局部 `_TERMINAL = {...}` 与 `:50` `_TERMINAL_STATUSES` 重复；`:1051 :1271 :1334` + `session_registry.py:250` 另有 4 处内联同一三元组 | Task 1 |
| E | 槽位归还三连（`_running_tasks.discard` + `_running_agents.pop` + `_queue.unmark_running`）逐字重复 5 次（`:554 :567 :777 :800 :837`），第 6 处 `:861` 是变体 | Task 1 |
| F | `runtime.py:66` 从 `task_manager` 导入私有 `_task_payload` | Task 1 |
| G | `protocols/events.py:213` `ORCHESTRATOR_SESSION_MANAGER = "orchestrator.session_registry"`——上个 commit 改了类名和值，漏了枚举成员名 | Task 1 |
| H | `session_registry.py:10-11` 与 `:14-15` 各有一对拆开的同模块 import | Task 1 |
| I | 无 run 语境的 `Event(...)` 封套重复 **9 次跨 3 包**：orchestrator 7（ALM 5 + TM 1 + SR 1）、`hitl/service.py:194`、`runtime.py:2792`；`EVENT_TYPES` 白名单校验另有 3 份（`loop/driver.py` 的 `make_event` + 两个 `_emit`） | Task 2 |
| J | `task_manager.py` 散文占比 38.5%（259 注释行 + 347 docstring 行 / 1576 行），其中 36 行是「Task N 之前是 X」「见 task-9-brief.md」「总账 A5」式变更史 | Task 7 |

---

## 重构后的全景（目标状态）

执行者应先读完本节再动手——它是所有 Task 共同要抵达的地方。

### 1. 依赖方向：本次真正修好的东西

**之前**（`core.hitl` 是干净的，但 capability 的归属是错的）：

```
                      ┌─────────────┐
                      │   runtime   │
                      └──────┬──────┘
                             ↓
        ┌────────────────────┴────────────────────┐
        ↓                                         ↓
  ┌───────────┐   引 capability 四件套      ┌──────────────┐
  │ core.loop │ ──────────────────────────→ │ orchestrator │
  └─────┬─────┘                             └──────────────┘
        ↓                                    ↑ 调度 + 生命周期
  ┌───────────┐                              + capability 四件套（1178 行，
  │ core.hitl │                                 调度半边零引用）
  └───────────┘
```

`core.loop` 为了拿 `CapabilityCache` / `ControlCapabilityProvider` /
`SkillExecutorCapabilityProvider` / `CapabilityResolver` 而依赖 `orchestrator`——
但它要的东西和调度毫无关系。

**之后**：

```
                      ┌─────────────┐
                      │   runtime   │
                      └──────┬──────┘
                             ↓
        ┌────────────────────┼────────────────────┐
        ↓                    ↓                    ↓
  ┌───────────┐      ┌──────────────┐    ┌──────────────────┐
  │ core.loop │ ────→│ capabilities │    │   orchestrator   │
  └─────┬─────┘      └──────────────┘    │ 调度 + 生命周期    │
        ↓                                 └──────────────────┘
  ┌───────────┐
  │ core.hitl │
  └───────────┘
        ↓
  ┌──────────────────────────────────────────────────┐
  │  core.events    core.utils    protocols.events    │  ← 叶子，谁都能引
  └──────────────────────────────────────────────────┘
```

两条关键边：

- **`loop → orchestrator` 断了**（Task 3）。`loop` 现在引 `capabilities`，
  与调度再无瓜葛。
- **`core.events` 在最底层**（Task 2）。这是为什么它不能放
  `orchestrator/emitter.py`：`hitl/service.py` 也要用同一份封套，而
  `hitl` 在 `loop` / `runtime` **之下**——放 orchestrator 会造出一条
  `hitl → orchestrator` 的反向边。

### 2. 文件全景

★ = 新文件。行数为**目标值**，允许 ±15%；偏离更多说明切法走样了，停下来对一遍。

```
core/events.py                     ★  ~60   全仓事件封套的唯一构造点（叶子）

core/capabilities/                     ~1200  ← 整体来自 orchestrator，逻辑一行未改
  __init__.py                      ★   ~20
  cache.py                             102   per-session capability 快照
  resolver.py                          101   required → retrieve → 去 forbidden
  control_tools.py                     725   7 个内置控制工具 + provider
  skill_executor.py                    250   list_files / read_file / exec_script

core/orchestrator/                     ~3230
  ── 调度内核 ──────────────────────────────────────────────────
  task_queue.py                        130   LIFO + DAG（+ seed_completed）
  task_disposition.py                  150   结局→处置的纯函数表（+ TERMINAL_TASK_STATUSES）
  task_runner.py                        78   两阶段 runner 协议（assemble / execute）
  task_manager.py                     ~1290  队列驱动 + 派发 + task 状态改写 + 会话信号
  task_reopen.py                   ★  ~150   reopen 的 prompt 改写（纯函数）
  failure_threshold.py             ★  ~130   熔断清场的分类（纯函数）
  hooks.py                         ★   ~60   TaskManagerHooks
  ── 生命周期注册表 ────────────────────────────────────────────
  session_registry.py                 ~340   session 创建 + 成员登记
  agent_state.py                       117   agent 五态机（纯函数）
  agent_lifecycle_manager.py          ~700   agent 身份与配置的唯一住所
  template_lookup.py                    69   cap.id 前缀路由的模板加载
```

**总行数基本持平（约 +100）。** 这是意料之中、也应当如实预期的：抽出来的模块要带
自己的模块 docstring 与类型签名，抵消掉去重省下的量。本次重构买的不是行数，是：

- 最大文件 **-18%**（1575 → ~1290），且它不再兼任四种角色；
- 四块逻辑从「只能造个 TaskManager 间接测」变成**可直接单测的纯函数**；
- 一条错误的包依赖边被切断，一条反向边被提前避免。

### 3. 每个新文件回答什么问题

| 文件 | 它回答的问题 | 为什么是纯函数 |
|---|---|---|
| `core/events.py` | 「一条事件的封套长什么样」 | 无状态；`bus=None` 的 no-op 语义在此统一承担 |
| `failure_threshold.py` | 「熔断时，**谁**进哪一桶」 | 顺序（8 步）留在 TaskManager——那才是契约 |
| `task_reopen.py` | 「重开后 prompt **长什么样**」 | 队列动作留在 TaskManager；这里只算内容 |
| `hooks.py` | 「TaskManager 需要外界给它什么」 | frozen dataclass，整体替换，装不出半接线态 |

这四个的切法是同一条：**纯函数回答「是什么 / 该做什么」，TaskManager 负责「照办 +
发事件」。** 这不是新发明——`task_disposition.disposition_for` 已经这么做了，本次只是
把同一条规则再用三次。

### 4. `TaskManager` 的对外面貌

**之前**：构造完是半成品，13 个 `set_*`，接线顺序是隐含契约。

```python
tm = TaskManager(session_id, event_bus=bus)
tm.set_runner(...);              tm.set_is_current(...)
tm.set_session_registry(...);    tm.set_cancel_pending_hitl(...)
tm.set_cancel_inflight(...);     tm.set_threshold_finalizer(...)
tm.set_cancel_finalizer(...);    tm.set_session(...)
tm.set_session_done_callback(...); tm.set_session_idle_callback(...)
```

**之后**：4 个入口，其中 3 个是真需要多次调用的运行期开关。

```python
tm = TaskManager(session_id, event_bus=bus)
tm.set_runner(runner)             # start / recover 各一次
tm.set_session(session)           # 三处（含 push_task 前的必须调用）
tm.set_hooks(TaskManagerHooks(    # 一次性接线，整体替换
    is_current=..., session_registry=...,
    cancel_pending_hitl=..., cancel_inflight=...,
    threshold_finalizer=..., cancel_finalizer=...,
    on_session_done=..., on_session_idle=...,
))
tm.set_pause_abandon(flag)        # pause 窗口的运行期开关
```

`TaskManager` 之后仍然负责：task 注册表、parent/child DAG 索引、staged spawn 缓冲、
队列驱动与派发、task 状态改写（`apply_run_outcome` 是唯一入口）、会话级聚合信号。
**它仍然是这个包里最大的类**——见 §6。

### 5. 新增的测试杠杆

这四块今天只能通过「先造一个 TaskManager、喂事件、看总线」间接测。之后可以直接测：

| 纯函数 | 直接测什么 | 计划里的测试数 |
|---|---|---|
| `new_event` / `emit_event` | 封套字段、白名单、`bus=None` no-op、校验先于发射 | 7 |
| `plan_threshold_trip` | 在途 root vs 已终态 root、有框 vs 无框、已启动 vs 未启动 | 10 |
| `build_reopen_prompt` | 重复 reopen 不叠加、list base 不别名、upstream 取代直接修订说明 | 8 |
| `append_text_sections` | event 侧与 memory 侧逐分支同构（三种空 base 的收敛） | （含上） |
| `TaskManagerHooks` | 整体替换语义、frozen、旧 setter 已消失 | 6 |
| 收口不变量 | 无内联终态字面量、`seed_completed`、`task_payload` 公开 | 4 |

合计 **+35 个新测试**，全部在计划里给了完整代码。

### 6. 明确**没有**变的（免得预期落空）

1. **`TaskManager` 仍持有 task 注册表 + DAG 索引 + staged 缓冲。**
   `_try_resume_parent` 要在同一临界区里同时读 `_children_of` / `_tasks` / `_queue`，
   拆它得先定清楚锁的边界。这是落点 ~1290 而不是 ~950 的原因。
2. **`runtime.py` 3465 行，除 3 处接线点外一行未动。**
3. **`session_registry` 与 `agent_lifecycle_manager` 的职责边界未重划。**
   两份 docstring 各自写清了「只是登记表」与「身份配置的唯一住所」，暂无冲突证据。
4. **`core/errors.py` → `task_disposition` 的方向依赖保留**（见 Global Constraints §7）。
5. **8 个先行失败仍是 8 个**——本次零行为变更，不修它们。

---

### Task 0: 基线

**Files:**
- 无改动

- [ ] **Step 1: 确认工作区干净**

```bash
git status --porcelain
```

Expected: 空输出。非空则先停下问用户。

- [ ] **Step 2: 记录基线的通过数与失败集合**

```bash
python -m pytest -q --tb=no 2>&1 | tee /tmp/baseline.txt | tail -3
grep "^FAILED" /tmp/baseline.txt | sed 's/ - .*//' | sort > /tmp/baseline-failures.txt
cat /tmp/baseline-failures.txt
```

把末行 `X failed, N passed, ...` 里的 **N 抄到下方占位**。

> 基线通过数 N = ______（执行时填写）

**已知先行失败（2026-09-03 实测于 `feat/multimodal`，与本次重构无关）：**

```
tests/integration/test_compact_flow_e2e.py::test_multiround_retry_accumulates_then_l3_collapses_e2e
tests/integration/test_dispatch_boundary_recap_e2e.py::test_dispatch_boundary_recap_e2e
tests/unit/test_bash_exec_liveness.py::test_bash_exec_streams_and_completes
tests/unit/test_bash_exec_liveness.py::test_bash_exec_idle_timeout_reports_error
tests/unit/test_observe_outcomes.py::test_default_role_prompt_uses_two_fields
tests/unit/test_script_runner.py::test_clean_exit_collects_output
tests/unit/test_script_runner.py::test_idle_timeout_kills_silent_sleeper
tests/unit/test_script_runner.py::test_hard_cap_kills_busy_but_overlong
```

⚠️ **其中 `test_dispatch_boundary_recap_e2e` 已实测为 flaky**（2026-09-04：同一份工作区
连跑两次，一次 PASS 一次 FAIL，中间零改动）。做失败集合比对时**把它从两侧都剔除**：
```bash
grep "^FAILED" /tmp/now.txt | sed 's/ - .*//' | grep -v dispatch_boundary_recap | sort > /tmp/now-failures.txt
```
基线侧同样处理。剩下 7 条应逐条稳定复现。

Step 2 跑出来的集合若与上面这 8 条**不一致**（除去 flaky 的那条），先停下报告——说明环境或分支已变，本计划的安全网前提需要重新确认。

后续每个 Task 结束时的核对方式（而不是肉眼比对）：

```bash
python -m pytest -q --tb=no 2>&1 | tee /tmp/now.txt | tail -3
grep "^FAILED" /tmp/now.txt | sed 's/ - .*//' | sort > /tmp/now-failures.txt
diff /tmp/baseline-failures.txt /tmp/now-failures.txt && echo "失败集合未变 ✓"
```

Expected: `失败集合未变 ✓`。`diff` 有输出即为回归，立刻回退。

> ⚠️ 其中 `test_observe_outcomes.py::test_default_role_prompt_uses_two_fields` 与
> 两个 e2e 看起来**不是**环境问题（另外 5 个是 Windows 上的 subprocess/shell 相关）。
> 它们是既有的真红，但**不在本次范围**——本计划零行为变更，修它们要另开工单。
> 记在这里只是为了让执行者不把它们误当成自己弄坏的。

- [ ] **Step 3: 清掉改名残留的 pycache**

上一个 commit（`badb2f4`）把 `session_manager.py` → `session_registry.py`、`agent_registry.py` → `agent_lifecycle_manager.py`，`__pycache__` 里还留着旧模块的 `.pyc`，会让 `import` 在某些情形下命中幽灵模块。

```bash
find src -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null; true
find tests -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null; true
git status --porcelain
```

Expected: `git status` 仍为空（`__pycache__` 本就在 `.gitignore` 里）。

---

### Task 1: 机械收口

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/task_queue.py`（加 `seed_completed`）
- Modify: `src/ctx_weft/core/orchestrator/task_manager.py`（常量收口 / 槽位 helper / `_task_payload` 改公开）
- Modify: `src/ctx_weft/core/orchestrator/session_registry.py`（常量收口 / import 合并）
- Modify: `src/ctx_weft/protocols/events.py:213`（枚举成员改名）
- Modify: `src/ctx_weft/core/runtime.py`（`_task_payload` → `task_payload`；`EventOrigin` 成员名）
- Test: `tests/unit/test_orchestrator_internals.py`（新建）

**Interfaces:**
- Produces: `TaskQueue.seed_completed(task_ids: Iterable[str]) -> None`
- Produces: `TaskManager._release_slot(task_id: str) -> None`（须已持锁）、`TaskManager._clear_running(task_id: str) -> None`（须已持锁）
- Produces: `ctx_weft.core.orchestrator.task_manager.task_payload`（原 `_task_payload`，签名不变）
- Produces: `EventOrigin.ORCHESTRATOR_SESSION_REGISTRY`（值不变，仍是 `"orchestrator.session_registry"`）

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_orchestrator_internals.py`：

```python
"""Task 1 收口的三条不变量：私有集合不再被穿透、终态常量单一来源、槽位归还单一实现。"""

from ctx_weft.core.orchestrator.task_queue import QueueEntry, TaskQueue
from ctx_weft.core.orchestrator.task_manager import _TERMINAL_STATUSES


def test_seed_completed_marks_ids_done_without_touching_privates():
    q = TaskQueue()
    q.seed_completed(["a", "b"])
    # 已完成的依赖在 push 时即被摘掉
    q.push(QueueEntry(task_id="c", session_id="s", blocked_by={"a", "b"}))
    entry = q.pop()
    assert entry is not None and entry.task_id == "c"


def test_seed_completed_is_idempotent():
    q = TaskQueue()
    q.seed_completed(["a"])
    q.seed_completed(["a"])
    q.push(QueueEntry(task_id="c", session_id="s", blocked_by={"a"}))
    assert q.pop() is not None


def test_no_inline_terminal_triple_outside_the_single_definition():
    """终态三元组只许在 task_disposition.TERMINAL_TASK_STATUSES 定义处出现一次。

    task_manager / session_registry 里应为 **0 处**——它们引常量，不再内联字面量。
    注释行豁免：注释里引用字面量是说明性文字，允许。
    """
    import pathlib
    import re

    pattern = re.compile(r'"FINISHED"\s*,\s*"FAILED"\s*,\s*"CANCELED"')

    def _code_hits(rel: str) -> int:
        src = pathlib.Path(rel).read_text(encoding="utf-8")
        code = "\n".join(
            line for line in src.splitlines() if not line.lstrip().startswith("#")
        )
        return len(pattern.findall(code))

    assert _code_hits("src/ctx_weft/core/orchestrator/task_manager.py") == 0
    assert _code_hits("src/ctx_weft/core/orchestrator/session_registry.py") == 0
    assert _code_hits("src/ctx_weft/core/orchestrator/task_disposition.py") == 1
    assert _TERMINAL_STATUSES == frozenset({"FINISHED", "FAILED", "CANCELED"})


def test_task_payload_is_public():
    """runtime.py 依赖它——跨模块使用的东西不该带下划线。"""
    from ctx_weft.core.orchestrator import task_manager

    assert hasattr(task_manager, "task_payload")
    assert not hasattr(task_manager, "_task_payload")
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/unit/test_orchestrator_internals.py -v
```

Expected: 4 个全 FAIL —— `AttributeError: 'TaskQueue' object has no attribute 'seed_completed'`、终态三元组计数为 4、`task_payload` 不存在。

- [ ] **Step 3: 给 TaskQueue 加 `seed_completed`**

在 `src/ctx_weft/core/orchestrator/task_queue.py` 的 `unmark_completed` 之前插入：

```python
    def seed_completed(self, task_ids: "Iterable[str]") -> None:
        """恢复期批量装填「已终态」集合。

        `TaskManager.restore` 此前直接写 `self._queue._completed`——那是穿透私有。
        与 `mark_complete` 的区别：这里不做「刷新已排队条目的 blocked_by」，因为
        restore 的调用顺序是**先装填、后 push**，push 自己就会摘掉已完成依赖
        （见 `push` 首行 `entry.blocked_by -= self._completed`），无需重复扫描。
        """
        self._completed.update(task_ids)
```

同文件顶部 import 区补上（`collections.abc` 那行已存在 `Callable`）：

```python
from collections.abc import Callable, Iterable
```

- [ ] **Step 4: TaskManager 改用 `seed_completed`**

`task_manager.py:247-248`，把：

```python
        for tid in terminal_ids:
            self._queue._completed.add(tid)
```

替换为：

```python
        self._queue.seed_completed(terminal_ids)
```

- [ ] **Step 5: 终态常量收口（5 处）**

`task_manager.py:238`，删掉局部定义：

```python
        _TERMINAL = {"FINISHED", "FAILED", "CANCELED"}
        parked = parked_task_ids or set()
```
→
```python
        parked = parked_task_ids or set()
```

同方法内 `:249` 的 `if t.status in _TERMINAL:` → `if t.status in _TERMINAL_STATUSES:`

`task_manager.py:1051`（`_trip_failure_threshold` 第 6 步）：

```python
            if t.status in ("FINISHED", "FAILED", "CANCELED"):
```
→
```python
            if t.status in _TERMINAL_STATUSES:
```

`task_manager.py:1269-1272`（`_try_resume_parent`）：

```python
            all_done = bool(siblings) and all(
                (self._tasks[tid].status if tid in self._tasks else "PENDING")
                in ("FINISHED", "FAILED", "CANCELED")
                for tid in siblings
            )
```
→
```python
            all_done = bool(siblings) and all(
                (self._tasks[tid].status if tid in self._tasks else "PENDING")
                in _TERMINAL_STATUSES
                for tid in siblings
            )
```

`task_manager.py:1334`（`resume_task`）：

```python
        if t is None or t.status in ("FINISHED", "FAILED", "CANCELED"):
```
→
```python
        if t is None or t.status in _TERMINAL_STATUSES:
```

`session_registry.py:248-253`——这里不能直接引 `task_manager._TERMINAL_STATUSES`（会在 `session_registry` 已 import `task_manager` 的基础上再多一条私有依赖）。把常量提到 `task_disposition.py`（纯 stdlib 叶子模块，两边都能安全引用）：

在 `task_disposition.py` 的 `__all__` 上方加：

```python
#: 已经坐实的 task 终态。`TaskManager` 与 `SessionRegistry` 共用同一份判据——
#: 此前两处各内联一遍 ("FINISHED", "FAILED", "CANCELED") 字面量。
TERMINAL_TASK_STATUSES: frozenset[str] = frozenset({"FINISHED", "FAILED", "CANCELED"})
```

并把它加进 `__all__`：

```python
__all__ = [
    "TERMINAL_TASK_STATUSES",
    "Disposition", "RunOutcome", "RunOutcomeKind", "disposition_for",
]
```

`task_manager.py:44-50` 改成 re-export（保留原注释，它解释了这条守卫的来历）：

```python
#: 已经坐实的终态。TM 自己写过其中之一（熔断 trip 的 root 判死）之后，run 的结局
#: 不得再把它盖掉——这条守卫此前长在 `_run_loop` 的三个 except 支里，随发射一起搬来。
#: 定义在 `task_disposition`（纯 stdlib 叶子）以便 `SessionRegistry` 共用同一份判据。
_TERMINAL_STATUSES = TERMINAL_TASK_STATUSES
```

`task_manager.py` 的 import 块补上（`:17-21` 那个已有的 `task_disposition` import）：

```python
from ctx_weft.core.orchestrator.task_disposition import (
    TERMINAL_TASK_STATUSES,
    RunOutcome,
    RunOutcomeKind,
    disposition_for,
)
```

`session_registry.py:248-253` 改为：

```python
        unfinished = [
            tid for tid, t in view.tasks.items()
            if t.status not in TERMINAL_TASK_STATUSES
            and (t.settings_raw or {}).get("_type") not in (
                "CompactTaskSettings", "MetadataFillerTaskSettings")
        ]
```

- [ ] **Step 6: import 合并（session_registry.py:9-17）**

把：

```python
from ctx_weft.core.errors import UnfinishedTasksError
from ctx_weft.protocols.events import EventBus
from ctx_weft.protocols.events import EVENT_TYPES, Event, EventOrigin, EventType
from ctx_weft.core.orchestrator.agent_lifecycle_manager import AgentLifecycleManager, ModelChoice
from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.state.models import Session, Task
from ctx_weft.core.state.models import NormalTaskSettings
from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols.context import ProviderContext
```

替换为（顺序按 ruff isort 规则：同一包内字母序）：

```python
from ctx_weft.core.errors import UnfinishedTasksError
from ctx_weft.core.orchestrator.agent_lifecycle_manager import AgentLifecycleManager, ModelChoice
from ctx_weft.core.orchestrator.task_disposition import TERMINAL_TASK_STATUSES
from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.state.models import NormalTaskSettings, Session, Task
from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols.context import ProviderContext
from ctx_weft.protocols.events import EVENT_TYPES, Event, EventBus, EventOrigin, EventType
```

- [ ] **Step 7: 槽位归还 helper（消掉 5 份重复）**

在 `task_manager.py` 的 `_agent_id_of` 方法（`:1118`）**之前**插入：

```python
    def _clear_running(self, task_id: str) -> None:
        """从「在跑」登记里摘掉这个 task。**调用方须已持 `self._lock`。**"""
        self._running_tasks.discard(task_id)
        self._running_agents.pop(task_id, None)

    def _release_slot(self, task_id: str) -> None:
        """归还一个派发槽位：清在跑登记 + 解除队列的 running 标记。

        **调用方须已持 `self._lock`。** 五个非终态出口（park / 重排 / 装配失败的
        终态守卫 / 装配失败的重试 / INTERRUPTED 挂起）逐字共用这三行——不摘干净
        会永久少一个并发槽位（见 `_handle_task_failure` 终态守卫分支的注释）。
        终态出口（`on_task_finished`）不走这里：它的队列侧动作是
        `mark_complete` / `mark_failed`（两者内部已 `_running.discard`），
        只需 `_clear_running`。
        """
        self._clear_running(task_id)
        self._queue.unmark_running(task_id)
```

然后把这 5 处：

```python
            async with self._lock:
                self._running_tasks.discard(task_id)
                self._running_agents.pop(task_id, None)
                self._queue.unmark_running(task_id)
```

分别替换为 `async with self._lock: self._release_slot(task_id)`（保持原缩进与原注释）：

- `:554-557`（`_settle` 的 `_PARKED_STATUSES` 出口）→
  ```python
            async with self._lock:
                self._release_slot(task_id)
  ```
- `:567-571`（`_settle` 的 `PENDING` 出口，注意它多一行 `push`）→
  ```python
            async with self._lock:
                self._release_slot(task_id)
                self._queue.push(QueueEntry(task_id=task_id, session_id=self._session_id))
  ```
- `:777-780`（`_handle_task_failure` 终态守卫分支）→
  ```python
            async with self._lock:
                self._release_slot(task_id)
  ```
- `:800-804`（`_handle_task_failure` 重试分支，注意 `unmark_running` 行尾原有注释「清除 queue._running，使 pop() 能再次调度」——该注释已被 `_release_slot` 的 docstring 覆盖，删掉行尾注释）→
  ```python
            async with self._lock:
                self._release_slot(task_id)
                entry = QueueEntry(task_id=task_id, session_id=self._session_id)
                self._queue.push(entry)
  ```
- `:836-840`（`_suspend_task_interrupted`，缩进少一级）→
  ```python
        async with self._lock:
            self._release_slot(task_id)
  ```

`on_task_finished`（`:861-868`）改为：

```python
        async with self._lock:
            self._clear_running(task_id)
            if status == "FAILED":
                self._queue.mark_failed(task_id)
            else:
                self._queue.mark_complete(task_id)
```

- [ ] **Step 8: `_task_payload` → `task_payload`**

`task_manager.py:1527` 的 `def _task_payload(` → `def task_payload(`；同文件 `:333` 的调用点 `payload=_task_payload(task, ...)` → `payload=task_payload(task, ...)`。

`runtime.py:66`：

```python
from ctx_weft.core.orchestrator.task_manager import TaskManager, _task_payload
```
→
```python
from ctx_weft.core.orchestrator.task_manager import TaskManager, task_payload
```

再改 runtime.py 内的调用点：

```bash
grep -n "_task_payload" src/ctx_weft/core/runtime.py
```
把找到的每一处 `_task_payload(` 改成 `task_payload(`。

同样检查测试：

```bash
grep -rn "_task_payload" tests/
```
命中的一并改。

- [ ] **Step 9: `EventOrigin` 成员改名**

`src/ctx_weft/protocols/events.py:213`：

```python
    ORCHESTRATOR_SESSION_MANAGER = "orchestrator.session_registry"
```
→
```python
    ORCHESTRATOR_SESSION_REGISTRY = "orchestrator.session_registry"
```

全仓替换引用（**值不变**，所以事件流和投影完全不受影响）：

```bash
grep -rln "ORCHESTRATOR_SESSION_MANAGER" --include=*.py src tests \
  | xargs sed -i 's/ORCHESTRATOR_SESSION_MANAGER/ORCHESTRATOR_SESSION_REGISTRY/g'
grep -rn "ORCHESTRATOR_SESSION_MANAGER" --include=*.py src tests
```

Expected: 最后一条 grep 无输出。

- [ ] **Step 10: 跑新测试**

```bash
python -m pytest tests/unit/test_orchestrator_internals.py -v
```
Expected: 4 passed

- [ ] **Step 11: 跑全量 + lint**

```bash
python -m pytest -q --tb=no 2>&1 | tee /tmp/now.txt | tail -3
grep "^FAILED" /tmp/now.txt | sed 's/ - .*//' | sort > /tmp/now-failures.txt
diff /tmp/baseline-failures.txt /tmp/now-failures.txt && echo "失败集合未变 ✓"
python -m ruff check src tests
```
Expected: `N+4 passed` **且** `失败集合未变 ✓`（N 为 Task 0 基线）；ruff 无 error。

- [ ] **Step 12: 提交**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor(orchestrator): 机械收口——私有穿透、重复字面量、重复槽位归还

- TaskQueue.seed_completed 取代 TaskManager 直接写 _queue._completed
- 终态三元组收进 task_disposition.TERMINAL_TASK_STATUSES，5 处内联收口
- _release_slot/_clear_running 取代 6 份逐字重复的在跑登记清理
- _task_payload → task_payload（runtime.py 跨模块使用，不该带下划线）
- EventOrigin.ORCHESTRATOR_SESSION_MANAGER → ..._SESSION_REGISTRY（值不变）
- session_registry.py 合并两对拆开的同模块 import

零行为变更。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: 共享事件封套 —— `core/events.py`

**Files:**
- Create: `src/ctx_weft/core/events.py`
- Modify: `src/ctx_weft/core/orchestrator/task_manager.py`（`_emit` 改为薄封装）
- Modify: `src/ctx_weft/core/orchestrator/session_registry.py`（同上）
- Modify: `src/ctx_weft/core/orchestrator/agent_lifecycle_manager.py`（5 处内联 `Event(...)`）
- Modify: `src/ctx_weft/core/hitl/service.py:194-207`（`_emit` 改为薄封装）
- Modify: `src/ctx_weft/core/runtime.py:2792-2801`（恢复信号的内联 `Event(...)`）
- Modify: `src/ctx_weft/core/loop/driver.py:160-187`（`make_event` 改为委托，白名单校验去重）
- Test: `tests/unit/test_core_events.py`（新建）

**Interfaces:**
- Produces:
  ```python
  def new_event(
      event_type: str,
      *,
      session_id: str,
      tenant_id: str,
      origin: str,
      run_id: str | None = None,
      sequence: int = 0,
      task_id: str | None = None,
      agent_id: str | None = None,
      payload: dict | None = None,
      metadata: dict | None = None,
      timestamp: "datetime | None" = None,
      causation_id: str | None = None,
  ) -> Event

  async def emit_event(bus: "EventBus | None", event_type: str, **kwargs) -> None
  ```
- Consumes: 无（叶子模块，只引 `core.utils` + `protocols.events`）

#### 为什么是 `core/events.py`，不是 `core/orchestrator/emitter.py`

「无 run 语境」的封套（`run_id=None, sequence=0`）在全仓有 **9 处**逐字重复，跨 **3 个包**：

```
core/orchestrator/agent_lifecycle_manager.py  :223 :488 :559 :578 :613
core/orchestrator/task_manager.py             :1150
core/orchestrator/session_registry.py         :344
core/hitl/service.py                          :197
core/runtime.py                               :2793
```

`core/hitl/` 今天**只**依赖 `protocols` + `core.utils` + 自己的兄弟模块——把 helper 放进
`orchestrator/` 会新增一条 `hitl → orchestrator` 边，而 `loop` 和 `runtime` 都在 hitl 之上，
方向是反的。`core/events.py` 作为叶子模块（依赖面与 `core/utils.py` 同级）则谁都能引，无环。

**顺带收掉第三份重复：** `EVENT_TYPES` 白名单校验今天有三份——`loop/driver.py` 的
`make_event`、`TaskManager._emit`、`SessionRegistry._emit`。`make_event` 改为委托
`new_event` 之后只剩一份。`make_event` **本身保留**（它做 LoopState 字段抽取 +
`sequence_counter` 自增，是 run 域的真实职责），只是不再自己拼 `Event`。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_core_events.py`：

```python
"""core.events：全仓事件封套的唯一构造点。"""

import pytest

from ctx_weft.core.events import emit_event, new_event
from ctx_weft.protocols.events import EventOrigin, EventType


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, ev) -> None:
        self.events.append(ev)


def test_no_run_context_defaults():
    """默认就是无 run 语境：run_id=None / sequence=0。"""
    ev = new_event(
        EventType.TASK_CREATED,
        session_id="ses_1", tenant_id="acme",
        origin=EventOrigin.ORCHESTRATOR_TASK_MANAGER,
    )
    assert ev.run_id is None and ev.sequence == 0
    assert ev.payload == {} and ev.metadata == {}
    assert ev.id.startswith("evt")


def test_run_scoped_fields_passthrough():
    """make_event 委托本函数时传 run_id/sequence。"""
    ev = new_event(
        EventType.STEP_STARTED,
        session_id="s", tenant_id="default", origin=EventOrigin.LOOP_DRIVER,
        run_id="run_1", sequence=7, task_id="tsk_1", agent_id="agt_1",
        metadata={"m": 1}, causation_id="cau_1",
    )
    assert ev.run_id == "run_1" and ev.sequence == 7
    assert ev.task_id == "tsk_1" and ev.agent_id == "agt_1"
    assert ev.metadata == {"m": 1} and ev.causation_id == "cau_1"


def test_unknown_event_type_rejected():
    """白名单校验的唯一一份（此前 make_event / TM._emit / SR._emit 各一份）。"""
    with pytest.raises(ValueError, match="Unknown event type"):
        new_event("NotARealEvent", session_id="s", tenant_id="d", origin="x")


def test_explicit_timestamp_wins():
    from datetime import datetime, timezone

    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ev = new_event(EventType.SESSION_CREATED, session_id="s", tenant_id="d",
                   origin=EventOrigin.ORCHESTRATOR_SESSION_REGISTRY, timestamp=ts)
    assert ev.timestamp == ts


async def test_emit_event_puts_it_on_the_bus():
    bus = _Bus()
    await emit_event(bus, EventType.TASK_RESUMED, session_id="s", tenant_id="d",
                     origin=EventOrigin.ORCHESTRATOR_TASK_MANAGER, task_id="t")
    (ev,) = bus.events
    assert ev.type == EventType.TASK_RESUMED and ev.task_id == "t"


async def test_emit_event_none_bus_is_noop():
    """TaskManager 的既有语义：event_bus 未注入时静默跳过（大量单测依赖）。"""
    await emit_event(None, EventType.TASK_CREATED, session_id="s",
                     tenant_id="d", origin=EventOrigin.ORCHESTRATOR_TASK_MANAGER)


async def test_emit_event_validates_before_touching_bus():
    """坏类型不该先落一半再报错。"""
    bus = _Bus()
    with pytest.raises(ValueError):
        await emit_event(bus, "Nope", session_id="s", tenant_id="d", origin="x")
    assert bus.events == []
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/unit/test_core_events.py -v
```
Expected: 全 FAIL，`ModuleNotFoundError: No module named 'ctx_weft.core.events'`

- [ ] **Step 3: 写 core/events.py**

```python
"""事件封套的唯一构造点。**叶子模块**：只引 `core.utils` + `protocols.events`。

改造前，「无 run 语境」的封套（`run_id=None, sequence=0`）在全仓有 9 处逐字重复，
跨 3 个包：`core.orchestrator`（7）、`core.hitl`（1）、`core.runtime`（1）；
`EVENT_TYPES` 白名单校验另有 3 份（`loop.driver.make_event` + 两个 `_emit`）。

放在 `core/` 顶层而不是任何一个子包里，是因为 `core.hitl` 今天只依赖
`protocols` + `core.utils` + 自己的兄弟模块——把 helper 塞进 `core.orchestrator`
会新增一条 `hitl → orchestrator` 边，而 `loop` 和 `runtime` 都在 hitl 之上，
方向是反的。本模块的依赖面与 `core/utils.py` 同级，谁都能引，无环。

run 级事件仍走 `core.loop.driver.make_event`——它做 LoopState 字段抽取 +
`sequence_counter` 自增，那是 run 域的真实职责；它只是不再自己拼 `Event`。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols.events import EVENT_TYPES, Event

if TYPE_CHECKING:
    from datetime import datetime

    from ctx_weft.protocols.events import EventBus

__all__ = ["emit_event", "new_event"]


def new_event(
    event_type: str,
    *,
    session_id: str,
    tenant_id: str,
    origin: str,
    run_id: str | None = None,
    sequence: int = 0,
    task_id: str | None = None,
    agent_id: str | None = None,
    payload: dict | None = None,
    metadata: dict | None = None,
    timestamp: "datetime | None" = None,
    causation_id: str | None = None,
) -> Event:
    """构造一个 Event，自动分配 id 与时间戳。

    `run_id` / `sequence` 默认是「无 run 语境」的形态——session / agent / task 级
    事实的定义特征，不是缺省值。run 级调用方（`make_event`）显式传两者。

    未知事件类型 → `ValueError`：V1 严格白名单（设计文档 §14.3）。这是**唯一**
    一份校验，直接构造 `Event` 的路径此前会绕过它，故两个 `_emit` 各补了一份。
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Unknown event type: {event_type}; not in EVENT_TYPES")
    return Event(
        id=generate_id("evt"),
        run_id=run_id,
        sequence=sequence,
        session_id=session_id,
        type=event_type,
        timestamp=timestamp or now_utc(),
        tenant_id=tenant_id,
        task_id=task_id,
        agent_id=agent_id,
        origin=origin,
        payload=payload or {},
        metadata=metadata or {},
        causation_id=causation_id,
    )


async def emit_event(bus: "EventBus | None", event_type: str, **kwargs) -> None:
    """构造并发出。``bus`` 为 None → no-op。

    None-tolerant 是 `TaskManager._emit` 的既有语义（TM 允许在没有总线的情况下被
    构造，大量单测依赖这一点），在这里统一承担。校验先于发射：坏类型不会先落一半。
    """
    ev = new_event(event_type, **kwargs)
    if bus is None:
        return
    await bus.emit(ev)
```

- [ ] **Step 4: 跑新测试确认通过**

```bash
python -m pytest tests/unit/test_core_events.py -v
```
Expected: 7 passed

- [ ] **Step 5: `TaskManager._emit` 改为薄封装**

`task_manager.py` 的 `_emit` 方法体（保留整段 docstring 原文）改为：

```python
        await emit_event(
            self._event_bus,
            event_type,
            session_id=self._session_id,
            tenant_id=self._session.tenant_id if self._session else "default",
            origin=_ORIGIN,
            task_id=task_id,
            agent_id=(
                agent_id if agent_id is not None
                else (self._agent_id_of(task_id) if task_id is not None else None)
            ),
            payload=payload,
        )
```

import 区补 `from ctx_weft.core.events import emit_event`，按 ruff `F401` 报告清理不再使用的 `EVENT_TYPES` / `Event`（**以 ruff 实际报告为准，不要凭猜删**——`EventType` / `EventOrigin` / `generate_id` / `now_utc` 在别处仍有用）。

- [ ] **Step 6: `SessionRegistry._emit` 改为薄封装**

`session_registry.py` 的 `_emit` 方法体改为：

```python
        await emit_event(
            self.event_bus,
            event_type,
            session_id=session_id,
            tenant_id=tenant_id,
            origin=_ORIGIN,
            agent_id=agent_id,
            payload=payload,
            timestamp=timestamp,
        )
```

import 区补 `from ctx_weft.core.events import emit_event`，按 ruff 清理 `EVENT_TYPES` / `Event`。

- [ ] **Step 7: ALM 的 5 处内联 Event 改为 emit_event**

`agent_lifecycle_manager.py` 的 5 处 `await self.event_bus.emit(Event(...))`：

1. **`apply_input`（`:218` 附近）** →
   ```python
        await emit_event(
            self.event_bus, tr.event_type,
            session_id=rec.session_id, tenant_id=rec.tenant_id,
            origin=_ORIGIN, task_id=task_id, agent_id=agent_id,
            payload=dict(tr.payload),
        )
   ```
   ⚠️ 原代码传的是 `type=tr.event_type`（裸 str，**没走白名单**）。`new_event` 会校验，
   而 `AgentTransition.event_type` 的取值必须全在 `EVENT_TYPES` 里，否则这一步会把
   静默通过变成抛错。**先验证**：
   ```bash
   python -c "
   from ctx_weft.protocols.events import EVENT_TYPES
   for t in ('AgentRunning','AgentIdle','AgentTerminated','AgentWaitingHuman','AgentInterrupted'):
       print(t, t in EVENT_TYPES)
   "
   ```
   五个全 `True` 才继续。有 `False` 就**停下报告**——那说明存在一条 agent 事件没进白名单，
   是需要单独处理的真问题，不属于本次零行为变更的范围。

2. `instantiate` 的 `SPAWN_REJECTED`（`:488` 附近）
3. `instantiate` 的 `AGENT_SPAWNED`（`:559` 附近）
4. `instantiate` 的 `AGENT_INSTANTIATED`（`:578` 附近）
5. `set_agent_llm` 的 `AGENT_LLM_CHANGED`（`:613` 附近，注意传 `causation_id=causation_id`）

后四处形态一致：把
`Event(id=generate_id("evt"), run_id=None, sequence=0, session_id=X, type=T, timestamp=now_utc(), tenant_id=Y, task_id=Z, agent_id=A, origin=_ORIGIN, payload=P)`
换成
`emit_event(self.event_bus, T, session_id=X, tenant_id=Y, origin=_ORIGIN, task_id=Z, agent_id=A, payload=P)`，
**payload 内容一字不动**。

import 区补 `from ctx_weft.core.events import emit_event`，按 ruff 清理 `Event`。

- [ ] **Step 8: `HitlService._emit` 改为薄封装**

`src/ctx_weft/core/hitl/service.py:194-207`：

```python
    async def _emit(self, event_type: EventType, req: PendingHitl, payload: dict) -> None:
        await self._bus.emit(Event(
            id=generate_id("evt"),
            run_id=None,
            sequence=0,
            session_id=req.session_id,
            type=event_type,
            timestamp=self._now(),
            tenant_id=req.tenant_id,
            task_id=req.task_id or None,
            agent_id=req.agent_id or None,
            origin=_ORIGIN,
            payload=payload,
        ))
```
→
```python
    async def _emit(self, event_type: EventType, req: PendingHitl, payload: dict) -> None:
        await emit_event(
            self._bus,
            event_type,
            session_id=req.session_id,
            tenant_id=req.tenant_id,
            origin=_ORIGIN,
            task_id=req.task_id or None,
            agent_id=req.agent_id or None,
            payload=payload,
            timestamp=self._now(),
        )
```

⚠️ `timestamp=self._now()` **必须显式传**——HitlService 持有一个可注入的时钟
（`self._now`），单测靠它冻结时间。丢掉它会让时间源从注入的时钟静默换成 `now_utc()`。

import 区补 `from ctx_weft.core.events import emit_event`，按 ruff 清理 `Event` / `generate_id`。

- [ ] **Step 9: `runtime.py` 的恢复信号改为 emit_event**

`runtime.py:2791-2801`：

```python
        await self._event_bus.emit(Event(
            id=generate_id("evt"),
            run_id=None,
            sequence=0,
            session_id=session_id,
            type=event_type,
            timestamp=now_utc(),
            tenant_id=tenant_id,
            origin=EventOrigin.RUNTIME,
            payload=payload,
        ))
```
→
```python
        await emit_event(
            self._event_bus, event_type,
            session_id=session_id, tenant_id=tenant_id,
            origin=EventOrigin.RUNTIME, payload=payload,
        )
```

import 区补 `from ctx_weft.core.events import emit_event`。

> `runtime.py` 另外两处 `Event(` 是别的形态（`:2592` 用外部传入的 `event_id` 做重放/回填），
> **不动**——它们不是「新造一条事实」，套上 `new_event` 会把 id 生成语义搞错。

- [ ] **Step 10: `make_event` 改为委托**

`src/ctx_weft/core/loop/driver.py:160-187`，函数体改为：

```python
def make_event(
    state: LoopState,
    type: str,
    payload: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    causation_id: str | None = None,
    *,
    origin: str | None = None,
) -> Event:
    """构造一个 run 级 Event：从 LoopState 抽字段 + 自增 sequence。

    封套本身与白名单校验交 `core.events.new_event`（唯一一份）；本函数只保留
    run 域真正属于自己的两件事——LoopState 的字段抽取，与 `sequence_counter` 自增。
    """
    state.sequence_counter += 1
    return new_event(
        type,
        session_id=state.session.id,
        tenant_id=state.session.tenant_id,
        origin=origin if origin is not None else getattr(state, "origin", ""),
        run_id=state.run_id,
        sequence=state.sequence_counter,
        task_id=state.task.id,
        agent_id=state.agent.id,
        payload=payload,
        metadata=metadata,
        causation_id=causation_id,
    )
```

⚠️ **行为差异一处，必须确认无碍**：原代码在 `raise ValueError` **之前**不自增
`sequence_counter`；新代码把自增放在 `new_event`（校验点）**之前**。即坏类型下
counter 会多加 1。判断：`make_event` 的坏类型是编程错误、抛出后那次 run 即崩溃，
counter 的值不再被观察。**但**若有测试断言「坏类型不改 counter」，则把自增挪到
`new_event` 之后：

```bash
grep -rn "sequence_counter" tests/ src/ctx_weft | grep -v "driver.py"
```
有命中就逐个看；无命中则按上面写法。

import 区补 `from ctx_weft.core.events import new_event`，按 ruff 清理 `EVENT_TYPES` /
`generate_id` / `now_utc`（若 driver.py 别处仍用则保留）。

- [ ] **Step 11: 确认无环**

```bash
python -c "
import ctx_weft.core.events, ctx_weft.core.hitl.service
import ctx_weft.core.orchestrator, ctx_weft.core.loop.driver, ctx_weft.core.runtime
print('ok')
"
```
Expected: `ok`

- [ ] **Step 12: 跑事件相关测试**

```bash
python -m pytest tests/unit/test_core_events.py tests/unit/test_event_origin.py \
  tests/unit/test_event_persistence_wiring.py tests/unit/test_hitl_service.py \
  tests/unit/test_llm_event_convergence.py -v
```
Expected: 全 PASS

> `test_event_origin.py` 是这一步的核心安全网——它逐条钉死了哪个组件发哪条事件、
> origin 填什么。红了就说明某处 origin/字段被改动，逐字比对回退。

- [ ] **Step 13: 跑全量 + lint**

```bash
python -m pytest -q --tb=no 2>&1 | tee /tmp/now.txt | tail -3
grep "^FAILED" /tmp/now.txt | sed 's/ - .*//' | sort > /tmp/now-failures.txt
diff /tmp/baseline-failures.txt /tmp/now-failures.txt && echo "失败集合未变 ✓"
python -m ruff check src tests
```
Expected: `N+11 passed` **且** `失败集合未变 ✓`

- [ ] **Step 14: 提交**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor: 事件封套构造收进 core/events.py

「无 run 语境」的封套（run_id=None, sequence=0）此前在全仓有 9 处逐字重复，
跨 3 个包：orchestrator(7) / hitl(1) / runtime(1)；EVENT_TYPES 白名单校验
另有 3 份（loop.driver.make_event + TaskManager._emit + SessionRegistry._emit）。

放 core/ 顶层而不是 orchestrator/ 子包：core.hitl 今天只依赖 protocols +
core.utils，把 helper 塞进 orchestrator 会新增一条 hitl → orchestrator 边，
而 loop 和 runtime 都在 hitl 之上，方向是反的。

make_event 保留（LoopState 字段抽取 + sequence 自增是 run 域的真实职责），
只是改为委托 new_event，白名单校验因此只剩一份。

零行为变更：payload / origin / 时间戳来源逐字保持（HitlService 的可注入时钟
显式透传）。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: capability 四件套搬去 `core/capabilities/`

**Files:**
- Create: `src/ctx_weft/core/capabilities/__init__.py`
- Move: `orchestrator/capability_cache.py` → `capabilities/cache.py`
- Move: `orchestrator/capability_resolver.py` → `capabilities/resolver.py`
- Move: `orchestrator/control_capability.py` → `capabilities/control_tools.py`
- Move: `orchestrator/skill_executor_capability.py` → `capabilities/skill_executor.py`
- Modify: `src/ctx_weft/core/orchestrator/__init__.py`
- Modify: ~60 个 src/tests 文件的 import 行（脚本批改）
- Modify: `src/ctx_weft/core/loop/driver.py:30`（包级 import 拆分）

**命名说明：** `control_capability.py` 搬过去叫 **`control_tools.py`** 而不是 `control.py`——`src/ctx_weft/core/control/` 已经存在（控制平面：CancelToken / reducers / replay），`core.capabilities.control` 与 `core.control` 并存会持续误导。`control_tools.py` 也更准确：文件内容就是 7 个内置控制**工具** + 它们的 provider。

- [ ] **Step 1: 建包**

```bash
mkdir -p src/ctx_weft/core/capabilities
cat > src/ctx_weft/core/capabilities/__init__.py <<'EOF'
"""Capability 层：缓存 / 解析 / 两个内置 provider。

这四个模块此前住在 `core.orchestrator` 下，但调度那半边一行都没引用它们——
真正的消费者是 `core.loop`（`steps/_capabilities.py`、`capability_gateway.py`、
若干 step）和 `core.assembler.composer`。按消费者归位。
"""

from ctx_weft.core.capabilities.cache import CapabilityCache
from ctx_weft.core.capabilities.control_tools import ControlCapabilityProvider
from ctx_weft.core.capabilities.resolver import CapabilityResolver
from ctx_weft.core.capabilities.skill_executor import SkillExecutorCapabilityProvider

__all__ = [
    "CapabilityCache",
    "CapabilityResolver",
    "ControlCapabilityProvider",
    "SkillExecutorCapabilityProvider",
]
EOF
```

- [ ] **Step 2: git mv 四个文件**

```bash
git mv src/ctx_weft/core/orchestrator/capability_cache.py            src/ctx_weft/core/capabilities/cache.py
git mv src/ctx_weft/core/orchestrator/capability_resolver.py         src/ctx_weft/core/capabilities/resolver.py
git mv src/ctx_weft/core/orchestrator/control_capability.py          src/ctx_weft/core/capabilities/control_tools.py
git mv src/ctx_weft/core/orchestrator/skill_executor_capability.py   src/ctx_weft/core/capabilities/skill_executor.py
```

- [ ] **Step 3: 批改 import 路径**

```bash
grep -rl "orchestrator\.\(capability_cache\|capability_resolver\|control_capability\|skill_executor_capability\)" --include=*.py src tests \
| xargs sed -i \
  -e 's/core\.orchestrator\.capability_cache/core.capabilities.cache/g' \
  -e 's/core\.orchestrator\.capability_resolver/core.capabilities.resolver/g' \
  -e 's/core\.orchestrator\.control_capability/core.capabilities.control_tools/g' \
  -e 's/core\.orchestrator\.skill_executor_capability/core.capabilities.skill_executor/g'
```

验证无残留：

```bash
grep -rn "orchestrator\.\(capability_cache\|capability_resolver\|control_capability\|skill_executor_capability\)" --include=*.py src tests
```
Expected: 无输出

- [ ] **Step 4: 修 orchestrator/__init__.py**

`src/ctx_weft/core/orchestrator/__init__.py` 全文替换为：

```python
"""Core orchestrator: TaskQueue, TaskManager, AgentLifecycleManager, SessionRegistry.

Capability 层（cache / resolver / control_tools / skill_executor）已迁往
`ctx_weft.core.capabilities`——它们的消费者是 `core.loop`，不是调度。
"""

from ctx_weft.core.orchestrator.agent_lifecycle_manager import AgentLifecycleManager
from ctx_weft.core.orchestrator.session_registry import SessionRegistry
from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.orchestrator.task_queue import TaskQueue

__all__ = [
    "AgentLifecycleManager",
    "SessionRegistry",
    "TaskManager",
    "TaskQueue",
]
```

- [ ] **Step 5: 修 driver.py 的包级 import**

`src/ctx_weft/core/loop/driver.py:30`：

```python
    from ctx_weft.core.orchestrator import CapabilityCache, TaskManager
```
→
```python
    from ctx_weft.core.capabilities import CapabilityCache
    from ctx_weft.core.orchestrator import TaskManager
```

- [ ] **Step 6: 修 runtime.py 的两处函数内 import**

`runtime.py:289-291` 与 `:571-573` 的：

```python
        from ctx_weft.core.orchestrator.skill_executor_capability import (
            SkillExecutorCapabilityProvider,
        )
```

Step 3 的 sed 已经把路径改对了，但换行后的括号形式可能变得不必要。**不要顺手压成一行**——保持 diff 最小。只需确认路径是 `ctx_weft.core.capabilities.skill_executor`。

同样检查 `runtime.py:596`（`template_lookup` 的函数内 import）**不受影响**——`template_lookup` 留在 orchestrator。

- [ ] **Step 7: 确认没有循环 import**

```bash
python -c "import ctx_weft.core.runtime; import ctx_weft.core.capabilities; import ctx_weft.core.orchestrator; print('ok')"
```
Expected: `ok`

> `core.capabilities.control_tools` 在 `TYPE_CHECKING` 下引 `orchestrator.task_manager.TaskManager`（原文件 `:32`），运行期不成环。若这里报 `ImportError`，检查是否有非 `TYPE_CHECKING` 的反向引用漏改。

- [ ] **Step 8: 跑全量 + lint**

```bash
python -m pytest -q --tb=no 2>&1 | tee /tmp/now.txt | tail -3
grep "^FAILED" /tmp/now.txt | sed 's/ - .*//' | sort > /tmp/now-failures.txt
diff /tmp/baseline-failures.txt /tmp/now-failures.txt && echo "失败集合未变 ✓"
python -m ruff check src tests
```
Expected: `N+11 passed` **且** `失败集合未变 ✓`（数字与 Task 2 结束时一致——纯搬家不增不减）

- [ ] **Step 9: 提交**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor: capability 四件套从 core.orchestrator 迁往 core.capabilities

capability_cache / capability_resolver / control_capability /
skill_executor_capability 的唯一消费者是 core.loop 与 core.assembler，
调度半边零引用。按消费者归位，包名恢复诚实。

- capability_cache.py            → capabilities/cache.py
- capability_resolver.py         → capabilities/resolver.py
- control_capability.py          → capabilities/control_tools.py（避开已存在的 core/control/）
- skill_executor_capability.py   → capabilities/skill_executor.py

不留转发 shim：旧路径一次改干净（src + tests 共 ~60 文件）。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: `TaskManagerHooks` —— 13 个 setter 收成 4 个

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/task_manager.py`
- Modify: `src/ctx_weft/core/runtime.py:1393-1436`
- Test: `tests/unit/test_task_manager_hooks.py`（新建）

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class TaskManagerHooks:
      is_current: "Callable[[], bool] | None" = None
      session_registry: "SessionRegistry | None" = None
      cancel_pending_hitl: "Callable[[], Coroutine[Any, Any, None]] | None" = None
      cancel_inflight: "Callable[[str], bool] | None" = None
      threshold_finalizer: "Callable[[Task | None, list[Task], list[tuple[str, str]]], Coroutine[Any, Any, None]] | None" = None
      cancel_finalizer: "Callable[[list[Task], str], Coroutine[Any, Any, None]] | None" = None
      on_session_done: "Callable[[], Coroutine[Any, Any, None]] | None" = None
      on_session_idle: "Callable[[], Coroutine[Any, Any, None]] | None" = None

  def TaskManager.set_hooks(self, hooks: TaskManagerHooks) -> None
  ```
- 保留为独立方法（它们在不同时刻被多次调用，不属于一次性接线）：`set_runner`、`set_session`、`set_pause_abandon`、`track_background`

**依据：** `runtime.py:1393-1436` 是一段**连续**代码，一口气调了这 8 个 setter，且此后再不重设。而 `set_session`（`:1222 :1370 :1744` 三处）、`set_runner`（`:1360 :1764` 两处）、`set_pause_abandon`（`:840 :862 :1433` 三处，是运行期开关）不是。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_task_manager_hooks.py`：

```python
"""TaskManagerHooks：一次性接线的 8 个回调收成一个不可变载荷。"""

from ctx_weft.core.orchestrator.task_manager import TaskManager, TaskManagerHooks


def _tm() -> TaskManager:
    return TaskManager(session_id="ses_1")


def test_default_hooks_all_none():
    tm = _tm()
    # 未接线的 TM 仍是可用的：is_alive 在无谓词时恒 True
    assert tm.is_alive() is True


def test_set_hooks_installs_is_current():
    tm = _tm()
    tm.set_hooks(TaskManagerHooks(is_current=lambda: False))
    assert tm.is_alive() is False


def test_set_hooks_replaces_wholesale():
    """hooks 是整体替换语义，不是逐字段合并——避免半接线的中间态。"""
    tm = _tm()
    tm.set_hooks(TaskManagerHooks(is_current=lambda: False))
    tm.set_hooks(TaskManagerHooks())
    assert tm.is_alive() is True


def test_hooks_is_frozen():
    import dataclasses
    import pytest

    h = TaskManagerHooks()
    with pytest.raises(dataclasses.FrozenInstanceError):
        h.is_current = lambda: True  # type: ignore[misc]


def test_legacy_setters_are_gone():
    """8 个一次性 setter 已被 set_hooks 取代。"""
    tm = _tm()
    for name in (
        "set_is_current", "set_session_registry", "set_cancel_pending_hitl",
        "set_cancel_inflight", "set_threshold_finalizer", "set_cancel_finalizer",
        "set_session_done_callback", "set_session_idle_callback",
    ):
        assert not hasattr(tm, name), f"{name} 应已被 set_hooks 取代"


def test_运行期开关仍是独立方法():
    tm = _tm()
    assert hasattr(tm, "set_runner")
    assert hasattr(tm, "set_session")
    assert hasattr(tm, "set_pause_abandon")
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/unit/test_task_manager_hooks.py -v
```
Expected: `ImportError: cannot import name 'TaskManagerHooks'`

- [ ] **Step 3: 定义 TaskManagerHooks**

在 `task_manager.py` 的 `class TaskManager:` **之前**插入（把 8 个 setter 的 docstring 原文搬成字段注释，一句不丢）：

```python
@dataclass(frozen=True)
class TaskManagerHooks:
    """TaskManager 的一次性接线载荷。由 `runtime._register_and_drain` 一口气装好。

    这 8 个字段此前是 8 个独立 setter，构造完的 TaskManager 因此是个半成品，
    接线顺序成了隐含契约。收成一个不可变载荷、整体替换（不逐字段合并），
    半接线的中间态就不再表示得出来。

    **仍是独立方法的三个**（它们在不同时刻被多次调用，不属于一次性接线）：
    `set_runner`（start / recover 各一次）、`set_session`（三处）、
    `set_pause_abandon`（pause 窗口的运行期开关）。

    全部字段 None-tolerant：缺注入时对应副作用整体跳过，绝不崩溃。
    """

    #: 归属权谓词：本 TM 是否仍是该 session 的当前 owner。None = 不受管（永远视为
    #: current，保持旧行为）。被同 session 上更新的 TM 顶替后返回 False → 迟到的收尾
    #: 变 no-op（不发 SessionFinished、不 _release_session）。
    is_current: "Callable[[], bool] | None" = None

    #: 会话状态的持有者。TM 对它**只查询、只发事实**；唯一的方法调用是 `cancel`，
    #: 那是外部命令的透传，不是 TM 在驱动 SM。
    session_registry: "SessionRegistry | None" = None

    #: "取消该 session 所有未决 pending HITL"（runtime 侧遍历 HitlService.cancel）。
    #: trip 序列第 3 步 best-effort 调用；HitlCancelled 需全部先于会话终态发出。
    cancel_pending_hitl: "Callable[[], Coroutine[Any, Any, None]] | None" = None

    #: "对指定在途 task 发协作取消信号"（runtime 侧查 _run_tokens 发 cancel）。
    #: 只发信号不代表任务立即终结——该任务的 TASK_CANCELED（非 root）由 run 结束后的
    #: `apply_run_outcome` 发；root 则由 trip 序列自己先标 FAILED。
    cancel_inflight: "Callable[[str], bool] | None" = None

    #: 熔断收尾：(root_we_failed_and_started|None, ack_tasks, failures) -> None。
    #: trip 序列第 7 步内联 await（不是后台甩），保证 memory 落盘发生在 SESSION_FINISHED
    #: （SSE 关闭）之前；异常只记日志不阻断终结。
    threshold_finalizer: (
        "Callable[[Task | None, list[Task], list[tuple[str, str]]], "
        "Coroutine[Any, Any, None]] | None"
    ) = None

    #: 统一取消胶囊闭合：(tasks, reason) -> None。调用点：`cancel_all`
    #: （reason=USER_CANCEL）、熔断清场对已启动的挂起/排队任务
    #: （reason=FAILURE_THRESHOLD）、`on_task_finished` 的 CANCELED 分支
    #: （在途协作取消 funnel）。异常记日志不阻断。
    cancel_finalizer: "Callable[[list[Task], str], Coroutine[Any, Any, None]] | None" = None

    #: session 真正结束（所有任务处理完、无重试待执行）时调用。幂等由调用方承担
    #: （runtime 侧 `_release_session` 本就幂等）：会话「已终态吸收一切」的闩锁长在
    #: 状态机里，TM 不再自持一份。
    on_session_done: "Callable[[], Coroutine[Any, Any, None]] | None" = None

    #: session 进入**空闲挂起**（有任务 park/suspend 且无其它在跑任务、非终结）时调用。
    #: 区别于 `on_session_done`：那是终结回调（回收全部 per-session 状态）；这是
    #: 「会话暂停、待续接」的信号，供 runtime 回收按 run 计的控制信号（pause/cancel
    #: token）。可多次触发（每次 park 一次）；回调须幂等。
    on_session_idle: "Callable[[], Coroutine[Any, Any, None]] | None" = None
```

- [ ] **Step 4: TaskManager 内部改用 `self._hooks`**

`__init__` 里删掉这 8 个字段的初始化（`_on_session_done` / `_on_session_idle` / `_session_registry` / `_is_current` / `_cancel_pending_hitl` / `_cancel_inflight` / `_threshold_finalizer` / `_cancel_finalizer`，连同它们上方的长注释——注释已搬进 `TaskManagerHooks` 字段），加一行：

```python
        self._hooks = TaskManagerHooks()
```

加方法（放在 `set_runner` 旁边）：

```python
    def set_hooks(self, hooks: TaskManagerHooks) -> None:
        """一次性装好全部回调。**整体替换**，不逐字段合并（见 TaskManagerHooks）。"""
        self._hooks = hooks
```

删掉这 8 个 setter 方法。然后把全文里的引用逐个改名：

| 旧 | 新 |
|---|---|
| `self._is_current` | `self._hooks.is_current` |
| `self._session_registry` | `self._hooks.session_registry` |
| `self._cancel_pending_hitl` | `self._hooks.cancel_pending_hitl` |
| `self._cancel_inflight` | `self._hooks.cancel_inflight` |
| `self._threshold_finalizer` | `self._hooks.threshold_finalizer` |
| `self._cancel_finalizer` | `self._hooks.cancel_finalizer` |
| `self._on_session_done` | `self._hooks.on_session_done` |
| `self._on_session_idle` | `self._hooks.on_session_idle` |

```bash
sed -i \
  -e 's/self\._is_current/self._hooks.is_current/g' \
  -e 's/self\._session_registry/self._hooks.session_registry/g' \
  -e 's/self\._cancel_pending_hitl/self._hooks.cancel_pending_hitl/g' \
  -e 's/self\._cancel_inflight/self._hooks.cancel_inflight/g' \
  -e 's/self\._threshold_finalizer/self._hooks.threshold_finalizer/g' \
  -e 's/self\._cancel_finalizer/self._hooks.cancel_finalizer/g' \
  -e 's/self\._on_session_done/self._hooks.on_session_done/g' \
  -e 's/self\._on_session_idle/self._hooks.on_session_idle/g' \
  src/ctx_weft/core/orchestrator/task_manager.py
```

⚠️ 跑完 sed 后**通读一遍 diff**：上面 8 个 setter 方法体里也有这些名字，它们应该已在本 Step 前被整个删掉；若 sed 命中了残留的 setter，说明删漏了。

- [ ] **Step 5: 改 runtime.py 接线点**

`runtime.py:1393-1436`，把 8 次 `task_manager.set_*(...)` 与两个内嵌的 `async def _on_done/_on_idle` 重排成一次 `set_hooks`。`_on_done` / `_on_idle` 的定义**位置上移**到调用之前，函数体一字不动：

```python
        async def _on_done() -> None:
            # compare-and-clear：仅当本 TM 仍是当前 owner 才回收，避免顶替它的新 TM 被误释放。
            if self._task_managers.get(session.id) is task_manager:
                self._release_session(session.id)

        async def _on_idle() -> None:
            # per-run token 生命周期已随 run 对齐（execute finally 注销），无需在此回收。
            # 只清 pause 弃子闩锁；compare-and-check 防被顶替旧 TM 的迟到 idle 误清新一轮闩锁。
            if self._task_managers.get(session.id) is task_manager:
                self._pausing.discard(session.id)
                self._pause_claimed.discard(session.id)
                task_manager.set_pause_abandon(False)

        task_manager.set_hooks(TaskManagerHooks(
            # 归属权谓词：多轮对话里每次 resume 都新建 TM 并覆盖 _task_managers 映射。
            # 旧 TM 的收尾若迟到（被其慢的 background observe 拖住），必须认出自己已被
            # 顶替、变 no-op，否则会冲掉新一轮的会话状态。
            is_current=lambda tm=task_manager: self._task_managers.get(session.id) is tm,
            session_registry=self._session_registry,
            cancel_pending_hitl=lambda sid=session.id: self._cancel_session_hitl(
                sid, message=CancelReason.FAILURE_THRESHOLD),
            cancel_inflight=lambda tid, sid=session.id: self._cancel_run_token(sid, tid),
            threshold_finalizer=lambda root, ack_tasks, failures, sess=session: (
                self._finalize_threshold_memory(sess, root, ack_tasks, failures)),
            cancel_finalizer=lambda tasks, reason, sess=session: (
                self._finalize_cancel_memory(sess, tasks, reason)),
            on_session_done=_on_done,
            on_session_idle=_on_idle,
        ))
        # 纳入 SM 管理。setdefault 语义，重入安全：多轮对话/恢复重建都会走到这里，
        # 已有状态不被重置（新一轮的显式 RUNNING 由 resume_session 负责）。
        self._session_registry.register_session(session.id, tenant_id=session.tenant_id)
```

`runtime.py:66` 的 import 补上 `TaskManagerHooks`：

```python
from ctx_weft.core.orchestrator.task_manager import TaskManager, TaskManagerHooks, task_payload
```

- [ ] **Step 6: 修其它调用点**

```bash
grep -rn "set_is_current\|set_session_registry\|set_cancel_pending_hitl\|set_cancel_inflight\|set_threshold_finalizer\|set_cancel_finalizer\|set_session_done_callback\|set_session_idle_callback" --include=*.py src tests
```

逐个改成 `set_hooks(TaskManagerHooks(...))`。测试里常见形态：

```python
    tm.set_threshold_finalizer(_finalizer)
```
→
```python
    tm.set_hooks(TaskManagerHooks(threshold_finalizer=_finalizer))
```

⚠️ 若同一个测试连续调了两个 setter（`tests/unit/test_failure_threshold_trip.py` 里可能有），必须合并成**一次** `set_hooks`——整体替换语义下，两次调用会让第一个丢失。逐个测试确认。

- [ ] **Step 7: 跑测试**

```bash
python -m pytest tests/unit/test_task_manager_hooks.py tests/unit/test_failure_threshold_trip.py tests/unit/test_threshold_finalizer.py -v
```
Expected: 全 PASS

- [ ] **Step 8: 跑全量 + lint**

```bash
python -m pytest -q --tb=no 2>&1 | tee /tmp/now.txt | tail -3
grep "^FAILED" /tmp/now.txt | sed 's/ - .*//' | sort > /tmp/now-failures.txt
diff /tmp/baseline-failures.txt /tmp/now-failures.txt && echo "失败集合未变 ✓"
python -m ruff check src tests
```
Expected: `N+17 passed` **且** `失败集合未变 ✓`

- [ ] **Step 9: 提交**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor(orchestrator): TaskManager 的 8 个一次性 setter 收成 TaskManagerHooks

构造完的 TaskManager 此前是个半成品，13 个 set_* 的接线顺序成了隐含契约
（_make_root_task_manager 那条「push_task 前必须先 set_session」的注释就是
被它咬过一次）。runtime._register_and_drain 里连续调用的 8 个收成一个
frozen dataclass，整体替换、不逐字段合并，半接线的中间态不再表示得出来。

运行期开关仍是独立方法：set_runner / set_session / set_pause_abandon。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: 抽出 `task_reopen.py`

**Files:**
- Create: `src/ctx_weft/core/orchestrator/task_reopen.py`
- Modify: `src/ctx_weft/core/orchestrator/task_manager.py`（`reopen_task` 瘦身；删两个模块级 helper）
- Modify: `tests/unit/test_task_reopen_multimodal.py:112,122,129`（`_append_text_sections` 的 import 路径）
- Test: `tests/unit/test_reopen_prompt.py`（新建）

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class ReopenPrompt:
      user_prompt: "str | list[ContentPart]"
      user_prompt_event_jsonable: "str | list[dict] | None"
      original_user_prompt: "str | list[ContentPart]"
      original_user_prompt_event_jsonable: "str | list[dict] | None"

  def build_reopen_prompt(
      task: "Task", reason: str, upstream: "tuple[str, str] | None",
  ) -> ReopenPrompt

  def append_text_sections(
      jsonable: "str | list[dict] | None", sections: "list[str]",
  ) -> "str | list[dict] | None"

  def outputs_to_text(outputs: Any) -> str
  ```

**为什么这么切：** `reopen_task` 的 90 行里只有约 20 行是调度（拿锁、翻状态、push、emit），其余全是 prompt 拼装 + 事件侧 jsonable 的同构维护——那是内容操作，与队列无关，而且它是本文件注释密度最高的一段（`_append_text_sections` 一个 helper 就带 20 行 docstring 论证两条路径逐分支同构）。切出去之后这段逻辑可以被**直接单测**，不必先造一个 TaskManager。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_reopen_prompt.py`：

```python
"""build_reopen_prompt：memory 侧与 event 侧两条路径必须逐分支同构。"""

from ctx_weft.core.orchestrator.task_reopen import (
    ReopenPrompt,
    append_text_sections,
    build_reopen_prompt,
    outputs_to_text,
)
from ctx_weft.core.state.models import NormalTaskSettings, Task


def _task(**kw) -> Task:
    base = dict(
        id="tsk_1", session_id="ses_1", status="FINISHED",
        settings=NormalTaskSettings(),
    )
    base.update(kw)
    return Task(**base)  # type: ignore[arg-type]


def test_str_base_direct_revision():
    t = _task(user_prompt="做一个 X", outputs="旧结果")
    p = build_reopen_prompt(t, "不对，重做", upstream=None)
    assert isinstance(p, ReopenPrompt)
    assert p.original_user_prompt == "做一个 X"
    assert p.user_prompt == (
        "做一个 X\n\n## Previous attempt (rejected)\n旧结果"
        "\n\n## Revision required\n不对，重做"
    )


def test_upstream_note_replaces_direct_reason():
    """级联重开的后继拿的是 upstream 提示，不是直接修订说明。"""
    t = _task(user_prompt="第二步", outputs="")
    p = build_reopen_prompt(t, "head 错了", upstream=("第一步", "head 错了"))
    assert "## Upstream task revised" in str(p.user_prompt)
    assert "## Revision required" not in str(p.user_prompt)


def test_original_snapshot_taken_once_no_accumulation():
    """重复 reopen 始终基于 original，不叠加。"""
    t = _task(user_prompt="原始", outputs="A")
    p1 = build_reopen_prompt(t, "第一次", upstream=None)
    t.user_prompt = p1.user_prompt
    t.original_user_prompt = p1.original_user_prompt
    t.outputs = "B"
    p2 = build_reopen_prompt(t, "第二次", upstream=None)
    assert str(p2.user_prompt).count("原始") == 1
    assert "第一次" not in str(p2.user_prompt)


def test_list_base_is_copied_not_aliased():
    """list base 直接复用同一引用会让 user_prompt 与 original 别名同一份 parts。"""
    parts = [{"type": "text", "text": "看图"}]
    t = _task(user_prompt=list(parts), outputs="")
    t.original_user_prompt = list(parts)
    p = build_reopen_prompt(t, "重做", upstream=None)
    assert p.user_prompt is not p.original_user_prompt


def test_append_text_sections_empty_bases_collapse_to_str():
    assert append_text_sections([], ["a", "b"]) == "a\n\nb"
    assert append_text_sections("", ["a", "b"]) == "a\n\nb"
    assert append_text_sections(None, ["a", "b"]) == "a\n\nb"


def test_append_text_sections_merges_into_trailing_text_part():
    base = [{"type": "image", "source": {}}, {"type": "text", "text": "hi"}]
    out = append_text_sections(base, ["S"])
    assert isinstance(out, list) and len(out) == 2
    assert out[-1]["text"] == "hi\n\nS"


def test_append_text_sections_noop_without_sections():
    base = [{"type": "text", "text": "hi"}]
    assert append_text_sections(base, []) is base


def test_outputs_to_text():
    assert outputs_to_text("x") == "x"
    assert outputs_to_text([{"type": "text", "text": "a"},
                            {"type": "image", "source": {}},
                            {"type": "text", "text": "b"}]) == "a\nb"
    assert outputs_to_text(None) == ""
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/unit/test_reopen_prompt.py -v
```
Expected: `ModuleNotFoundError: No module named 'ctx_weft.core.orchestrator.task_reopen'`

- [ ] **Step 3: 写 task_reopen.py**

新建 `src/ctx_weft/core/orchestrator/task_reopen.py`。把 `task_manager.py` 里 `reopen_task` 的 prompt 构造段（`:660-733` 之间的非队列部分）与两个模块级 helper（`_append_text_sections` `:1482`、`_outputs_to_text` `:1514`）**连同全部注释原样**搬过来，组织成：

```python
"""reopen 的 prompt 改写：memory 侧内容 + event 侧 jsonable，两条路径逐分支同构。

从 `task_manager.py` 搬出——`reopen_task` 的 90 行里只有约 20 行是调度（拿锁、
翻状态、push、emit），其余全是内容拼装，与队列无关。切出来之后这段逻辑可以直接
单测，不必先造一个 TaskManager。

两条路径的同构是本模块的**全部难点**：memory 侧（`build_reopen_prompt` 的
`user_prompt`）逐 section 调 `content_with_suffix`，event 侧
（`append_text_sections`）必须产出逐字节等价的结果——否则崩溃恢复重放出来的
prompt 会和内存里跑的那份分叉。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ctx_weft.core.content import content_with_suffix

if TYPE_CHECKING:
    from ctx_weft.core.state.models import Task
    from ctx_weft.protocols import ContentPart

__all__ = [
    "ReopenPrompt",
    "append_text_sections",
    "build_reopen_prompt",
    "outputs_to_text",
]


@dataclass(frozen=True)
class ReopenPrompt:
    """一次 reopen 的四份内容。调用方（`TaskManager.reopen_task`）照原样写回 Task。

    original_* 两个字段是**首次 reopen 时拍下的快照**，之后每次 reopen 都以它为
    base——重复 reopen 因此不会叠加修订说明。调用方须把它们写回 task，否则下一次
    reopen 会把已改写过的 prompt 当成 base。
    """

    user_prompt: "str | list[ContentPart]"
    user_prompt_event_jsonable: "str | list[dict] | None"
    original_user_prompt: "str | list[ContentPart]"
    original_user_prompt_event_jsonable: "str | list[dict] | None"


def build_reopen_prompt(
    task: "Task", reason: str, upstream: "tuple[str, str] | None",
) -> ReopenPrompt:
    """按 base + 上一轮产出 + 修订说明拼出重跑用的 prompt（memory 侧 + event 侧）。

    `upstream=(head_title, head_reason)` 标记级联重开的后继：它拿到的是
    「上游任务已修订，按更新后的结果重做」的指令（上游的新结果经 blackboard
    订阅送达），而不是直接的修订说明。

    照搬改动前 `reopen_task` docstring 的第 2-4 段（从 "Resets the task to a clean
    PENDING state..." 起，到 "...pointing at the predecessor's updated result" 止），
    去掉其中只与队列有关的两句（"Emits TASK_REQUEUED..." 与 "Returns True if the
    task was re-queued."）——那两句描述的是调用方的行为，留在 `TaskManager.reopen_task`
    自己的 docstring 里。
    """
    # base = 首次执行的原始 prompt（首次 reopen 时快照下来）
    if task.original_user_prompt is None:
        # 原样保留（含多模态）：这是 reopen 的 base，拍扁会让重开后图片永久消失。
        original = task.user_prompt or ""
        original_jsonable = task.user_prompt_event_jsonable
    else:
        original = task.original_user_prompt
        original_jsonable = task.original_user_prompt_event_jsonable

    prev_output = outputs_to_text(task.outputs) or (task.process_report or "")
    sections: list[str] = []
    if prev_output:
        sections.append(f"## Previous attempt (rejected)\n{prev_output}")
    if upstream is not None:
        head_title, head_reason = upstream
        sections.append(
            f"## Upstream task revised\n"
            f"Predecessor '{head_title}' was reopened (reason: {head_reason}). "
            f"Its updated result appears in the conversation above. "
            f"Redo this task based on the updated result."
        )
    elif reason:
        sections.append(f"## Revision required\n{reason}")

    # base 可能是多模态（list[ContentPart]），不能进 "\n\n".join()。
    # 有 base 时从 base 起逐段 content_with_suffix；无 base 时退回纯文本 join。
    if original:
        # 无 section 时 new_prompt 必须与 base 是不同对象：list base 若直接复用同一
        # 引用，task.user_prompt 与 task.original_user_prompt 会别名同一份 parts，
        # 日后任一方被就地修改都会污染另一方（str 不可变故无此风险）。
        new_prompt: "str | list[ContentPart]" = (
            list(original) if isinstance(original, list) else original
        )
        for sec in sections:
            new_prompt = content_with_suffix(new_prompt, f"\n\n{sec}")
    else:
        new_prompt = "\n\n".join(sections) if sections else original

    # 兜底：event jsonable 没被填上（历史上 `_restore_task_prompts` 跳过纯文本、
    # `run_single_task` 丢弃它，都出过这个洞），而 base 又是非空 str。纯文本的事件
    # 形态就是它自己，直接补上；决不能让「字段没填」被 `append_text_sections` 读成
    # 「base 为空」，那会把用户的原始指令从 TASK_REQUEUED 里抹掉、并在下一次重放时
    # 永久生效。只兜 str：list base 的事件形态含 event ref，core 无从凭空重建（重建
    # 就意味着拿 memory ref 冒充 event ref，正是两个命名空间不得相通的红线）。
    if original_jsonable is None and isinstance(original, str) and original:
        original_jsonable = original

    return ReopenPrompt(
        user_prompt=new_prompt,
        user_prompt_event_jsonable=append_text_sections(original_jsonable, sections),
        original_user_prompt=original,
        original_user_prompt_event_jsonable=original_jsonable,
    )


def append_text_sections(
    jsonable: "str | list[dict] | None", sections: "list[str]",
) -> "str | list[dict] | None":
    """照搬改动前 `task_manager._append_text_sections` 的整段 docstring（"把 reopen 的
    文本 section 追加到事件侧 jsonable 尾部..." 起的全部 18 行），一字不改——它逐分支
    论证了与 memory 侧的同构，是本模块最要紧的一份说明。
    """
    if not sections:
        return jsonable
    if not jsonable:  # None / "" / [] —— 与 memory 侧 `if base_prompt:` 判据一致
        return "\n\n".join(sections)
    suffix = "".join(f"\n\n{sec}" for sec in sections)
    if isinstance(jsonable, str):
        return jsonable + suffix
    if isinstance(jsonable[-1], dict) and jsonable[-1].get("type") == "text":
        tail = jsonable[-1]
        merged = {**tail, "text": tail.get("text", "") + suffix}
        return [*jsonable[:-1], merged]
    return [*jsonable, {"type": "text", "text": suffix}]


def outputs_to_text(outputs: Any) -> str:
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
```

- [ ] **Step 4: 跑新测试**

```bash
python -m pytest tests/unit/test_reopen_prompt.py -v
```
Expected: 8 passed

- [ ] **Step 5: TaskManager.reopen_task 瘦身**

`task_manager.py` 里 `reopen_task` 的方法体（docstring 保留）替换为：

```python
        task = self._tasks.get(task_id)
        if task is None or task.status != "FINISHED":
            return False

        prompt = build_reopen_prompt(task, reason, upstream)

        async with self._lock:
            self._queue.unmark_completed(task_id)
            self._queue.unmark_running(task_id)
            task.status = "PENDING"
            task.actor_done = False
            task.retry_count = 0
            task.outputs = None
            task.finished_at = None
            task.user_prompt = prompt.user_prompt
            task.original_user_prompt = prompt.original_user_prompt
            task.original_user_prompt_event_jsonable = (
                prompt.original_user_prompt_event_jsonable
            )
            task.user_prompt_event_jsonable = prompt.user_prompt_event_jsonable
            task.user_prompt_in_memory = False  # let the driver re-ingest the revised prompt
            if blocked_by is not None:
                task.dag_deps = list(blocked_by)  # restart 时由 dag_deps 重建依赖链
            self._queue.push(QueueEntry(
                task_id=task_id, session_id=self._session_id, priority=task.priority,
                blocked_by=set(blocked_by or []),
            ))
        # 把改写后的 prompt 一并落进事件，使崩溃恢复（event replay）能重建修订后的
        # user_prompt。reopen 只在 prompt 尾部追加**文本** section，不可能引入事件流
        # 没见过的图。故事件形态直接由首次发射那份 + 文本拼出，零 blob IO，且同一张图
        # 的 event ref 跨 reopen 逐字节相同（重放确定性）。
        await self._emit(
            EventType.TASK_REQUEUED,
            task_id=task_id,
            payload={
                "reason": "observer_review_reopen",
                "user_prompt": prompt.user_prompt_event_jsonable,
                "original_user_prompt": prompt.original_user_prompt_event_jsonable,
            },
        )
        logger.info("TaskManager.reopen_task: re-queued %s", task_id)
        return True
```

⚠️ **行为等价的两个要点，改完必须逐条核对：**
1. 原代码是「先写 `task.original_user_prompt`（在 `if task.original_user_prompt is None:` 分支里），后拿锁」；新代码把快照挪进了 `build_reopen_prompt`（纯函数，不写 task），改由锁内统一写回。**写回必须无条件执行**（不能只在原来是 None 时写），因为 `build_reopen_prompt` 在非首次 reopen 时返回的就是 task 上已有的那份，写回是幂等的。
2. 原代码在锁**外**才计算 `original_user_prompt_jsonable` 的兜底，新代码在锁**前**（`build_reopen_prompt` 内）算完。`reopen_task` 全程无并发写 task 的路径，等价。

删掉 `task_manager.py` 文件末尾的 `_append_text_sections` 与 `_outputs_to_text`（已搬走），并删掉 `from ctx_weft.core.content import content_with_suffix`（若 ruff 报 F401）。import 区补：

```python
from ctx_weft.core.orchestrator.task_reopen import build_reopen_prompt
```

- [ ] **Step 6: 修引用旧 helper 的测试**

`tests/unit/test_task_reopen_multimodal.py` 的 `:112 :122 :129` 三处：

```python
    from ctx_weft.core.orchestrator.task_manager import _append_text_sections
```
→
```python
    from ctx_weft.core.orchestrator.task_reopen import append_text_sections as _append_text_sections
```

再全仓扫一遍：

```bash
grep -rn "_append_text_sections\|_outputs_to_text" --include=*.py src tests
```
命中的一并改。

- [ ] **Step 7: 跑 reopen 相关测试**

```bash
python -m pytest tests/unit/test_reopen.py tests/unit/test_task_reopen_multimodal.py tests/unit/test_reopen_prompt.py -v
```
Expected: 全 PASS

- [ ] **Step 8: 跑全量 + lint**

```bash
python -m pytest -q --tb=no 2>&1 | tee /tmp/now.txt | tail -3
grep "^FAILED" /tmp/now.txt | sed 's/ - .*//' | sort > /tmp/now-failures.txt
diff /tmp/baseline-failures.txt /tmp/now-failures.txt && echo "失败集合未变 ✓"
python -m ruff check src tests
```
Expected: `N+25 passed` **且** `失败集合未变 ✓`

- [ ] **Step 9: 提交**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor(orchestrator): reopen 的 prompt 改写抽成 task_reopen.py

reopen_task 的 90 行里只有约 20 行是调度（拿锁、翻状态、push、emit），
其余是 prompt 拼装 + event 侧 jsonable 的同构维护——内容操作，与队列无关，
且是本文件注释密度最高的一段。抽成纯函数后可直接单测，不必先造 TaskManager。

build_reopen_prompt / append_text_sections / outputs_to_text 三个纯函数
+ ReopenPrompt 载荷。memory 侧与 event 侧逐分支同构的论证原样保留。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: 抽出 `failure_threshold.py`

**Files:**
- Create: `src/ctx_weft/core/orchestrator/failure_threshold.py`
- Modify: `src/ctx_weft/core/orchestrator/task_manager.py`（`_trip_failure_threshold` 144 行 → ~45 行）
- Test: `tests/unit/test_failure_threshold_plan.py`（新建）

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class TripPlan:
      cancel_queued: tuple[str, ...]       # 清队：非 root 的排队条目 → CANCELED + 事件
      cancel_suspended: tuple[str, ...]    # 非 root 的 SUSPENDED → CANCELED + 事件
      signal_inflight: tuple[str, ...]     # 在途非 root → 只发协作取消信号，不发事件
      ack_task_ids: tuple[str, ...]        # 在途且带派发框 → eager ack 替换
      cancel_now_ids: tuple[str, ...]      # 已直接标 CANCELED 且已启动 → 立即整对闭合
      fail_roots: tuple[str, ...]          # 非终态 root → FAILED + 事件
      signal_roots: tuple[str, ...]        # 在途 root → 协作取消信号（先标 FAILED 之后）

  def plan_threshold_trip(
      tasks: "Mapping[str, Task]",
      *,
      pending_ids: "Sequence[str]",
      running_ids: "AbstractSet[str]",
  ) -> TripPlan
  ```

**为什么这么切（沿用 `task_disposition.py` 的既定范式）：** `_trip_failure_threshold` 的 144 行里，**分类**（哪些 task 进哪一桶）是纯逻辑、占了大半的阅读负担，而**顺序**（8 步：闩位 → 发 THRESHOLD_HIT → 取消 HITL → 清队 → 取消挂起 → 立即闭合 → root 判死 → finalizer → 会话终态）是这段代码真正的契约，必须留在有 `await` 和 `emit` 的地方。切法与 `disposition_for` 完全同构：纯函数回答「谁该怎么办」，TaskManager 负责「照办 + 发事件」。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_failure_threshold_plan.py`：

```python
"""plan_threshold_trip：熔断清场的分类判据（纯函数，不碰总线/队列）。"""

from ctx_weft.core.orchestrator.failure_threshold import TripPlan, plan_threshold_trip
from ctx_weft.core.state.models import NormalTaskSettings, Task


def _t(tid: str, *, parent: str | None = "root", status: str = "ACTIVE",
       started: bool = True) -> Task:
    from ctx_weft.core.utils import now_utc

    return Task(  # type: ignore[call-arg]
        id=tid, session_id="ses_1", status=status, parent_task_id=parent,
        settings=NormalTaskSettings(),
        started_at=now_utc() if started else None,
    )


def test_root_non_terminal_is_failed():
    tasks = {"root": _t("root", parent=None, status="SUSPENDED")}
    plan = plan_threshold_trip(tasks, pending_ids=[], running_ids=set())
    assert plan.fail_roots == ("root",)


def test_root_already_terminal_is_skipped():
    tasks = {"root": _t("root", parent=None, status="FAILED")}
    plan = plan_threshold_trip(tasks, pending_ids=[], running_ids=set())
    assert plan.fail_roots == ()


def test_queued_non_root_canceled_root_entry_dropped():
    """清队：非 root 条目取消；root 条目直接丢弃（它的去向是 root 判死）。"""
    tasks = {"root": _t("root", parent=None), "c1": _t("c1", status="PENDING")}
    plan = plan_threshold_trip(tasks, pending_ids=["root", "c1"], running_ids=set())
    assert plan.cancel_queued == ("c1",)


def test_started_queued_task_goes_to_cancel_now():
    tasks = {"c1": _t("c1", status="PENDING", started=True)}
    plan = plan_threshold_trip(tasks, pending_ids=["c1"], running_ids=set())
    assert "c1" in plan.cancel_now_ids


def test_unstarted_queued_task_not_in_cancel_now():
    """未启动过的任务从未铸框/写过 memory，跳过闭合——零 memory 写。"""
    tasks = {"c1": _t("c1", status="PENDING", started=False)}
    plan = plan_threshold_trip(tasks, pending_ids=["c1"], running_ids=set())
    assert plan.cancel_now_ids == ()


def test_suspended_non_root_canceled_and_closed():
    tasks = {"c1": _t("c1", status="SUSPENDED", started=True)}
    plan = plan_threshold_trip(tasks, pending_ids=[], running_ids=set())
    assert plan.cancel_suspended == ("c1",)
    assert "c1" in plan.cancel_now_ids


def test_inflight_non_root_gets_signal_not_event():
    """在途任务只发协作取消信号；它的 TASK_CANCELED 由 run 结束后的 apply_run_outcome 发。"""
    tasks = {"c1": _t("c1", status="ACTIVE", started=True)}
    plan = plan_threshold_trip(tasks, pending_ids=[], running_ids={"c1"})
    assert plan.signal_inflight == ("c1",)
    assert "c1" not in plan.cancel_suspended
    assert "c1" not in plan.cancel_now_ids
    # 已启动带框 → eager ack 替换
    assert plan.ack_task_ids == ("c1",)


def test_inflight_without_dispatch_frame_not_acked():
    """判据是 started_at + parent_task_id（框由 ensure_dispatch_frame_at_start 铸），
    不看瞬态的 origin_tool_call_id。"""
    tasks = {"c1": _t("c1", status="ACTIVE", started=False)}
    plan = plan_threshold_trip(tasks, pending_ids=[], running_ids={"c1"})
    assert plan.ack_task_ids == ()


def test_inflight_root_gets_signal():
    tasks = {"root": _t("root", parent=None, status="ACTIVE")}
    plan = plan_threshold_trip(tasks, pending_ids=[], running_ids={"root"})
    assert plan.fail_roots == ("root",)
    assert plan.signal_roots == ("root",)


def test_plan_is_frozen():
    import dataclasses
    import pytest

    plan = plan_threshold_trip({}, pending_ids=[], running_ids=set())
    assert isinstance(plan, TripPlan)
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.fail_roots = ()  # type: ignore[misc]
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/unit/test_failure_threshold_plan.py -v
```
Expected: `ModuleNotFoundError: No module named 'ctx_weft.core.orchestrator.failure_threshold'`

- [ ] **Step 3: 写 failure_threshold.py**

```python
"""熔断真终结（failure threshold trip）的清场分类。**纯函数，不碰总线、不碰队列。**

与 `task_disposition.py` 同一范式：这里回答「哪个 task 该落哪一桶」，
`TaskManager._trip_failure_threshold` 负责「照办 + 按定死的 8 步顺序发事件」。

顺序**不在**本模块——它是这段代码真正的契约（HitlCancelled 必须全部先于会话终态；
root 必须先标 FAILED 再对在跑的 root 发协作取消，两道守卫才接得住），必须留在有
await 和 emit 的地方。本模块只消化分类，那是 144 行里占掉大半阅读负担的部分。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AbstractSet, Mapping, Sequence

    from ctx_weft.core.state.models import Task

__all__ = ["TripPlan", "plan_threshold_trip"]

_TERMINAL = frozenset({"FINISHED", "FAILED", "CANCELED"})


def _has_dispatch_frame(t: "Task") -> bool:
    """「已启动的子任务必有框」：框由 ensure_dispatch_frame_at_start 在 start 时铸。

    不看 origin_tool_call_id——它是瞬态字段，重启重建后为 None，拿它当条件会把跨重启
    的在途子任务误判成「无框」而漏掉 ack 替换（框其实在，靠 child_task_id 认）。
    """
    return bool(t.started_at and t.parent_task_id)


@dataclass(frozen=True)
class TripPlan:
    """清场分类的结果。每个字段是一组 task id，语义见字段注释。"""

    #: 清队命中的非 root 条目 → 标 CANCELED + 发 TASK_CANCELED。
    #: root 条目直接丢弃（它的去向是 `fail_roots`，不在清队步骤发事件）。
    cancel_queued: tuple[str, ...] = ()

    #: SUSPENDED 且非 root → 标 CANCELED + 发 TASK_CANCELED。
    cancel_suspended: tuple[str, ...] = ()

    #: 在途非 root → **只发协作取消信号，不发事件**。它的 TASK_CANCELED 由 run
    #: 结束后的 `apply_run_outcome` 发；finish 对交由 `on_task_finished` 的取消
    #: 胶囊闭合 funnel 补写。
    signal_inflight: tuple[str, ...] = ()

    #: 在途且已启动带框 → 交 threshold_finalizer 做 eager ack 替换（幂等自愈）。
    #: 是 `signal_inflight` 的子集。
    ack_task_ids: tuple[str, ...] = ()

    #: 已被直接标 CANCELED（清队 + 挂起两批）且已启动 → 终态已坐实，交
    #: cancel_finalizer 立即整对闭合（ack + finish 对一次写完），不走 ack-only 半闭合。
    cancel_now_ids: tuple[str, ...] = ()

    #: 非终态的 root（parent_task_id is None）→ 标 FAILED + 发 TASK_FAILED。
    #: 已终态的 root（自己就是第 N 败，或时序尾巴已 FINISHED）不改状态、不发事件。
    fail_roots: tuple[str, ...] = ()

    #: 在途的 root（`fail_roots` 的子集）→ **标 FAILED 之后**再发协作取消信号。
    signal_roots: tuple[str, ...] = ()


def plan_threshold_trip(
    tasks: "Mapping[str, Task]",
    *,
    pending_ids: "Sequence[str]",
    running_ids: "AbstractSet[str]",
) -> TripPlan:
    """把会话里的全部 task 分类成清场动作。

    ``pending_ids``：刚从队列 drain 出来的排队条目（调用方已持锁取出）。
    ``running_ids``：当前在途（已派发、`_run_task` 未返回）的 task id。
    """
    cancel_queued: list[str] = []
    cancel_now: list[str] = []
    for tid in pending_ids:
        t = tasks.get(tid)
        if t is None or t.parent_task_id is None:
            continue
        cancel_queued.append(tid)
        if t.started_at:
            cancel_now.append(tid)

    cancel_suspended: list[str] = []
    signal_inflight: list[str] = []
    ack_tasks: list[str] = []
    for t in tasks.values():
        if t.parent_task_id is None:
            continue
        if t.status == "SUSPENDED":
            cancel_suspended.append(t.id)
            if t.started_at:
                cancel_now.append(t.id)
        elif t.id in running_ids:
            signal_inflight.append(t.id)
            if _has_dispatch_frame(t):
                ack_tasks.append(t.id)

    fail_roots: list[str] = []
    signal_roots: list[str] = []
    for t in tasks.values():
        if t.parent_task_id is not None or t.status in _TERMINAL:
            continue
        fail_roots.append(t.id)
        if t.id in running_ids:
            signal_roots.append(t.id)

    return TripPlan(
        cancel_queued=tuple(cancel_queued),
        cancel_suspended=tuple(cancel_suspended),
        signal_inflight=tuple(signal_inflight),
        ack_task_ids=tuple(ack_tasks),
        cancel_now_ids=tuple(cancel_now),
        fail_roots=tuple(fail_roots),
        signal_roots=tuple(signal_roots),
    )
```

- [ ] **Step 4: 跑新测试**

```bash
python -m pytest tests/unit/test_failure_threshold_plan.py -v
```
Expected: 10 passed

- [ ] **Step 5: `_trip_failure_threshold` 改用 plan**

`task_manager.py:941-1084`，方法体（docstring 保留，含「步骤对应机制设计『trip 序列』1-8；顺序是定案」那句）替换为：

```python
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
            "failures": [{"title": title, "reason": reason}
                         for title, reason in self._recent_failures],
        })

        # 3) 取消该 session 所有未决 pending HITL（best-effort）：HitlCancelled 须全部
        #    先于会话终态发出，防止 host 投影翻态早于 hitl 侧收尾。
        if self._hooks.cancel_pending_hitl is not None:
            try:
                await self._hooks.cancel_pending_hitl()
            except Exception:
                logger.exception("TaskManager: cancel_pending_hitl callback failed")

        # 清场分类交纯函数（见 failure_threshold.py）；顺序留在这里。
        async with self._lock:
            pending = self._queue.drain_pending()
        plan = plan_threshold_trip(
            self._tasks, pending_ids=pending, running_ids=self._running_tasks,
        )

        # 4) 清队 + 5) 取消挂起：非 root 直接标 CANCELED + 发事件。
        for tid in (*plan.cancel_queued, *plan.cancel_suspended):
            t = self._tasks[tid]
            t.status = "CANCELED"
            t.finished_at = now_utc()
            await self._emit(
                EventType.TASK_CANCELED, task_id=tid,
                payload={"reason": CancelReason.FAILURE_THRESHOLD},
            )

        # 5b) 在途非 root：只发协作取消信号，不发事件（其 TASK_CANCELED 由 run 结束后的
        #     `apply_run_outcome` 发；finish 对交由 on_task_finished 的取消胶囊闭合 funnel）。
        for tid in plan.signal_inflight:
            self._signal_cancel(tid)

        # 5.5) 立即整对闭合已终态的取消任务（清队 + 挂起，均已启动）；在途任务保持
        #      eager ack（plan.ack_task_ids）+ funnel finish 对。
        if plan.cancel_now_ids and self._hooks.cancel_finalizer is not None:
            try:
                await self._hooks.cancel_finalizer(
                    [self._tasks[tid] for tid in plan.cancel_now_ids],
                    CancelReason.FAILURE_THRESHOLD,
                )
            except Exception:
                logger.exception(
                    "TaskManager: cancel_finalizer callback failed (threshold cleanup)")

        # 6) root 判 FAILED。**先标 FAILED 再**对在跑的 root 发 cancel_inflight
        #    ——顺序保证两道守卫都接得住：task 侧是 `apply_run_outcome` 的终态守卫
        #    （不把 FAILED 盖回 CANCELED）；run 侧是 `_run_loop` 的 cancel_takes_effect
        #    （不发 RUN_CANCELED）。
        root_we_failed_and_started: Task | None = None
        for tid in plan.fail_roots:
            t = self._tasks[tid]
            t.status = "FAILED"
            t.error_code = TaskErrorCode.BY_THRESHOLD
            t.error = (f"Session failure threshold reached "
                       f"({counter} consecutive sub-task failures).")
            t.finished_at = now_utc()
            await self._emit(EventType.TASK_FAILED, task_id=tid, payload={
                "error_code": TaskErrorCode.BY_THRESHOLD,
                "error_message": t.error,
            })
            if t.started_at is not None:
                root_we_failed_and_started = t
        for tid in plan.signal_roots:
            self._signal_cancel(tid)

        # 7) finalizer：内联 await（不是后台甩），保证 memory 落盘先于 SESSION_FINISHED
        #    （SSE 关闭）；异常只记日志不阻断终结。
        if self._hooks.threshold_finalizer is not None:
            try:
                await self._hooks.threshold_finalizer(
                    root_we_failed_and_started,
                    [self._tasks[tid] for tid in plan.ack_task_ids],
                    list(self._recent_failures),
                )
            except Exception:
                logger.exception("TaskManager: threshold_finalizer callback failed")

        # 8) 会话终态 + 收尾事件。终态由 SM 据 TaskQueueDrained 落定（`_final_status()`
        #    此刻恒为 FAILED——熔断的前提就是 failure_counter 已达阈值）。
        if self._session is not None:
            self._session.status = "FAILED"
        await self._fire_session_done()
```

并在 `_agent_id_of` 附近加这个小 helper（消掉 3 处重复的 try/except）：

```python
    def _signal_cancel(self, task_id: str) -> None:
        """对在途 task 发协作取消信号（best-effort，缺注入或异常都不阻断 trip 序列）。"""
        if self._hooks.cancel_inflight is None:
            return
        try:
            self._hooks.cancel_inflight(task_id)
        except Exception:
            logger.exception("TaskManager: cancel_inflight callback failed for %s", task_id)
```

import 区补：

```python
from ctx_weft.core.orchestrator.failure_threshold import plan_threshold_trip
```

⚠️ **两处行为等价性必须逐条核对**（原代码把清队和取消挂起分成两个循环、中间没有别的动作；新代码合并成一个 `for` 但发事件顺序仍是「先清队条目、后挂起条目」，与原顺序一致）：
1. 原代码在第 4 步循环里对 `t is None` 做了 `continue`——`plan.cancel_queued` 已经把 `None` 过滤掉，`self._tasks[tid]` 必然存在。
2. 原代码第 5 步的 `for t in list(self._tasks.values())` 是**同一次遍历**里分 SUSPENDED / 在途两支；新代码由 `plan_threshold_trip` 在同一次遍历里分完，两个列表的**相对顺序**与原来一致（同一 dict 迭代序）。`cancel_now` 的追加顺序（先清队批、后挂起批）也与原代码一致。

- [ ] **Step 6: 跑熔断测试**

```bash
python -m pytest tests/unit/test_failure_threshold_trip.py tests/unit/test_threshold_finalizer.py tests/unit/test_failure_threshold_plan.py tests/unit/test_cancel_closure.py -v
```
Expected: 全 PASS

> `test_failure_threshold_trip.py` 的 docstring 明说它「覆盖 task-9-brief.md 机制设计的 _trip_failure_threshold 步骤 1-8」——这是本 Task 的核心安全网。任何一条红了都必须停下来逐字比对原实现，不要改测试去迁就。

- [ ] **Step 7: 跑全量 + lint**

```bash
python -m pytest -q --tb=no 2>&1 | tee /tmp/now.txt | tail -3
grep "^FAILED" /tmp/now.txt | sed 's/ - .*//' | sort > /tmp/now-failures.txt
diff /tmp/baseline-failures.txt /tmp/now-failures.txt && echo "失败集合未变 ✓"
python -m ruff check src tests
echo "--- task_manager.py 行数 ---"
wc -l src/ctx_weft/core/orchestrator/task_manager.py
```
Expected: `N+35 passed` **且** `失败集合未变 ✓`；`task_manager.py` 从 1575 降到 ~1350 行
（Task 7 的注释治理之后落到 ~1250-1310，见「收尾核对」的行数表——**不要期待 <1000**，
那个数字要靠拆 task 注册表 / DAG 索引，不在本计划范围）。

- [ ] **Step 8: 提交**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor(orchestrator): 熔断清场的分类抽成 failure_threshold.plan_threshold_trip

_trip_failure_threshold 的 144 行里，分类（谁进哪一桶）是纯逻辑、占了大半
阅读负担，而 8 步顺序才是这段代码真正的契约。沿用 task_disposition 的范式：
纯函数回答「谁该怎么办」，TaskManager 负责「照办 + 按定死顺序发事件」。

顺序、事件、payload 一字未动；分类判据（_has_dispatch_frame 不看瞬态的
origin_tool_call_id 等）连注释一起搬。另加 _signal_cancel 消掉 3 处重复的
cancel_inflight try/except。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: 注释治理

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/*.py`（逐文件）

**判据（唯一的一条）：** 一句注释如果只有读过某份 brief / 记得某次 review 的人才能验证它，它就该走。留下的必须是**读代码本身就能验证**的东西。

| 删 | 留 |
|---|---|
| 「Task 4 之前是 `_run_loop`」「Task 14」「批次 B」——git 记着 | 「先发事件、再 drain，因为 drain 走 create_task 不等子协程」 |
| 「见 task-9-brief.md」「总账 A5」「review round 2 finding 1」「终审 C1」——外部文档，多半已漂移 | 「空集守卫：`all([])` 恒为 True 的 vacuous-truth 防御」 |
| 「与改造前逐字节相同（已逐例核对）」——一次性验证记录 | 「outage 的 retriable 是 True，转发会让它被错误重试」 |
| 「tests/unit/test_xxx.py 测的正是这个」——测试名会变 | 「调用方须已持 `self._lock`」 |

**保留 `spec/07 §9.1` 这类引用**——那些是仍然有效的规范文档，不是流程记录。

- [ ] **Step 1: 列出候选行**

```bash
grep -rnE "Task [0-9]+|task-[0-9]+-(brief|report)|总账 [A-Z][0-9]|review round|终审|评审|批次 [A-Z]" \
  src/ctx_weft/core/orchestrator/*.py > /tmp/comment-candidates.txt
wc -l /tmp/comment-candidates.txt
cat /tmp/comment-candidates.txt
```

Expected: ~60 行候选（`task_manager.py` 占大头）

- [ ] **Step 2: 逐文件处理，从小到大**

顺序：`task_runner.py`（2 处）→ `task_disposition.py`（2）→ `session_registry.py`（5）→ `agent_lifecycle_manager.py`（10）→ `task_manager.py`（36）。

每处的操作是**改写而非整段删除**：把「Task N 之前是 X，现在是 Y」压成「是 Y，因为 Z」。举例——

`task_disposition.py` 的 `disposition_for` docstring 里：

```
    与今天三处实际口径逐字对照（见 task-1-report.md）：
    - `TaskManager._handle_task_failure`：非 retriable 的运行层异常直接挂起...
```
→
```
    三条判据的来源与不变量：
    - 非 retriable 的运行层异常直接挂起（跳过重试判断）；retriable 且
      `retry_count < max_retries` 才原地重试，payload 里的 retry_count 是
      **已加 1** 的新值（处置表不 mutate，写回由 apply_run_outcome 负责）。
```

`task_manager.py` 的 `_handle_task_failure` 终态守卫分支那段 20 行注释——保留「不摘队列簿记会永久少一个并发槽位」和「不再调 is_done()/_fire_session_idle 的三条理由」，删掉「（tests/unit/test_terminal_guard_on_assembly_failure.py 的 test_terminal_guard_still_clears_running_bookkeeping 测的正是这个）」。

- [ ] **Step 3: 每处理完一个文件就跑一次全量**

```bash
python -m pytest -q 2>&1 | tail -3
```

注释改动不该影响任何测试——**包括 Task 1 加的那条 `test_no_inline_terminal_triple_outside_the_single_definition`**，它按「非注释行」过滤，改注释不影响它。若它红了，说明误删/误改了代码行，立刻 `git diff` 查。

- [ ] **Step 4: 复测散文占比**

```bash
python - <<'EOF'
import ast, io, pathlib, tokenize
for p in sorted(pathlib.Path('src/ctx_weft/core/orchestrator').glob('*.py')):
    src = p.read_text(encoding='utf-8')
    comment = sum(1 for t in tokenize.generate_tokens(io.StringIO(src).readline)
                  if t.type == tokenize.COMMENT)
    dsl = 0
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            d = ast.get_docstring(n, clean=False)
            if d:
                dsl += d.count('\n') + 1
    total = src.count('\n') + 1
    print(f'{p.name:32s} total={total:5d} prose={100*(comment+dsl)/total:5.1f}%')
EOF
```

目标：`task_manager.py` 从 38.5% 降到 30% 以下且**行数不增**。不追求某个具体数字——判据是 Step 0 那条，不是百分比。

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "$(cat <<'EOF'
docs(orchestrator): 清掉变更史注释，保留不变量说明

删的是只有读过某份 brief / 记得某次 review 才能验证的话：
「Task N 之前是 X」「见 task-9-brief.md」「总账 A5」「review round 2 finding 1」
「tests/unit/test_xxx.py 测的正是这个」。git 和测试本身记着这些。

留的是读代码就能验证的：顺序为什么这么排、守卫在防什么、调用方须持什么锁、
某个默认值方向为什么是刻意反的。spec/ 引用照留——那是仍然有效的规范。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## 收尾核对

- [ ] **通过数 = `N+35`，失败集合与 Task 0 记录的 8 条逐字相同**（`diff /tmp/baseline-failures.txt /tmp/now-failures.txt` 无输出）
- [ ] **lint 干净**：`python -m ruff check src tests`
- [ ] **无残留旧路径**：
  ```bash
  grep -rn "orchestrator\.\(capability_cache\|capability_resolver\|control_capability\|skill_executor_capability\)" --include=*.py src tests
  grep -rn "ORCHESTRATOR_SESSION_MANAGER\|_task_payload\|_append_text_sections" --include=*.py src tests
  ```
  Expected: 全部无输出
- [ ] **包结构如下：**
  ```
  core/capabilities/          __init__ cache resolver control_tools skill_executor
  core/events.py              ← 新建：全仓事件封套的唯一构造点（叶子模块）
  core/orchestrator/          __init__
                              task_queue task_disposition task_runner task_manager
                              task_reopen failure_threshold
                              session_registry agent_state agent_lifecycle_manager template_lookup
  ```
- [ ] **行数对照：**
  | 文件 | 前 | 后（目标） |
  |---|---|---|
  | `orchestrator/` 合计 | 4389 | ~3000（含 3 个新文件 hooks/task_reopen/failure_threshold；emitter 改为 `core/events.py`，不计入本包） |
  | `task_manager.py` | 1575 | **~1250-1310** |
  | `capabilities/` 合计 | — | ~1180 |

---

## 本计划**不**做的事（记在这里，免得下一个人以为漏了）

1. **`TaskManager` 仍持有 task 注册表 + parent/child DAG 索引 + staged 缓冲。** 这三块与队列驱动耦合得很紧（`_try_resume_parent` 同时读 `_children_of` / `_tasks` / `_queue` 且要在同一临界区内），拆它需要先想清楚锁的边界，属于另一次重构。
2. **`runtime.py`（3465 行）没动。** 它比 orchestrator 还大，但不在本次范围。
3. **`session_registry.py` 与 `agent_lifecycle_manager.py` 的职责边界没重划。** 前者「只是登记表」、后者「是 agent 身份与配置的唯一住所」，两份 docstring 都写得清楚，暂时没有冲突证据。
4. **`core/errors.py` → `task_disposition` 的方向依赖保留**（见 Global Constraints §7）。
