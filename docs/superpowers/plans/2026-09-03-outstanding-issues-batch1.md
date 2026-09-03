# 遗留问题批次一 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按遗留问题总账的推荐顺序，修掉两条数据损失、一条取消语义模糊、observe 的机械摘要，并把判别值收敛成枚举。

**Architecture:** 七个任务，前两个各修一条数据损失（投影里 task 成果恒空、agent 模型选择不进快照），第三个补取消来源的可辨识性，第四个按用户裁定重做 observe 的非 LLM 路径（机械判决 + 转 background observe，禁止任何机械合成摘要），第五个把 `reason` / `error_code` 收敛成 `StrEnum`，第六个把越界的事件发射搬回 TaskManager 并收紧守卫，第七个清死值域并补 observe 的起点事件。

**Tech Stack:** Python 3.12+，事件溯源（`EventType` / reducer / `*View` 投影 / 快照），pytest，ruff。

**Spec:** `docs/follow-ups/2026-09-03-outstanding-issues.md`（本计划逐条实现其「建议的处理顺序」）

## Global Constraints

- **行为等价优先**：除本计划显式裁定的修复外，**不改变任何 task 的最终状态转移结果**。
- 全量基线：`./.venv/Scripts/python.exe -m pytest tests/unit -q` **恰好 1 条既有失败**
  `tests/unit/test_observe_outcomes.py::test_default_role_prompt_uses_two_fields`。
- golden 基线：`./.venv/Scripts/python.exe -m pytest tests/unit/test_golden_conformance.py -q`
  → **31 collected / 31 passed / 0 skipped**。
- 已知 flake（撞上单独重跑确认）：`test_dispatch_boundary_recap_e2e`、
  `test_finish_plus_delegate_same_batch_e2e`、`test_hitl_multimodal_validation.py`。
- 新代码 `from __future__ import annotations`；ruff line-length 100；
  既有 RUF001/RUF002/RUF003（中文标点）是基线，不用管。
- 测试一律用仓内 venv：`./.venv/Scripts/python.exe -m pytest ...`
- **不要读 `docs/events-v2.md` 当事实来源** —— 它含「已定案、未实施」的内容（总账 E9）。
- **payload 一律从实际发射点抄**，不许从相邻事件或文档推断。本仓在这上面栽过。

## 用户已裁定的两个设计决定

1. **A1 的修法**：改 reducer 从 `TaskFinished` / `TaskFailed` / `TaskInterrupted` 读，
   **不**给 `TaskFinalized` 补 payload。理由是存量可重建性 —— 存量日志里
   `TaskFinished` 本来就带 `outputs`，改 reducer 让历史事件也能正确重放；
   补发射侧只对新事件有效。
2. **observe 的非 LLM 路径**：判决用规则出，**摘要一律不许机械合成**。
   `_rule_observe` 整个换成「机械判决 + 转 background observe」。

3. **取消在 observe 里的语义**：observe / finalize **不可中断是有意设计**
   （它们是「整理现状」与「闭合」，半途中止会留下既无判决、也没整理干净记忆的 task）。

   这里有个**必须分清的区别**，两者只差一个词但结论相反：

   | | 做不做 | 含义 |
   |---|---|---|
   | **检查点** | **禁止** | 执行途中 `raise_if_cancelled()`，把 step **打断在半路** |
   | **入口读 token 选路径** | **正是要做的**（Task 4） | observe **照常走完**，只是不再烧多轮 LLM |

   即：取消到达时，observe 降级走机械判决那条路、摘要交 background observe，
   **但它仍然跑完并交出 verdict**。用户已按下取消却还要等几轮 LLM，体验上说不过去；
   而中途甩手不管，比多等几轮更糟。

   background observe 不影响主进程，取消时照跑。

---

## File Structure

| 文件 | 责任 | 涉及任务 |
|---|---|---|
| `src/ctx_weft/core/control/reducers.py` | 事件 → 投影；快照序列化 | T1, T2 |
| `tests/unit/test_task_outputs_projection.py` | 新建：成果物折进投影的端到端 | T1 |
| `tests/unit/test_view_serialization_coverage.py` | 新建：快照必须覆盖 View 全字段 | T2 |
| `src/ctx_weft/core/runtime.py` | run 循环、取消、HITL 注入 | T3, T6 |
| `src/ctx_weft/core/loop/steps/observe.py` | observe 判决与路由 | T4, T7 |
| `src/ctx_weft/core/discriminators.py` | 新建：`reason` / `error_code` 枚举 | T5 |
| `src/ctx_weft/core/orchestrator/task_manager.py` | task 状态与事件唯一发射者 | T5, T6 |
| `tests/unit/test_task_manager_owns_status.py` | 两道静态守卫 | T6 |
| `src/ctx_weft/core/state/models.py` | 值域 | T7 |
| `src/ctx_weft/protocols/events.py` | 事件枚举 | T7 |

---

## Task 1: 成果物折进投影 —— reducer 改从 TaskFinished/TaskFailed 读

**Files:**
- Modify: `src/ctx_weft/core/control/reducers.py`（`TASK_FINALIZED` 分支 + `TASK_STATUS_BY_EVENT` 分支体）
- Modify: `docs/spec/golden/14-task-failed.json`（那份伪造的 payload）
- Modify: `tests/integration/test_task_recap_recovery.py`、`tests/unit/test_hitl_recovery_v2.py`、`tests/unit/test_task_unblock_events.py`（四处虚假绿灯）
- Test: `tests/unit/test_task_outputs_projection.py`（新建）

**Interfaces:**
- Produces: `TaskView.outputs` / `TaskView.error` 现在由 task 终态事件填充，后续任务不依赖此变更。

**背景（必读）**：今天 `TaskFinished.payload` **带 `outputs`**
（`task_disposition.py:123-125`），但 reducer 的 `TASK_FINISHED` 走
`elif t in TASK_STATUS_BY_EVENT`（`reducers.py:555`），**只写 status、不读 payload**。
而 `TASK_FINALIZED` 分支（`reducers.py:577-582`）**读** `outputs`/`error`，
发射侧却只发 `{task_id, outcome}`（`finalize.py:766`）→ 永远读到 `None`，
且这是投影里 `outputs` 的**唯一非 None 写入点**。

- [ ] **Step 1: 写失败的测试**

新建 `tests/unit/test_task_outputs_projection.py`：

```python
"""task 的成果物与死因必须能从事件流折进投影（总账 A1）。"""

from __future__ import annotations

from ctx_weft.core.control.reducers import reduce_events
from ctx_weft.protocols.events import Event, EventType
from ctx_weft.shared.ids import generate_id
from ctx_weft.shared.time import now_utc


def _ev(t: EventType, payload: dict, *, task_id: str = "tsk_1") -> Event:
    return Event(
        id=generate_id("evt"), run_id=None, sequence=0,
        session_id="sess_1", type=t, timestamp=now_utc(),
        tenant_id="default", task_id=task_id, payload=payload,
    )


def _created() -> Event:
    return _ev(EventType.TASK_CREATED, {"task": {
        "id": "tsk_1", "session_id": "sess_1", "status": "PENDING", "title": "t",
        "description": "", "creator_agent_id": "", "assigned_agent_id": "",
        "parent_task_id": None, "user_prompt": "p", "priority": 0, "max_retries": 3,
        "timeout_ms": 0, "dag_deps": [], "interaction_mode": "", "settings": {},
        "origin_tool_call_id": None, "origin_tool_name": None,
        "result": None, "outputs": {}, "error": None,
        "created_at": None, "updated_at": None,
    }})


def test_task_finished_folds_outputs_into_view():
    view = reduce_events([
        _created(),
        _ev(EventType.TASK_FINALIZED, {"task_id": "tsk_1", "outcome": "success"}),
        _ev(EventType.TASK_FINISHED, {
            "outcome": "success", "summary": "done", "outputs": {"result": "ok"},
        }),
    ], "run_1")
    assert view.tasks["tsk_1"].outputs == {"result": "ok"}
    assert view.tasks["tsk_1"].status == "FINISHED"


def test_task_failed_folds_error_message_into_view():
    view = reduce_events([
        _created(),
        _ev(EventType.TASK_FINALIZED, {"task_id": "tsk_1", "outcome": "fail"}),
        _ev(EventType.TASK_FAILED, {
            "error_code": "TASK_FAILED_BY_OBSERVER",
            "error_message": "boom", "retry_count": 0,
        }),
    ], "run_1")
    assert view.tasks["tsk_1"].error == "boom"
    assert view.tasks["tsk_1"].status == "FAILED"


def test_task_interrupted_folds_error_message_into_view():
    view = reduce_events([
        _created(),
        _ev(EventType.TASK_INTERRUPTED, {
            "reason": "run_crash", "error_code": "CONTEXT_OVERFLOW",
            "error_message": "window exceeded", "retry_count": 1,
        }),
    ], "run_1")
    assert view.tasks["tsk_1"].error == "window exceeded"


def test_task_finalized_no_longer_wipes_outputs():
    """TaskFinalized 在 TaskFinished 之前到达，不得把已折的成果清空。"""
    view = reduce_events([
        _created(),
        _ev(EventType.TASK_FINISHED, {
            "outcome": "success", "summary": "s", "outputs": {"a": 1},
        }),
        _ev(EventType.TASK_FINALIZED, {"task_id": "tsk_1", "outcome": "success"}),
    ], "run_1")
    assert view.tasks["tsk_1"].outputs == {"a": 1}


def test_task_finalized_still_sets_finished_at():
    view = reduce_events([
        _created(),
        _ev(EventType.TASK_FINALIZED, {"task_id": "tsk_1", "outcome": "success"}),
    ], "run_1")
    assert view.tasks["tsk_1"].finished_at is not None
```

- [ ] **Step 2: 跑测试确认它按预期失败**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_task_outputs_projection.py -v`
Expected: 前三条 FAIL（`outputs`/`error` 是 `None`），后两条 PASS。

- [ ] **Step 3: 改 reducer**

在 `reducers.py` 的 `elif t in TASK_STATUS_BY_EVENT` 分支体里（现有 `failure_counter`
折叠那段附近），按事件类型补成果物折叠。**注意顺序**：先写 status（既有逻辑），
再折 payload。改动后该分支体应包含：

```python
        # 成果物与死因：数据在 task 终态事件的 payload 里（总账 A1）。
        # TaskFinalized 从不发这两个键，故不能挂在它上面读。
        if t == EventType.TASK_FINISHED:
            if "outputs" in p:
                task.outputs = p.get("outputs")
        elif t in (EventType.TASK_FAILED, EventType.TASK_INTERRUPTED):
            msg = p.get("error_message")
            if msg:
                task.error = msg
```

同时把 `TASK_FINALIZED` 分支（`reducers.py:577-582`）里那两行**删掉**，只留 `finished_at`：

```python
    elif t == EventType.TASK_FINALIZED and ev.task_id:
        task = view.tasks.get(ev.task_id)
        if task is not None:
            # outputs/error 不在本事件的 payload 里（发射侧只发 task_id/outcome）——
            # 它们由 TaskFinished/TaskFailed/TaskInterrupted 折入，见总账 A1。
            task.finished_at = ev.timestamp
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_task_outputs_projection.py -v`
Expected: 5 passed。

- [ ] **Step 5: 修四处虚假绿灯的测试**

这四处手工构造了带 `outputs` 的 `TASK_FINALIZED`（一份 emitter 从不生产的 payload）：
`tests/integration/test_task_recap_recovery.py:95`、`:180`、
`tests/unit/test_hitl_recovery_v2.py:199-200`、`tests/unit/test_task_unblock_events.py:77`。

逐处改成用真实的终态事件携带数据。**不要只把断言删掉** —— 它们验证的
「成果物能折进投影」这件事是对的，错的是它们喂的 payload。
`test_hitl_recovery_v2.py:199` 那条注释「TASK_FINALIZED 是唯一把 outputs 折进
TaskView 的事件」也要一并订正。

- [ ] **Step 6: 修 golden 14 的伪造 payload**

`docs/spec/golden/14-task-failed.json` 里 `TaskFinalized` 的 payload
`{"outputs": null, "error": "boom: tool crashed"}` **现实中不存在**。
改成发射侧真实的 `{"task_id": ..., "outcome": ...}`，并让 `error` 由该用例里的
`TaskFailed` 事件携带（若该用例没有 `TaskFailed`，补一条，payload 从
`task_disposition.py:132-137` 抄）。

- [ ] **Step 7: 全量 + golden**

```
./.venv/Scripts/python.exe -m pytest tests/unit -q --tb=no
./.venv/Scripts/python.exe -m pytest tests/unit/test_golden_conformance.py -q
```
Expected: 全量恰好 1 条既有失败；golden 31/31/0 skipped。

- [ ] **Step 8: 提交**

```bash
git add -A && git commit -m "fix(projection): 成果物与死因改从 task 终态事件折入投影"
```

---

## Task 2: 快照覆盖 AgentView 全字段 + 补结构性守卫

**Files:**
- Modify: `src/ctx_weft/core/control/reducers.py`（`serialize_view` / `deserialize_view` 的 agents 段）
- Test: `tests/unit/test_view_serialization_coverage.py`（新建）

**Interfaces:**
- Consumes: 无（独立于 Task 1）
- Produces: 快照往返后 `AgentView.llm_account` / `llm_model` 保持不变。

**背景（必读）**：`serialize_view` 的 agents 段（`reducers.py:192-200`）只写 4 个字段，
`deserialize_view`（`:256-264`）只读 4 个 —— `llm_account` / `llm_model` **不进快照**。
后果：只要该 session 有快照，`rebuild_view` 走「快照 + delta」路径，
delta 里若无新的 `AgentInstantiated`/`AgentLlmChanged`，两个字段就是 `""`
→ `AgentRegistry.load` 建出 `ModelChoice("", "")` → **静默回落账号默认**，
使刚做完的「模型选择跨重启存活」失效。快照每 50 条非瞬态事件就落一次。

**同时修 B3**：没有任何测试保证 `serialize_view` 覆盖 View 的全部字段 ——
这是本缺陷能存在的**结构性原因**，只修字段不补守卫，下次加字段还会再漏。

- [ ] **Step 1: 写失败的测试**

新建 `tests/unit/test_view_serialization_coverage.py`：

```python
"""快照必须覆盖每个 View 的全部字段（总账 A2 / B3）。

serialize_view / deserialize_view 是手写字典字面量，没有 dataclasses.fields()
遍历 —— 漏一个字段完全静默。这道守卫把「漏」变成红灯。
"""

from __future__ import annotations

import dataclasses

from ctx_weft.core.control.reducers import deserialize_view, serialize_view
from ctx_weft.core.control.types import AgentView, RunStateView, SessionView, TaskView

#: 有意不进快照的字段 —— 每条都要写明理由。
_EXEMPT: dict[str, set[str]] = {
    "RunStateView": {
        "sessions", "tasks", "agents",   # 容器，各自单独序列化
        "target_event_id", "events_replayed",  # replay 专用，不属于快照状态
        "extra", "snapshot_at",          # 死字段（总账 D3），待清理
    },
}


def _field_names(cls: type) -> set[str]:
    return {f.name for f in dataclasses.fields(cls)}


def _make_view() -> RunStateView:
    view = RunStateView(run_id="run_1")
    view.sessions["sess_1"] = SessionView(id="sess_1")
    view.tasks["tsk_1"] = TaskView(id="tsk_1", session_id="sess_1")
    view.agents["agt_1"] = AgentView(
        id="agt_1", spawn_depth=1, parent_agent_id="agt_0",
        template_id="tpl", llm_account="acct", llm_model="mdl",
    )
    return view


def test_serialize_covers_every_agent_view_field():
    blob = serialize_view(_make_view())
    written = set(blob["agents"]["agt_1"])
    missing = _field_names(AgentView) - written - _EXEMPT.get("AgentView", set())
    assert missing == set(), f"AgentView 字段没进快照: {sorted(missing)}"


def test_serialize_covers_every_task_view_field():
    blob = serialize_view(_make_view())
    written = set(blob["tasks"]["tsk_1"])
    missing = _field_names(TaskView) - written - _EXEMPT.get("TaskView", set())
    assert missing == set(), f"TaskView 字段没进快照: {sorted(missing)}"


def test_serialize_covers_every_session_view_field():
    blob = serialize_view(_make_view())
    written = set(blob["sessions"]["sess_1"])
    missing = _field_names(SessionView) - written - _EXEMPT.get("SessionView", set())
    assert missing == set(), f"SessionView 字段没进快照: {sorted(missing)}"


def test_serialize_covers_every_run_state_view_field():
    blob = serialize_view(_make_view())
    missing = _field_names(RunStateView) - set(blob) - _EXEMPT["RunStateView"]
    assert missing == set(), f"RunStateView 字段没进快照: {sorted(missing)}"


def test_agent_llm_choice_survives_snapshot_round_trip():
    """A2 的正面表述：模型选择必须过得了快照往返。"""
    rebuilt = deserialize_view(serialize_view(_make_view()))
    agent = rebuilt.agents["agt_1"]
    assert agent.llm_account == "acct"
    assert agent.llm_model == "mdl"


def test_round_trip_preserves_all_agent_fields():
    original = _make_view().agents["agt_1"]
    rebuilt = deserialize_view(serialize_view(_make_view())).agents["agt_1"]
    assert dataclasses.asdict(rebuilt) == dataclasses.asdict(original)
```

- [ ] **Step 2: 跑测试确认它按预期失败**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_view_serialization_coverage.py -v`
Expected: `test_serialize_covers_every_agent_view_field`、
`test_agent_llm_choice_survives_snapshot_round_trip`、
`test_round_trip_preserves_all_agent_fields` 三条 FAIL。
若 Task/Session/RunStateView 那三条也红了，**停下来报告** —— 说明还有别的字段没进快照，
那是本任务范围之外的新发现，需要控制方裁定。

- [ ] **Step 3: 补 serialize_view 的 agents 段**

`reducers.py:192-200`：

```python
        "agents": {
            aid: {
                "id": a.id,
                "spawn_depth": a.spawn_depth,
                "parent_agent_id": a.parent_agent_id,
                "template_id": a.template_id,
                "llm_account": a.llm_account,
                "llm_model": a.llm_model,
            }
            for aid, a in view.agents.items()
        },
```

- [ ] **Step 4: 补 deserialize_view 的 agents 段**

`reducers.py:256-264`。**默认值必须能吃下旧快照**（旧快照没有这两个键）：

```python
        agents[aid] = AgentView(
            id=a["id"],
            spawn_depth=a.get("spawn_depth", 0),
            parent_agent_id=a.get("parent_agent_id"),
            # 旧快照无该键 → 留空，调用方回落 session 模板（零数据迁移）。
            template_id=a.get("template_id", ""),
            # 同上：旧快照无模型选择 → 空 ModelChoice，回落账号默认。
            llm_account=a.get("llm_account", ""),
            llm_model=a.get("llm_model", ""),
        )
```

- [ ] **Step 5: 跑测试确认通过**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_view_serialization_coverage.py -v`
Expected: 6 passed。

- [ ] **Step 6: 做负向对照**

临时从 `serialize_view` 的 agents 段里删掉 `"llm_model"` 那一行，跑守卫，
确认 `test_serialize_covers_every_agent_view_field` **变红**且报出 `llm_model`；
然后 `git checkout -- src/ctx_weft/core/control/reducers.py` 撤销，
确认 `git status --porcelain` 只剩你自己的改动。把这个过程写进报告。

**这一步不能跳** —— 本仓的守卫出过两次「看起来在守、实则漏」，两次都是负向对照才发现的。

- [ ] **Step 7: 全量 + golden**

Expected: 全量恰好 1 条既有失败；golden 31/31/0。

- [ ] **Step 8: 提交**

```bash
git add -A && git commit -m "fix(snapshot): AgentView 的模型选择进快照 + 补覆盖全字段的守卫"
```

---

## Task 3: 让 `_run_loop` 分得清取消来源

**Files:**
- Modify: `src/ctx_weft/core/runtime.py`（`_run_loop` 的 `except asyncio.CancelledError` 分支）
- Test: `tests/unit/test_cancel_end_to_end.py`（既有文件，追加）

**Interfaces:**
- Consumes: 无
- Produces: `RunOutcome(kind=CANCELED).reason` 现在区分 `"token"` 与 `"external"`。

**背景与范围收窄（必读）**：总账 A3 原本还包含「给 observe 加取消检查点」与
「finalize 期间取消被丢弃」两条，**都已撤销**：

- observe / finalize **不可中断是有意设计** —— 它们是「整理现状」与「闭合」，
  半途中止会留下既无判决、也没整理干净记忆的 task。**不得给它们加检查点。**
  （**注意与 Task 4 区分**：Task 4 在 observe **入口**读一次 token 来选路径，
  那不是检查点 —— observe 仍然跑完。见「用户已裁定」第 3 条的对照表。
  本任务**完全不碰 observe**。）
- 「取消被完全丢弃」是**判重了**：`cancel_session` 先调 `cancel_all`
  （清队 + 写 `session.status="CANCELED"` + 透传 SM 发 `SessionFinished{CANCELED}`），
  会话终态正确。真正的差异只是那个正在 finalize 的 task 报 FINISHED 而非 CANCELED，
  而它确实把活干完了 —— 可辩护，不修。

**只剩这一条**：`_run_loop` 的 `except asyncio.CancelledError` 只看 `task.status`
（`runtime.py:2518`），**从不看 token**。token 取消与外部 asyncio 取消
（进程 shutdown / `wait_for` 超时）产生的是同一个 `CancelledError`，处置上无法区分。

- [ ] **Step 1: 写失败的测试**

追加到 `tests/unit/test_cancel_end_to_end.py`。先读该文件既有的搭台手法
（它已有真 `_run_loop` + 真 `_run_task` 的端到端用例），照其风格写：

```python
async def test_token_cancel_is_labelled_token():
    """CancelToken 触发的取消，RunOutcome.reason 标 token。"""
    # 用既有搭台造一个会在 step 边界被 token 取消的 run
    state = await _run_until_token_cancel()
    assert state.run_outcome.kind is RunOutcomeKind.CANCELED
    assert state.run_outcome.reason == "token"


async def test_external_cancel_is_labelled_external():
    """非 token 来源的 CancelledError（如进程 shutdown）标 external。"""
    state = await _run_with_external_cancelled_error()
    assert state.run_outcome.kind is RunOutcomeKind.CANCELED
    assert state.run_outcome.reason == "external"
```

`_run_until_token_cancel` / `_run_with_external_cancelled_error` 两个 helper 要你自己写：
前者构造 `CancelToken` 并在某个 step 前 `cancel()`；后者用一个直接抛
`asyncio.CancelledError` 的替身 driver（**不** cancel token）。

- [ ] **Step 2: 跑测试确认失败**

Expected: 两条都 FAIL —— 今天 `reason` 恒为空串。

- [ ] **Step 3: 改 `_run_loop`**

`runtime.py:2511-2519` 的 `except asyncio.CancelledError` 分支：

```python
        except asyncio.CancelledError:
            was_cancelled = True
            # 取消来源：token 是本 runtime 的协作取消；否则是外部 asyncio 取消
            # （进程 shutdown / wait_for 超时）。两者产生同一个 CancelledError，
            # 只有 token 自己能区分（总账 A3）。
            by_token = cancel_token is not None and cancel_token.is_cancelled
            cancel_takes_effect = task.status not in ("FINISHED", "FAILED", "CANCELED")
            state = state.apply_patch({"run_outcome": RunOutcome(
                kind=RunOutcomeKind.CANCELED,
                reason="token" if by_token else "external",
            )})
```

**注意**：`cancel_token` 是 `_run_loop` 的形参，确认它在该作用域可见；
若不可见则从 `ctx` 取（`loop_ctx.cancel_token`），**以实测为准**。

- [ ] **Step 4: 确认 payload 没有跟着变**

`disposition_for` 的 CANCELED 支在 `reason` 非空时会**放 `reason` 键**
（`task_disposition.py:98-103`）—— 而今天 `TaskCanceled` 的 payload 是字面 `{}`。
本改动会让它变成 `{"reason": "token"}`。

**这是对外 payload 变更，必须确认是否可接受**：
跑一遍 golden，若 `13-task-canceled.json` 变红，**停下来报告**，
不要直接改 golden 迁就 —— 那需要控制方裁定（要么接受并更新 golden 与升级须知，
要么让 `_run_loop` 只把来源记进日志、不进 `RunOutcome.reason`）。

- [ ] **Step 5: 全量 + golden**

- [ ] **Step 6: 提交**

```bash
git add -A && git commit -m "fix(cancel): _run_loop 区分 token 取消与外部取消"
```

---

## Task 4: observe 的非 LLM 路径 —— 机械判决 + 转 background observe

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/observe.py`（删 `_rule_observe`，改 `execute` 的分流）
- Test: `tests/unit/test_observe_outcomes.py`（既有，改断言）+ 新增用例

**Interfaces:**
- Consumes: `launch_background_observe(state, ctx, *, boundary: str)`
  （`core/loop/steps/background_observe.py:346`）
- Produces: `ObserveStep.execute` 的非 LLM 路径不再产出任何合成文本。

**用户裁定（必读，这是本任务的全部依据）**：

> 摘要部分换成 background observe，判决可以用规则出，但是**后续不应当允许任何机械产生的摘要**。
> rule observe 环节可以直接换成机械出判决 + background observe。

**已核实的事实**：

1. **全仓机械合成摘要只有 `_rule_observe` 这一处**（`observe.py:380-398` 那 5 行文本：
   `"[No actor execution recorded]"` / `"Ran N conversation round(s)."` /
   `"Tools used: ..."` / `"No tools were called."` / `"Task completed."`）。
   删掉它，这条约束即满足，不用再去别处清扫。
2. `_fold_retry_segment` **已经是合规的**：注释明写「空则不折、段保 raw
   （**不写占位摘要**）」（`observe.py:447`）。摘要为空时它安全降级。
3. **时序过得去**：`runtime.py:2483` 在 run 入口
   `await await_pending_background_observe(task.id)` —— 新 run 开跑前必等 recap 完成。
   所以段折从「前台同步」变成「后台异步」，下一轮 prepare 仍看得到折后段摘要。
4. `_should_use_llm` 返回 False 的三种场景（`observe.py:419-433`）：
   无 observe ROLE、root task（无 parent）、以及 `_llm_observe` 抛异常的兜底。
   **root task 今天本来就走 `_rule_observe` + `launch_background_observe`** ——
   即真正的整理早就在 background observe 里做了，`_rule_observe` 只是兼着产廉价判决。

- [ ] **Step 1: 先验证地基 —— background observe 不依赖 observe ROLE**

本任务的前提是「无 observe ROLE 时转 background observe 仍产得出真 recap」。
`_run_background_observe` 用的是 `purpose="background_observe"`
（`background_observe.py:258`）这个独立装配口径，看起来不读 observe identity。

**去核实**：读 `_run_background_observe` 的装配链（`ContextRequest(purpose=..., template=...)`
→ assembler），确认无 observe ROLE 时它能正常产出，而不是静默空转或抛错。

**若实测发现它确实依赖 observe ROLE，停下来报告** —— 那本任务的设计前提不成立，
需要控制方重新裁定。

- [ ] **Step 2: 写失败的测试**

在 `tests/unit/test_observe_outcomes.py` 追加：

```python
async def test_non_llm_path_emits_no_synthetic_summary():
    """机械判决路径不得产出任何合成摘要文本（用户裁定）。"""
    state = _state_without_observe_role()
    outcome = await ObserveStep().execute(state, _ctx())
    verdict = outcome.state_patch["verdict"]
    assert verdict.act_recap == ""


async def test_non_llm_path_launches_background_observe(monkeypatch):
    """摘要改由 background observe 产。"""
    launched: list[str] = []
    monkeypatch.setattr(
        "ctx_weft.core.loop.steps.background_observe.launch_background_observe",
        lambda state, ctx, *, boundary: launched.append(boundary),
    )
    state = _state_without_observe_role()
    await ObserveStep().execute(state, _ctx())
    assert launched, "非 LLM 路径必须转 background observe"


async def test_mechanical_verdict_preserves_today_outcomes():
    """判决逐字不变：机械退出 → retry；正常/actor_done → success；空 transcript → fail。"""
    for exit_reason, expected in [
        ("max_turns", "retry"), ("context_limit", "retry"),
        ("normal", "success"), ("actor_done", "success"),
    ]:
        state = _state_without_observe_role(act_exit_reason=exit_reason)
        outcome = await ObserveStep().execute(state, _ctx())
        assert outcome.state_patch["verdict"].task_outcome == expected

    empty = _state_without_observe_role(transcript=[])
    outcome = await ObserveStep().execute(empty, _ctx())
    assert outcome.state_patch["verdict"].task_outcome == "fail"


async def test_cancelled_run_skips_llm_observe(monkeypatch):
    """取消时不跑 LLM ReAct，走机械判决 + background observe（observe 本身不中止）。"""
    called = False

    async def _never(*a, **kw):
        nonlocal called
        called = True

    monkeypatch.setattr(ObserveStep, "_llm_observe", _never)
    state = _state_with_observe_role()
    ctx = _ctx(cancel_token=_already_cancelled_token())
    outcome = await ObserveStep().execute(state, ctx)
    assert not called, "取消后不应再跑多轮 LLM observe"
    assert outcome.next_step == "finalize", "observe 仍须走完，不得中止"
```

`_state_without_observe_role` / `_state_with_observe_role` / `_already_cancelled_token`
按该文件既有搭台手法写。

- [ ] **Step 3: 跑测试确认失败**

- [ ] **Step 4: 删 `_rule_observe`，换成机械判决**

删掉整个 `_rule_observe` 方法（`observe.py:371-410` 附近，含它对
`_apply_assessment` 的调用），换成一个不产文本的纯判决函数：

```python
    def _mechanical_verdict(self, state: LoopState) -> Verdict:
        """无 LLM observer 时的判决：只定结局，**不产摘要**。

        摘要由 background observe 异步产出（用户裁定：不允许任何机械合成的摘要）。
        act_recap 留空——`_fold_retry_segment` 对空摘要的口径是「不折、段保 raw」
        （见其 docstring），不会写占位。

        三条映射逐字对齐删除前的 `_rule_observe` 结局，故 task 终态不变：
          空 transcript                      → fail
          max_turns / context_limit（机械退出）→ retry
          normal / actor_done                → success
        """
        if not state.transcript:
            return Verdict(task_outcome="fail", act_recap="")
        if state.act_exit_reason in ("max_turns", "context_limit"):
            return Verdict(task_outcome="retry", act_recap="")
        return Verdict(task_outcome="success", act_recap="")
```

**注意**：删掉的 `_rule_observe` 里有一句 `self._apply_assessment(state.task, verdict)`。
按 task 状态收口的不变量，**judgment 不得写 task 状态** —— 确认删除后没有别处依赖
那个副作用（`_apply_assessment` 今天已被清空成不写 status，见 `observe.py:411` 的注释）。
若发现有依赖，停下来报告。

- [ ] **Step 5: 改 `execute` 的分流**

`observe.py:246-260`：

```python
        # 取消时不跑多轮 LLM observe：observe 是「整理现状」，不中止（用户裁定），
        # 但也不该在用户已按下取消后再烧几轮 LLM——降级走机械判决，摘要交后台。
        tok = getattr(ctx, "cancel_token", None)
        cancelled = tok is not None and tok.is_cancelled

        used_llm = (not cancelled) and self._should_use_llm(state)
        if used_llm:
            try:
                verdict = await self._llm_observe(state, ctx, events)
            except Exception as exc:
                logger.warning("ObserveStep LLM call failed, degrading to mechanical: %s", exc)
                verdict = self._mechanical_verdict(state)
                used_llm = False
        else:
            verdict = self._mechanical_verdict(state)
```

并在机械路径下转 background observe。**放在既有那段 close 边界 launch 之后**，
避免对同一 task 重复 launch（`launch_background_observe` 内有 per-task 锁，
但重复 launch 会多发一对 `TaskRecapStarted`/`Done`）：

```python
        # 机械判决没有摘要 → 交后台产真 recap（用户裁定）。close 边界那支已经 launch 过
        # 的不再重复。
        if not used_llm and not _already_launched:
            from ctx_weft.core.loop.steps.background_observe import launch_background_observe
            launch_background_observe(state, ctx, boundary="mechanical")
```

`_already_launched` 用一个局部布尔记录上面 close 边界那支是否已 launch。
**`boundary="mechanical"` 是新值** —— 去 `background_observe.py` 确认
`_CLOSE_BOUNDARIES` 与其它按 boundary 分流的地方能吃下这个新值；
若需要登记，一并加上。

- [ ] **Step 6: 跑测试确认通过**

- [ ] **Step 7: 修既有测试**

`tests/unit/test_observe_outcomes.py` 里断言合成摘要文本的用例会红
（那正是我们要删的行为）。逐条判：断言的是**结局**→ 保留；断言的是**摘要文本**→ 改成
断言 `act_recap == ""`。**注意基线那条既有失败
`test_default_role_prompt_uses_two_fields` 与本改动无关，不要顺手"修"它。**

- [ ] **Step 8: 全量 + golden**

Expected: 全量恰好 1 条既有失败；golden 31/31/0。
若 golden 里有用例断言了 `ObserveCompleted.summary_length`，它会从非零变 0 —— 
**这是预期内的**，更新该 fixture 并在报告里说明。

- [ ] **Step 9: 提交**

```bash
git add -A && git commit -m "refactor(observe): 删机械合成摘要，非 LLM 路径改机械判决 + 转 background observe"
```

---

## Task 5: 判别值收敛成枚举

**Files:**
- Create: `src/ctx_weft/core/discriminators.py`
- Modify: `src/ctx_weft/core/runtime.py`、`src/ctx_weft/core/errors.py`、
  `src/ctx_weft/core/orchestrator/task_manager.py`、
  `src/ctx_weft/core/orchestrator/task_disposition.py`、
  `src/ctx_weft/core/loop/steps/finalize.py`、
  `src/ctx_weft/core/orchestrator/session_state.py`
- Test: `tests/unit/test_discriminators.py`（新建）

**Interfaces:**
- Produces: `InterruptReason` / `CancelReason` / `TaskErrorCode` 三个 `StrEnum`，
  供后续任务与 host 引用。

**背景（必读）**：`reason` **零集中定义** —— `"llm_outage"` 在**同一个函数里写了 4 遍**
（`runtime.py:2526/2527/2542/2548`），`"run_crash"` 3 处，`"failure_threshold"` 4 处。
`RunOutcome.reason` 的类型是裸 `str`。

`error_code` 有**半套机制**：`CtxWeftError.code` 类属性收敛了 `CONTEXT_OVERFLOW` /
`MAX_TURNS_EXCEEDED` 等，但处置表那三个码直接写字面量、不走那套，
其中 `TASK_FAILED_BY_OBSERVER` 在 `errors.py:191` 与 `task_disposition.py:134` **各写一份**。

- [ ] **Step 1: 写失败的测试**

新建 `tests/unit/test_discriminators.py`：

```python
"""判别值必须来自集中定义，不许散落字面量（总账 C1/C2）。"""

from __future__ import annotations

import pathlib
import re

from ctx_weft.core.discriminators import CancelReason, InterruptReason, TaskErrorCode


def test_enum_values_are_the_wire_strings():
    """StrEnum 的值就是上线上的字面量——改值等于改对外契约。"""
    assert InterruptReason.LLM_OUTAGE == "llm_outage"
    assert InterruptReason.RUN_CRASH == "run_crash"
    assert InterruptReason.ASSEMBLY_FAILURE == "assembly_failure"
    assert CancelReason.USER_CANCEL == "user_cancel"
    assert CancelReason.FAILURE_THRESHOLD == "failure_threshold"
    assert CancelReason.PAUSE_ABANDON == "pause_abandon"
    assert TaskErrorCode.BY_OBSERVER == "TASK_FAILED_BY_OBSERVER"
    assert TaskErrorCode.RETRY_EXHAUSTED == "TASK_FAILED_RETRY_EXHAUSTED"
    assert TaskErrorCode.BY_THRESHOLD == "TASK_FAILED_BY_THRESHOLD"


_SRC = pathlib.Path("src/ctx_weft")
_ALLOWED = {"src/ctx_weft/core/discriminators.py"}


def _literal_sites(literal: str) -> list[str]:
    hits: list[str] = []
    pat = re.compile(rf'"{re.escape(literal)}"')
    for p in _SRC.rglob("*.py"):
        rel = p.as_posix()
        if rel in _ALLOWED:
            continue
        for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if pat.search(code):
                hits.append(f"{rel}:{lineno}")
    return hits


def test_no_stray_reason_literals():
    """这些判别值只许从 discriminators 引用（注释里出现不算）。"""
    offenders: list[str] = []
    for lit in ("llm_outage", "run_crash", "assembly_failure",
                "user_cancel", "failure_threshold", "pause_abandon"):
        offenders += _literal_sites(lit)
    assert offenders == [], f"散落的判别值字面量: {offenders}"


def test_no_stray_task_error_code_literals():
    offenders: list[str] = []
    for lit in ("TASK_FAILED_BY_OBSERVER", "TASK_FAILED_RETRY_EXHAUSTED",
                "TASK_FAILED_BY_THRESHOLD"):
        offenders += _literal_sites(lit)
    assert offenders == [], f"散落的 error_code 字面量: {offenders}"
```

- [ ] **Step 2: 跑测试确认失败**

Expected: 后两条 FAIL 并列出全部散落点（这份清单就是 Step 4 的工作量）。

- [ ] **Step 3: 建 `discriminators.py`**

```python
"""判别值的集中定义。

事件 payload 里的 `reason` / `error_code` 是 host 用来分流的对外契约。
它们此前是散落各处的裸字面量（`"llm_outage"` 一个值在同一个函数里写过 4 遍），
改一处会静默分叉——与 `crash_error_code` / `crash_run_outcome` 当初被抽出来
是同一条理由（M3）。

**这些 StrEnum 的值就是上线上的字符串，改值等于改对外契约。**
"""

from __future__ import annotations

from enum import StrEnum


class InterruptReason(StrEnum):
    """`TaskInterrupted` / `RunInterrupted` / `TaskRequeued` 的 `reason`。"""

    LLM_OUTAGE = "llm_outage"
    RUN_CRASH = "run_crash"
    ASSEMBLY_FAILURE = "assembly_failure"


class CancelReason(StrEnum):
    """`TaskCanceled` 的 `reason`。"""

    USER_CANCEL = "user_cancel"
    FAILURE_THRESHOLD = "failure_threshold"
    PAUSE_ABANDON = "pause_abandon"


class TaskErrorCode(StrEnum):
    """task 结局码。异常派生的码走 `CtxWeftError.code`，不在此列。"""

    BY_OBSERVER = "TASK_FAILED_BY_OBSERVER"
    RETRY_EXHAUSTED = "TASK_FAILED_RETRY_EXHAUSTED"
    BY_THRESHOLD = "TASK_FAILED_BY_THRESHOLD"
```

**`discriminators.py` 必须是纯 stdlib**，不 import 任何 `ctx_weft` 运行期模块 ——
它会被 `task_disposition.py`（同样纯 stdlib）引用，不能引进环。

- [ ] **Step 4: 逐处替换散落字面量**

按 Step 2 的失败清单逐个替换。**因为是 `StrEnum`，`==` 比较与序列化行为不变**，
所以替换是纯机械的、零行为变化。

- [ ] **Step 5: 修 C2 —— `reason` 键里装 `error_code`**

`task_manager.py:1140`：

```python
"reason": (interrupted[0].error_code or interrupted[0].error or "interrupted")
```

于是 `TaskQueueInterrupted.reason` 的值域是「error_code ∪ 自由文本 ∪ 兜底串」的并集，
而注释明写 host 要按这个码分流。

**改法**：payload 拆成两个键，`error_code` 归 `error_code`：

```python
        await self._emit(EventType.TASK_QUEUE_INTERRUPTED, payload={
            # host 按码分流（CONTEXT_OVERFLOW → 提示换更大窗口的模型）。
            "error_code": interrupted[0].error_code or "",
            # 展示用自由文本，不参与分流。
            "reason": interrupted[0].error or "interrupted",
        })
```

**这是对外 payload 变更。** 必须同步：
- `session_manager.py:131` 取 `reason` 的那处（它透传进 `SessionInterrupted.payload`）——
  决定 `SessionInterrupted.reason` 取哪个键，**并在报告里说明你的选择与依据**；
- `docs/spec/03-reducer-rules.md` 与 `docs/upgrade/` 新增一条升级须知；
- golden 里若有 `TaskQueueInterrupted` 用例，更新之。

- [ ] **Step 6: 跑测试 + 全量 + golden**

- [ ] **Step 7: 提交**

```bash
git add -A && git commit -m "refactor(events): reason 与 error_code 收敛成 StrEnum，队列信号拆分两个键"
```

---

## Task 6: `TaskHumanResolved` 发射搬回 TaskManager + 收紧守卫

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/task_manager.py`（新增不看当前状态的入口）
- Modify: `src/ctx_weft/core/runtime.py`（`_inject_user_reply` 改调 TM）
- Modify: `tests/unit/test_task_manager_owns_status.py`（守卫清单）

**Interfaces:**
- Consumes: 无
- Produces: `TaskManager.mark_human_resolved(task_id, *, hitl_id) -> None`

**背景（必读）**：`TaskHumanResolved` 有两个发射点，其中
`runtime._inject_user_reply` **在 TM 之外、且同时写 `task.status`**
（`runtime.py:1938` 写 PENDING，`:1942` 发事件）。它确实是一条 task 状态事件 ——
映射到 PENDING、有专属 reducer 分支、S 档。

**两道守卫都没抓住，原因不同**：守卫 A 是 `TASK_STATUS_EVENTS` 清单漏了它
（形态本身可见）；守卫 B 是 `_inject_user_reply` 的精确豁免**当初是为「只改内存态」批的**，
本批次在同一个函数里加了事件发射，豁免顺带覆盖了没被审过的事。

**绕开的理由是「迁就测试替身」**（注释自陈：「task_manager 在部分既有单测里是
不带 `_emit` 的轻量 fake」）。功能上的原始问题是真的 —— 恢复路径上 `restore()` 先跑，
已把状态从 AWAITING_HUMAN 翻成 PENDING，`resume_task` 的 `was_blocked` 判据必然落空。
**正解是给 TM 一个不看当前状态的入口。**

- [ ] **Step 1: 写失败的守卫测试**

在 `tests/unit/test_task_manager_owns_status.py` 的 `TASK_STATUS_EVENTS`
（`:29-32`）里加两个名字：

```python
TASK_STATUS_EVENTS = (
    "TASK_FINISHED", "TASK_FAILED", "TASK_REQUEUED", "TASK_SUSPENDED",
    "TASK_AWAITING_HUMAN", "TASK_INTERRUPTED", "TASK_CANCELED",
    "TASK_HUMAN_RESOLVED", "TASK_RESUMED",
)
```

**不要加 `TASK_STARTED`** —— 它会误报 `session_manager.py:114` 的查表读，
说明守卫 A「属性访问即违规」的判据对「读表」没有免疫。那是另一个问题，不在本任务范围。

- [ ] **Step 2: 跑守卫确认它变红**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_task_manager_owns_status.py::test_only_task_manager_emits_task_status_events -q`
Expected: FAIL，offender 是 `src/ctx_weft/core/runtime.py:1944:TASK_HUMAN_RESOLVED`。
（`TASK_RESUMED` 的唯一发射点在 TM，不该报。）

- [ ] **Step 3: 给 TM 加不看当前状态的入口**

在 `task_manager.py` 的 `resume_task` 附近：

```python
    async def mark_human_resolved(self, task_id: str, *, hitl_id: str) -> None:
        """HITL 应答落地后把 task 置回 PENDING 并发事实——**不看当前状态**。

        与 `resume_task` 的区别：后者有 `was_blocked` 门（只对
        AWAITING_HUMAN/SUSPENDED 生效），而恢复路径上 `restore()` 先跑、
        已把状态翻成 PENDING，那道门必然落空。此入口专为该场景而设。

        终态不复活：已 FINISHED/FAILED/CANCELED 的 task 原样返回、不发事件。
        """
        task = self._tasks.get(task_id)
        if task is None or task.status in _TERMINAL_STATUSES:
            return
        task.status = "PENDING"
        await self._emit(EventType.TASK_HUMAN_RESOLVED, task_id=task_id,
                         payload={"hitl_id": hitl_id})
```

- [ ] **Step 4: 改 `_inject_user_reply` 调它**

`runtime.py:1934-1948`，删掉就地写状态与就地构造 `Event(...)` 那一整段，换成：

```python
        tm = self._task_managers.get(session.id)
        if tm is not None:
            await tm.mark_human_resolved(target.id, hitl_id=req.id)
```

**注释里那条「迁就轻量 fake」的理由要一并删掉** —— 若确有单测用不带 `_emit` 的
fake TaskManager，**改测试替身，不要改生产代码的所有权**。
这些替身补一个 `mark_human_resolved` 即可。

- [ ] **Step 5: 跑守卫确认转绿 + 跑 HITL 相关测试**

```
./.venv/Scripts/python.exe -m pytest tests/unit/test_task_manager_owns_status.py -q
./.venv/Scripts/python.exe -m pytest tests/unit/test_task_human_resolved_e2e.py tests/unit/test_hitl_recovery_v2.py -q
```

`test_task_human_resolved_e2e.py` 的 docstring 明写「两个真实发射点」——
现在只剩一个，**订正该 docstring 并合并那两条 e2e**。

- [ ] **Step 6: 收紧守卫 B 的豁免注释**

`_ALLOWED_STATUS_WRITES` 里 `("src/ctx_weft/core/runtime.py", "_inject_user_reply")`
这条豁免 —— 本任务之后该函数**不再写 `task.status`**。
跑守卫 B，若它已不需要豁免，**把这条删掉**（并做负向对照确认删了之后守卫仍能抓人）。
若仍需要（该函数还有别的 status 写入），保留并**更新注释说明它现在只做什么**。

- [ ] **Step 7: 全量 + golden**

- [ ] **Step 8: 提交**

```bash
git add -A && git commit -m "fix(ownership): TaskHumanResolved 发射搬回 TaskManager，守卫清单补两条"
```

---

## Task 7: 清死值域 + 补 `ObserveStarted`

**Files:**
- Modify: `src/ctx_weft/core/state/models.py`（删 `TO_BE_OBSERVED`）
- Modify: `src/ctx_weft/core/loop/steps/act_guidance.py`（两处过时 docstring）
- Modify: `src/ctx_weft/protocols/events.py`（加 `OBSERVE_STARTED`）
- Modify: `src/ctx_weft/core/loop/steps/observe.py`（发 `ObserveStarted`）
- Modify: `docs/upgrade/2026-09-02-session-status-ownership.md:49-53`
- Test: `tests/unit/test_observe_outcomes.py`（追加）

**Interfaces:**
- Consumes: Task 4 改后的 `ObserveStep.execute`
- Produces: `EventType.OBSERVE_STARTED`

**背景（必读）**：`TO_BE_OBSERVED` 从 LoomeJ baseline 导入后（`1ef4419`）
**从未被写过一次**，零读取、不在 `TASK_STATUS_BY_EVENT`，前身文档也只有值域枚举、
无任何语义说明。

它想表达的东西今天已有更合适的承载：`StepStarted{step_name}` 标记进入时刻、
已折进 `RunStateView.current_step`。层次差是关键 —— `current_step` 是 run 级瞬时相位，
`TaskStatus` 是 task 级持久状态；而 observe 必然在 run 内走完，
**不存在「任务停在 TO_BE_OBSERVED 等下一次调度」的场景**。

但 observe 这层确有**不对称**：有 `ObserveCompleted` 没有 `ObserveStarted`，
而后台链反倒是齐的（`TaskRecapStarted`/`TaskRecapDone` 成对）。

- [ ] **Step 1: 写失败的测试**

```python
def test_to_be_observed_is_gone():
    """死值域成员不该留在类型里（总账 D2）。"""
    import typing
    from ctx_weft.core.state.models import TaskStatus
    assert "TO_BE_OBSERVED" not in typing.get_args(TaskStatus)


async def test_observe_emits_started_and_completed():
    """observe 的起止成对，与后台 recap 的 TaskRecapStarted/Done 同形。"""
    state, ctx = _state_and_ctx()
    outcome = await ObserveStep().execute(state, ctx)
    types = [e.type for e in outcome.events]
    assert EventType.OBSERVE_STARTED in types
    assert EventType.OBSERVE_COMPLETED in types
    assert types.index(EventType.OBSERVE_STARTED) < types.index(EventType.OBSERVE_COMPLETED)
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 删 `TO_BE_OBSERVED`**

`src/ctx_weft/core/state/models.py:104`。删前再跑一次
`grep -rn "TO_BE_OBSERVED" src/ tests/` 确认只剩定义与两处 docstring。

- [ ] **Step 4: 订正两处过时 docstring**

`act_guidance.py:61` 与 `:111` 枚举非终态时漏了 `AWAITING_HUMAN`/`INTERRUPTED`、
多了 `TO_BE_OBSERVED`，而实际代码用的是终态补集。改成描述实际判据：

```python
    """session 内非终态 task（`_TERMINAL_STATUSES` 的补集），无 task_manager 时空表。"""
```

- [ ] **Step 5: 加 `OBSERVE_STARTED` 并发射**

`protocols/events.py`，在 `OBSERVE_COMPLETED` 旁：

```python
    OBSERVE_STARTED = "ObserveStarted"                # observe 起点（与 Completed 成对）
```

`observe.py` 的 `execute` 开头（在任何分流之前）：

```python
        events.append(make_event(
            state, EventType.OBSERVE_STARTED,
            payload={"task_id": state.task.id},
        ))
```

**payload 只放 `task_id`** —— 「用不用 LLM」在起点还没定（Task 4 之后它取决于
取消状态与 `_should_use_llm`），不要在这里猜。

- [ ] **Step 6: 订正升级须知的事实错误**

`docs/upgrade/2026-09-02-session-status-ownership.md:49-53` 把 `TO_BE_OBSERVED`
列进「本次新增的值域」—— 那次新增的是 `AWAITING_HUMAN` 与 `INTERRUPTED`，
它是搭便车被列进去的。订正该段，并补一句说明它已在本批次删除。

- [ ] **Step 7: 全量 + golden**

`OBSERVE_STARTED` 是新事件类型 —— 确认它经 `make_event` 的白名单校验
（`EVENT_TYPES` 由 `frozenset(EventType)` 自动派生，应自动通过）。
按维护清单，golden 若有覆盖 observe 的用例，事件序会多一条，更新之。

- [ ] **Step 8: 提交**

```bash
git add -A && git commit -m "chore(observe): 删死值域 TO_BE_OBSERVED，补 ObserveStarted 与两处过时 docstring"
```

---

## 自查

**Spec 覆盖**：总账「建议的处理顺序」六条 —— A1(T1) / A2+B3(T2) / A3(T3，已按用户裁定收窄) /
C1+C2(T5) / B1(T6) / D2+C7(T7)，外加用户新裁定的 observe 重做(T4)。
**未覆盖且有意留下的**：A4（sequence 重号）、A5（tenant_id 漏填）、A6、A7、
B2、B4、B5、C3-C6、D1、D3-D6、E1-E9 —— 属批次二。

**类型一致性**：T5 产出的三个 `StrEnum` 名字在 T5 内自洽；
T6 产出的 `mark_human_resolved` 签名在 T6 内自洽；
T4 的 `_mechanical_verdict` 只在 `observe.py` 内使用。

**已知风险**：
- T3 Step 4 与 T5 Step 5 各有一处**对外 payload 变更**，都要求实施者
  撞上 golden 变红时**停下来报告**而非改 fixture 迁就。
- T4 的前提（background observe 不依赖 observe ROLE）在 Step 1 显式验证，
  不成立则停。
