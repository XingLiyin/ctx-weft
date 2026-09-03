# AgentRegistry 与 LLM 归属 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 agent 的所有权从 runtime 各处收进一个真正的持有者 `AgentRegistry`，并把「用哪个 LLM」的真相源从 session 移到 agent record——一个真相源、一个构造入口、一个事件发射点、一处模型解析。

**Architecture:** `LifecycleManager` 今天是无状态工厂（dataclass 只有 `template_lookup`，在 `runtime.py` 里被 `new` 五次），agent 对象每次派发现造，因此没有任何地方能安放 agent 的持久状态。本计划把它晋升成 runtime 级、持有 `dict[agent_id, _AgentRecord]` 的 `AgentRegistry`：三条 Agent 构造路径塌成一个 `materialize`，四条 agent 域事件收进一个发射点，恢复由 `load(agent_views)` 装填（喂进来，不查回去），LLM 的 choice 进 record、client 由注入的 `ModelResolver` 现解、窗口从 client 派生。换模型收敛成 `set_agent_llm` / `set_session_llm` 两条命令，续跑路径一个 llm 参数都不剩。

**Tech Stack:** Python ≥3.11、pytest + pytest-asyncio（`asyncio_mode = "auto"`）、ruff（line-length 100）、uv。

**Spec:**
- AgentRegistry（结构 + 模型解析）：https://claude.ai/code/artifact/a335b91f-ed65-41b0-b739-6108c355ada9
- Agent 模型归属（命令面拆分）：https://claude.ai/code/artifact/18464b13-6c67-42d9-8bcd-401a41c1bfde

## Global Constraints

- **Python** `>=3.11`；**ruff** `line-length = 100`，`select = ["E","W","F","I","B","UP","RUF"]`，`ignore = ["E501"]`。
- **pytest** `asyncio_mode = "auto"`、`testpaths = ["tests"]`、`addopts = "-ra -q --strict-markers"`。测试文件顶部沿用现有惯例 `pytestmark = pytest.mark.asyncio`。
- 运行命令一律 `uv run pytest`。
- **事件体系 V2 三条硬规则**（`docs/events-v2.md` §6/§7）：
  1. `EventType` 全集 ≡ 实际发射集合 ∪ L 档——**定义即必须发射**；
  2. **新名字绝不复用任何曾经发射过的字符串**；
  3. S 档 payload **只可加字段**，不可改语义、不可删。
- **不碰 `origin`**：`Event` today 尚无 `origin` 字段（V2 §4 整节待接线），本计划不引入。
- **不碰会话状态机**：不新增 `SessionManager` 的任何输入，不动六条会话事件，`SessionStatus` 值域不变。
- **发射者即所有者**：agent 域的状态事实由 `AgentRegistry` 发。**绝不通过订阅总线来应用状态变更**——in-process bus 在 `emit()` 内同步 drain 且背压下丢事件；binding 是控制流数据，必须「先在内存里改完，再发」。
- **行号是导航，符号名才是锚。** 本计划最初对着 `aa11802` 写，基线已推进到 `8a14fd4`（task 状态所有权重构落地，**agent 域未被触及**，全部前提已逐条复核成立）。行号已按 `8a14fd4` 刷新，但仍可能漂移——**以符号名 / 函数名定位，行号只作粗略指引**。
- **回落而非报错**：模板解析不出、agent 未登记等恢复期缺口，一律降级 + WARNING 日志，不抛异常阻断恢复（沿用 `agents_from_projection` docstring 已确立的口径）。

## 分批上线

| 批次 | 任务 | host |
|---|---|---|
| **A · 结构** | Task 1–6 | 事件流同构，**host 不用改**，可独立回滚 |
| **B · llm 归属** | Task 7–9 | `AgentLlmChanged` 是 S 档 → **host `projection_updater.py` 同批次** |
| **C · task 解除阻塞** | Task 10 | 新 S 档事件 → **host 同批次**；与 A/B 无依赖，可任意时候插入 |

## File Structure

| 文件 | 责任 |
|---|---|
| `src/ctx_weft/core/orchestrator/agent_registry.py` | **新建**（Task 6 由 `lifecycle_manager.py` 改名而来）。`ModelChoice` / `ResolvedModel` / `ModelResolver` / `_AgentRecord` / `AgentRegistry`。agent 身份与配置的唯一持有者与唯一改写者，agent 域四条事件的唯一发射点。 |
| `src/ctx_weft/core/orchestrator/lifecycle_manager.py` | Task 1–5 期间继续存在；Task 6 删除（git mv）。 |
| `src/ctx_weft/core/state/models.py` | `Agent` 瘦身（删六个死字段 + `AgentStatus`）。 |
| `src/ctx_weft/core/runtime.py` | 删 `_default_agent` / `_flush_tracking_memory` / `_sync_session_llm_window` / 回填 / `is_new_agent` / `except SpawnDepthExceeded` / `pre_resolved_agents` / `_resolved_agents` / 四个临时 LM 实例 / 续跑路径的 llm 参数。新增 `set_agent_llm` / `set_session_llm` 转发。 |
| `src/ctx_weft/core/control/converters.py` | 删 `agents_from_projection`（并入 `AgentRegistry.load`）。 |
| `src/ctx_weft/core/control/types.py` | `AgentView` 加 `llm_account` / `llm_model`。 |
| `src/ctx_weft/core/control/reducers.py` | `AGENT_INSTANTIATED` 分支补两个字段；新增 `AGENT_LLM_CHANGED` 分支；`TASK_RESUMED` 映射改 `PENDING`；新增 `TASK_HUMAN_RESOLVED` 分支。 |
| `src/ctx_weft/core/loop/llm_gateway.py` | `resolve_llm_identity` 改读 `state.resolved_model`，删两级兜底。 |
| `src/ctx_weft/protocols/events.py` | 新增 `AGENT_LLM_CHANGED` / `TASK_HUMAN_RESOLVED`。 |
| `src/ctx_weft/protocols/hitl.py` | 删 `ResumeHint` 与 `HitlReply.resume_hint`。 |
| `src/ctx_weft/core/orchestrator/session_manager.py` | 字段名 `lifecycle_manager` → `agent_registry`；不再自己发 `AgentInstantiated`。 |
| `src/ctx_weft/core/orchestrator/task_manager.py` | `resume_task` 发 `TaskHumanResolved`；`_try_resume_parent` 不再预置 `ACTIVE`。 |

**既有的验收网**——这三个测试是 Task 4 的正确性判据，**必须不改一行仍然通过**：
`tests/integration/test_spawn_rejected_event.py`、`tests/integration/test_subagent_instantiated_event.py`、`tests/unit/test_agent_template_id_replay.py`。

---

# 批次 A · 结构

### Task 1: 清六个死字段

`Agent` 十五个字段里六个只写不读。先清它们，后面每一步要搬的东西都小一圈。

**Files:**
- Modify: `core/state/models.py`（`AgentStatus`）（`AgentStatus`）、`:264-291`（`Agent`）
- Modify: `src/ctx_weft/core/state/__init__.py:7,24`（导出）
- Modify: `src/ctx_weft/core/orchestrator/lifecycle_manager.py:67-80`
- Modify: `src/ctx_weft/core/runtime.py:189-199`（`_flush_tracking_memory`）、`:2792`（`_default_agent`）、两处 `_flush_tracking_memory(...)` 调用
- Modify: `src/ctx_weft/core/control/converters.py:75-104`
- Test: `tests/unit/test_agent_field_domain.py`（新建）

**Interfaces:**
- Produces: `Agent` 的字段集合收敛为 `id` / `session_id` / `tenant_id` / `template_id` / `parent_agent_id` / `spawn_depth` / `memory_config` / `loop_config` / `loop_guard` / `created_at` / `updated_at` / `runtime`。后续所有 Task 按此集合构造 `Agent`。

被删的六个及其证据：

| 字段 | 证据 |
|---|---|
| `status`（+ `AgentStatus` 类型） | LM 写 `"IDLE"`、`_default_agent` 写 `"RUNNING"`，全仓无读取，`AgentView` 也没有 |
| `template_version` | `identity.py:53` 与两个事件 payload 读的都是**活 template 对象**的 `.version` |
| `bound_capability_ids` | 只有 LM 写；LM 自己的 docstring：「仅作元数据记录，不驱动 capability 解析」 |
| `active_task_id` | 全仓零引用 |
| `tracking_task_ids` | 从未被写（`control_capability.py:260` 写的是 **Task** 的同名字段） |
| `fetched_tracking_ids` | 只在遍历上一行那个永远为空的列表的循环体里被写 |

- [ ] **Step 1: 写下钉住字段集合的失败测试**

```python
# tests/unit/test_agent_field_domain.py
"""Agent 的字段集合是被钉住的——六个只写不读的字段已在 2026-09-02 删除。

与 tests/unit/test_session_status_domain.py 同类：值域/字段域由测试守，
免得「看着像状态机、其实没人读」的字段再长回来。
"""
from __future__ import annotations

import dataclasses

from ctx_weft.core.state.models import Agent

EXPECTED_FIELDS = {
    "id", "session_id", "tenant_id",
    "template_id", "parent_agent_id", "spawn_depth",
    "memory_config", "loop_config", "loop_guard",
    "runtime", "created_at", "updated_at",
}

REMOVED_FIELDS = {
    "status", "template_version", "bound_capability_ids",
    "active_task_id", "tracking_task_ids", "fetched_tracking_ids",
}


def test_agent_field_set_is_pinned():
    actual = {f.name for f in dataclasses.fields(Agent)}
    assert actual == EXPECTED_FIELDS


def test_removed_fields_stay_removed():
    actual = {f.name for f in dataclasses.fields(Agent)}
    assert actual & REMOVED_FIELDS == set()


def test_agent_status_type_is_gone():
    import ctx_weft.core.state as state
    assert not hasattr(state, "AgentStatus")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_agent_field_domain.py -v`
Expected: FAIL —— `test_agent_field_set_is_pinned` 断言不等（actual 多出六个字段）。

- [ ] **Step 3: 删字段**

`models.py`：删 `AgentStatus = Literal[...]` 整块；`Agent` 里删 `status` / `template_version` / `bound_capability_ids` / `active_task_id` / `tracking_task_ids` / `fetched_tracking_ids` 六行。
`state/__init__.py`：删 `AgentStatus` 的 import 与 `__all__` 条目。

- [ ] **Step 4: 删三个构造器里对应的赋值**

`lifecycle_manager.py` 的 `Agent(...)` 去掉 `template_version=` / `status=` / `bound_capability_ids=`；
`runtime.py:2870` 的 `_default_agent` 去掉 `template_version=` / `status=`；
`converters.py:93` 的 `Agent(...)` 去掉 `template_version=` / `status=`，并删掉现在没有用户的 `fallback_template_version` 参数（连同 `agents_from_projection` 的签名与它在 `runtime.py:1495` 的调用实参）。

- [ ] **Step 5: 删 `_flush_tracking_memory`**

```python
# runtime.py:189-199 —— 整个函数删除
# 它遍历 agent.tracking_task_ids（永远为空），把元素加进 fetched_tracking_ids（无人读）。
# docstring 自己写着：「本函数仅保留 fetched_tracking_ids 记账，签名不变
# （memory/task_manager 参数暂留待日落）」——日落就在这里。
```

同时删除它在 `TaskRunner.assemble` 两个分支里的调用（subagent 分支与 `case _` 分支各一处 `await _flush_tracking_memory(...)`）。

- [ ] **Step 6: 跑新测试 + 全量**

Run: `uv run pytest tests/unit/test_agent_field_domain.py -v && uv run pytest`
Expected: 新测试 PASS；全量通过。**若有测试因 `Agent(status=...)` / `Agent(template_version=...)` 而 TypeError，去掉那两个实参**（已知涉及 `tests/integration/test_interactive_task.py`、`tests/unit/test_act_tool_loop_serial_hitl.py`、`tests/unit/test_background_observe_wiring.py`、`tests/unit/test_blackboard.py`）。

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor(agent): 删掉 Agent 的六个只写不读字段与空转的 _flush_tracking_memory

status / template_version / bound_capability_ids / active_task_id /
tracking_task_ids / fetched_tracking_ids 全仓无读取点；tracking 那一对是
「遍历永远为空的列表、把结果写进无人读的集合」。AgentStatus 类型随之删除——
agent 没有状态机，「在不在跑」是 TaskManager 的领域。

字段集合由 tests/unit/test_agent_field_domain.py 钉住。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: LifecycleManager 有状态化

晋升成 runtime 级长生命周期组件，持有 `_AgentRecord` 注册表。**此时仍只有 `instantiate_agent` 一个方法，行为不变。**

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/lifecycle_manager.py`
- Modify: `src/ctx_weft/core/runtime.py:609`（SM 构造参数）、删 `:925` `:1471` `:1636` `:1752` 四个临时实例、`:887` 附近的 `_release_session`
- Test: `tests/unit/test_agent_registry_state.py`（新建）

**Interfaces:**
- Produces:
  ```python
  @dataclass
  class _AgentRecord:
      session_id: str
      tenant_id: str
      template_id: str
      parent_agent_id: str | None
      spawn_depth: int
      memory_config: MemoryConfig
      loop_config: LoopConfig

  @dataclass
  class LifecycleManager:
      template_lookup: TemplateLookup
      _agents: dict[str, _AgentRecord] = field(default_factory=dict)
      _sessions: dict[str, _SessionDefaults] = field(default_factory=dict)

      def register_session(self, session_id: str, *, tenant_id: str,
                           fallback_template_id: str) -> None
      def release_session(self, session_id: str) -> None
      def has(self, agent_id: str) -> bool
      def template_id_of(self, agent_id: str) -> str
  ```
  `_SessionDefaults` 是 `@dataclass class _SessionDefaults: tenant_id: str; fallback_template_id: str`。`register_session` 重入安全（`setdefault`，镜像 `SessionManager.register_session`）。
- Consumes: Task 1 收敛后的 `Agent` 字段集合。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_agent_registry_state.py
"""AgentRegistry 持有 agent 的身份与配置——注册表，不是状态机。

镜像 SessionManager 的 _SessionState / _states / register_session 形状
（docs/events-v2.md §2.1.1 的那次晋升）。区别：它零订阅。
"""
from __future__ import annotations

import pytest

from ctx_weft.core.orchestrator.lifecycle_manager import LifecycleManager
from tests.integration.test_minimal_loop import (
    InlineAgentTemplateProvider,
    make_echo_template,
)

pytestmark = pytest.mark.asyncio


def _lm() -> LifecycleManager:
    from ctx_weft.core.orchestrator.template_lookup import TemplateLookup
    provider = InlineAgentTemplateProvider([make_echo_template()])
    return LifecycleManager(template_lookup=TemplateLookup(providers=[provider]))


async def test_instantiate_registers_a_record():
    lm = _lm()
    agent, _tmpl = await lm.instantiate_agent(
        template_id="agent__tpl_echo", session_id="s1", tenant_id="default",
    )
    assert lm.has(agent.id)
    assert lm.template_id_of(agent.id) == agent.template_id


async def test_register_session_is_reentrant():
    lm = _lm()
    lm.register_session("s1", tenant_id="t1", fallback_template_id="agent__tpl_echo")
    lm.register_session("s1", tenant_id="OTHER", fallback_template_id="OTHER")
    # 已存在则保留原状态——与 SessionManager.register_session 同口径
    assert lm._sessions["s1"].tenant_id == "t1"


async def test_release_session_drops_only_that_sessions_agents():
    lm = _lm()
    a, _ = await lm.instantiate_agent(
        template_id="agent__tpl_echo", session_id="s1", tenant_id="default")
    b, _ = await lm.instantiate_agent(
        template_id="agent__tpl_echo", session_id="s2", tenant_id="default")
    lm.release_session("s1")
    assert not lm.has(a.id)
    assert lm.has(b.id)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_agent_registry_state.py -v`
Expected: FAIL —— `AttributeError: 'LifecycleManager' object has no attribute 'has'`。

- [ ] **Step 3: 加注册表**

在 `lifecycle_manager.py` 里加 `_SessionDefaults` / `_AgentRecord` 两个 dataclass 与 `_agents` / `_sessions` 两个字段；`instantiate_agent` 在 `return` 之前登记一条 record；实现 `register_session` / `release_session` / `has` / `template_id_of`。

`release_session` 用扫描实现（会话内 agent 数量是个位数，不值得再维护一个索引）：

```python
def release_session(self, session_id: str) -> None:
    for aid in [k for k, r in self._agents.items() if r.session_id == session_id]:
        self._agents.pop(aid, None)
    self._sessions.pop(session_id, None)
```

- [ ] **Step 4: 收成 runtime 级单例**

`runtime.py:609` 已经把一个 LM 传给 SM。把它提成 `self._agent_registry = LifecycleManager(template_lookup=self._template_lookup)`，再传给 SM。
删除 `:925` / `:1471` / `:1636` / `:1752` 四处 `lm = LifecycleManager(...)`，改用 `self._agent_registry`。
在 `runtime._release_session(session_id)` 里补一行 `self._agent_registry.release_session(session_id)`。

- [ ] **Step 5: 跑测试**

Run: `uv run pytest tests/unit/test_agent_registry_state.py -v && uv run pytest`
Expected: 全 PASS。

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(agent): LifecycleManager 晋升为 runtime 级有状态组件

从「runtime.py 里 new 五次、用完即弃的无状态 dataclass」变成持有
dict[agent_id, _AgentRecord] 的长生命周期注册表——与 SessionManager 在
2026-09-02 做过的那次晋升同形（docs/events-v2.md §2.1.1）。

此时仍只有 instantiate_agent 一个方法，行为不变。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: 拆 `instantiate` / `materialize`

**这是整件事的支点。** `instantiate_agent(existing_agent_id=...)` 同时是「新建」和「按 id 水合」，五个调用点里只有两个是真新建，于是调用方必须自己算 `is_new_agent`（`runtime.py:2713`）——事件因此只能留在调用方。参数消失，歧义随之消失。

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/lifecycle_manager.py`
- Modify: `src/ctx_weft/core/runtime.py:2870`（删 `_default_agent`）、`:2646`、`:1622`、`:1738`、`:2743`
- Modify: `src/ctx_weft/core/orchestrator/session_manager.py:197`
- Test: `tests/unit/test_agent_registry_materialize.py`（新建）

**Interfaces:**
- Produces:
  ```python
  async def instantiate(
      self, *, template_id: str, session_id: str, tenant_id: str,
      parent_agent_id: str | None = None, ctx: ProviderContext | None = None,
  ) -> tuple[Agent, AgentTemplate]
      # 真新建。id 由本方法生成。深度超限抛 SpawnDepthExceeded。
      # ★ 无 existing_agent_id 参数

  def materialize(
      self, agent_id: str, *, context_limit: int, reserved_output_tokens: int,
  ) -> Agent
      # 水合。零事件。未登记 → 按 session 的 fallback_template_id 就地补登记 + WARNING
  ```
- Consumes: Task 2 的 `_AgentRecord` / `register_session`。

> **`materialize` 永不抛。** 未登记的 id 走「补登记 + 警告」而不是 `KeyError`：这是 `agents_from_projection` docstring 已确立的口径——「回落而非报错是刻意的：授权按模板做策略，重启后把未知模板判成『无权限』会让老会话直接跑不动」。把恢复期的一个缺口变成崩溃是净损失。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_agent_registry_materialize.py
"""instantiate（真新建）与 materialize（水合）是两件事。

拆开之前它们是同一个方法的两种模式，靠 existing_agent_id 是否为 None 区分，
而区分的结果只有调用方知道 —— 那正是 agent 域事件散落在 runtime 里的原因。
"""
from __future__ import annotations

import pytest

from ctx_weft.core.orchestrator.lifecycle_manager import LifecycleManager
from ctx_weft.core.orchestrator.template_lookup import TemplateLookup
from tests.integration.test_minimal_loop import (
    InlineAgentTemplateProvider,
    make_echo_template,
)

pytestmark = pytest.mark.asyncio

TPL = "agent__tpl_echo"


def _lm() -> LifecycleManager:
    provider = InlineAgentTemplateProvider([make_echo_template()])
    lm = LifecycleManager(template_lookup=TemplateLookup(providers=[provider]))
    lm.register_session("s1", tenant_id="default", fallback_template_id=TPL)
    return lm


async def test_instantiate_has_no_existing_agent_id_param():
    import inspect
    sig = inspect.signature(LifecycleManager.instantiate)
    assert "existing_agent_id" not in sig.parameters


async def test_materialize_carries_template_config():
    lm = _lm()
    agent, tmpl = await lm.instantiate(
        template_id=TPL, session_id="s1", tenant_id="default")
    got = lm.materialize(agent.id, context_limit=123, reserved_output_tokens=45)
    assert got.id == agent.id
    assert got.template_id == tmpl.id
    # 从 template 来，不是 dataclass 默认 —— 这修掉了 agents_from_projection 的旧行为
    assert got.memory_config == tmpl.memory_config
    assert got.loop_config == tmpl.loop_config
    # 窗口由调用方传入（批次 B 改成从 record 的 llm 派生）
    assert got.loop_guard.context_limit == 123
    assert got.loop_guard.reserved_output_tokens == 45


async def test_materialize_is_a_fresh_object_each_call():
    """Agent 带一次 run 的可变量（loop_guard.context_tokens 由 act.py 改写），
    所以每次派发产出新实例是正确的，不是浪费。"""
    lm = _lm()
    agent, _ = await lm.instantiate(
        template_id=TPL, session_id="s1", tenant_id="default")
    a = lm.materialize(agent.id, context_limit=1, reserved_output_tokens=1)
    b = lm.materialize(agent.id, context_limit=1, reserved_output_tokens=1)
    assert a is not b


async def test_materialize_unknown_id_falls_back_and_never_raises(caplog):
    """恢复期缺口降级，不抛 —— 与 agents_from_projection 的既有口径一致。"""
    lm = _lm()
    got = lm.materialize("agt_never_seen", context_limit=9, reserved_output_tokens=9)
    assert got.id == "agt_never_seen"
    assert got.template_id  # 回落到 session 的 fallback_template_id
    assert lm.has("agt_never_seen")  # 就地补登记，第二次不再警告
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_agent_registry_materialize.py -v`
Expected: FAIL —— `AttributeError: ... has no attribute 'instantiate'`。

- [ ] **Step 3: 实现两个方法**

`instantiate` = 今天的 `instantiate_agent` 去掉 `existing_agent_id`、`parent_agent` 换成 `parent_agent_id: str | None`（深度从 `self._agents[parent_agent_id].spawn_depth` 取，不再要调用方递一个 Agent 对象进来），末尾登记 record 并 `return self.materialize(...), template`。

`materialize` 从 record 造 `Agent`：

```python
def materialize(self, agent_id, *, context_limit, reserved_output_tokens) -> Agent:
    rec = self._agents.get(agent_id)
    if rec is None:
        rec = self._register_fallback(agent_id)   # 补登记 + logger.warning
    return Agent(
        id=agent_id,
        session_id=rec.session_id,
        tenant_id=rec.tenant_id,
        template_id=rec.template_id,
        parent_agent_id=rec.parent_agent_id,
        spawn_depth=rec.spawn_depth,
        memory_config=rec.memory_config,
        loop_config=rec.loop_config,
        loop_guard=LoopGuard(
            context_limit=context_limit,
            reserved_output_tokens=reserved_output_tokens,
        ),
        created_at=now_utc(),
    )
```

- [ ] **Step 4: 改五个调用点**

| 位置 | 改成 |
|---|---|
| `session_manager.py:197` | `await self.agent_registry.instantiate(...)`（root，无 parent） |
| `runtime.py:2716`（subagent，`t.assigned_agent_id` 为空） | `await self._registry.instantiate(..., parent_agent_id=t.creator_agent_id)` |
| `runtime.py:2716`（subagent，`t.assigned_agent_id` 有值） | `self._registry.materialize(t.assigned_agent_id, context_limit=..., reserved_output_tokens=...)` —— **分支由 `t.assigned_agent_id` 是否为空决定，这一步先保留调用方的 `is_new_agent`，Task 4 才删** |
| `runtime.py:1638`（background observe） | `self._registry.materialize(agent_id, ...)` |
| `runtime.py:1754`（compact_session） | `self._registry.materialize(target_agent_id, ...)` |
| `runtime.py:2812`（`case _` 默认分支） | `self._registry.materialize(effective_agent_id(t, root), context_limit=self._session.context_limit, reserved_output_tokens=self._session.reserved_output_tokens)` |

删除 `runtime.py:2870` 的 `_default_agent`。

- [ ] **Step 5: 跑测试**

Run: `uv run pytest tests/unit/test_agent_registry_materialize.py -v && uv run pytest`
Expected: 全 PASS。**特别确认 `tests/integration/test_compact_flow_e2e.py` 与 `test_subagent_instantiated_event.py` 未改动即通过。**

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(agent): 拆 instantiate / materialize，去掉 existing_agent_id

一个方法同时是「新建」和「按 id 水合」，五个调用点里只有两个是真新建，
于是调用方必须自己算 is_new_agent（runtime.py:2713 的注释就是自白）——
而只有算出这一步的人才知道该不该发出身事件。参数消失，歧义随之消失。

三条 Agent 构造路径（LM / _default_agent / agents_from_projection）
的前两条在此合并；materialize 永不抛，未登记的 id 走回落补登记。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: 四条事件收进 Registry

**本 Task 的验收判据：三个既有集成测试不改一行仍然通过。**

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/lifecycle_manager.py`（加 `event_bus` 字段与三处 emit）
- Modify: `src/ctx_weft/core/runtime.py:2713`（删 `is_new_agent`）、`:2651-2675`（删 `except SpawnDepthExceeded` 块）、`:2676-2720`（删两处 emit）
- Modify: `src/ctx_weft/core/orchestrator/session_manager.py:233-236`（删 emit）
- Test: `tests/unit/test_agent_registry_events.py`（新建）

**Interfaces:**
- Consumes: Task 3 的 `instantiate`。
- Produces: `instantiate` 现在收 `task_id: str | None = None`（`AgentSpawned` / `SpawnRejected` 的 envelope 需要它），并在内部按下列顺序发事件。

**因果顺序必须保持**（`runtime.py:2750-2757` 的注释）：`AgentSpawned` 先、`AgentInstantiated` 后。前者的主语是**父 agent 的一次 spawn 动作**（与 `SpawnRejected` 配对，构成对每次 spawn 尝试的完整审计），后者的主语是这个 agent 自己的出身。root agent 没有 spawn 动作 → 由 `parent_agent_id is None` 决定只发后者，**不需要调用方传标志**。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_agent_registry_events.py
"""agent 域四条事件的唯一发射点是 AgentRegistry。

搬迁之前：AgentInstantiated 由 session_manager.py:233（root）与
runtime.py:2713（子 agent）两处发，AgentSpawned/SpawnRejected 在
runtime.py —— 而深度判定（SpawnDepthExceeded）本来就在 LM 里。
判定在这边、事件在那边。
"""
from __future__ import annotations

import pytest

from ctx_weft.core.orchestrator.lifecycle_manager import (
    LifecycleManager,
    SpawnDepthExceeded,
)
from ctx_weft.core.orchestrator.template_lookup import TemplateLookup
from ctx_weft.protocols.events import EventType
from tests.integration.test_minimal_loop import (
    InlineAgentTemplateProvider,
    make_echo_template,
)

pytestmark = pytest.mark.asyncio

TPL = "agent__tpl_echo"


class _Bus:
    def __init__(self):
        self.events = []

    async def emit(self, ev):
        self.events.append(ev)

    def types(self):
        return [e.type for e in self.events]


def _lm(bus, *, max_depth=3):
    tmpl = make_echo_template()
    tmpl.loop_config.max_spawn_depth = max_depth
    provider = InlineAgentTemplateProvider([tmpl])
    lm = LifecycleManager(
        template_lookup=TemplateLookup(providers=[provider]), event_bus=bus)
    lm.register_session("s1", tenant_id="default", fallback_template_id=TPL)
    return lm


async def test_root_emits_only_instantiated():
    bus = _Bus()
    lm = _lm(bus)
    await lm.instantiate(template_id=TPL, session_id="s1", tenant_id="default")
    assert bus.types() == [EventType.AGENT_INSTANTIATED]


async def test_child_emits_spawned_then_instantiated():
    """因果顺序：先记「这次 spawn 被准了」，再记「诞生的 agent 长这样」。"""
    bus = _Bus()
    lm = _lm(bus)
    root, _ = await lm.instantiate(
        template_id=TPL, session_id="s1", tenant_id="default")
    bus.events.clear()
    await lm.instantiate(
        template_id=TPL, session_id="s1", tenant_id="default",
        parent_agent_id=root.id, task_id="tsk_1")
    assert bus.types() == [EventType.AGENT_SPAWNED, EventType.AGENT_INSTANTIATED]


async def test_depth_exceeded_emits_spawn_rejected_and_raises():
    """SpawnRejected 的 envelope agent_id 填**父**——子 agent 没诞生，没有 id 可填。"""
    bus = _Bus()
    lm = _lm(bus, max_depth=0)
    root, _ = await lm.instantiate(
        template_id=TPL, session_id="s1", tenant_id="default")
    bus.events.clear()
    with pytest.raises(SpawnDepthExceeded):
        await lm.instantiate(
            template_id=TPL, session_id="s1", tenant_id="default",
            parent_agent_id=root.id, task_id="tsk_1")
    assert bus.types() == [EventType.SPAWN_REJECTED]
    assert bus.events[0].agent_id == root.id


async def test_materialize_emits_nothing():
    bus = _Bus()
    lm = _lm(bus)
    agent, _ = await lm.instantiate(
        template_id=TPL, session_id="s1", tenant_id="default")
    bus.events.clear()
    lm.materialize(agent.id, context_limit=1, reserved_output_tokens=1)
    assert bus.events == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_agent_registry_events.py -v`
Expected: FAIL —— `LifecycleManager.__init__() got an unexpected keyword argument 'event_bus'`。

- [ ] **Step 3: 在 Registry 里发事件**

加 `event_bus: EventBus` 字段。`instantiate` 内部：深度检查失败 → emit `SPAWN_REJECTED`（envelope `agent_id=parent_agent_id`，payload `{"reason": "depth_limit", "fallback_to_inline": False, "attempted_subtask_id": task_id}`）后抛 `SpawnDepthExceeded`；成功且 `parent_agent_id is not None` → emit `AGENT_SPAWNED`（payload `{"parent_agent_id": parent_agent_id, "subtask_id": task_id}`）；然后无条件 emit `AGENT_INSTANTIATED`（payload `{"template_id": tmpl.id, "template_version": tmpl.version}`）。

**payload 与 envelope 逐字段照抄今天 `runtime.py:2726-2790` 与 `session_manager.py:233-236` 的形状**——本 Task 只换发射者，不改内容。

- [ ] **Step 4: 删调用方的事件与判定**

`session_manager.py:233-236`：删 `await self._emit(EventType.AGENT_INSTANTIATED, ...)`。
`runtime.py`：删 `is_new_agent = not t.assigned_agent_id`（`:2643`）、`except SpawnDepthExceeded:` 整块（`:2651-2675`）、`if is_new_agent:` 下的两处 emit（`:2676-2720`）。
分支判据改成：`t.assigned_agent_id` 为空 → `instantiate`（Registry 内部会发事件）；有值 → `materialize`（零事件）。`SpawnDepthExceeded` 现在由 `assemble` 的调用方原样上抛，行为与今天一致（今天 `except` 块里除了发事件也是继续抛）。

- [ ] **Step 5: 跑测试 + 验收网**

Run:
```bash
uv run pytest tests/unit/test_agent_registry_events.py -v
uv run pytest tests/integration/test_spawn_rejected_event.py \
              tests/integration/test_subagent_instantiated_event.py \
              tests/unit/test_agent_template_id_replay.py -v
uv run pytest
```
Expected: 全 PASS。**那三个集成测试必须未改动即通过——这是本 Task 的正确性判据。**

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(agent): agent 域四条事件收进 Registry，唯一发射点

AgentInstantiated 从两处（session_manager root + runtime 子 agent）收成一处；
AgentSpawned / SpawnRejected 从 runtime 收进来——深度判定（SpawnDepthExceeded）
本来就在这里，此前是「判定在这边、事件在那边」。

调用方的 is_new_agent 计算与 except SpawnDepthExceeded 块随之删除：
新建与水合已经是两个方法，Registry 自己知道这次是不是新建。

因果顺序保持：AgentSpawned 先（主语是父 agent 的一次 spawn 动作，与
SpawnRejected 配对），AgentInstantiated 后（主语是这个 agent 的出身）。

验收：test_spawn_rejected_event / test_subagent_instantiated_event /
test_agent_template_id_replay 三个既有测试未改一行仍然通过。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: `load()` 吸收 `agents_from_projection`

恢复链今天已经存在，只是终点接在 `TaskRunner` 的缓存上。换终点，不是加路。

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/lifecycle_manager.py`
- Modify: `src/ctx_weft/core/runtime.py:1495`、删 `:1338` `:2685` `:2698` 的 `pre_resolved_agents` / `_resolved_agents`、`:2711` 的父查找
- Delete: `src/ctx_weft/core/control/converters.py` 的 `agents_from_projection`
- Test: `tests/unit/test_agents_from_projection.py` → 改写为 `tests/unit/test_agent_registry_load.py`

**Interfaces:**
- Produces:
  ```python
  async def load(
      self, agent_views: dict[str, AgentView], *,
      session_id: str, tenant_id: str, fallback_template_id: str,
  ) -> int   # 返回装填条数
  ```
  **是 async**：`AgentView` 只有 `id` / `spawn_depth` / `parent_agent_id` / `template_id` 四个字段，`memory_config` / `loop_config` 要用 `template_lookup` 重新解析。

> **喂进来，不是查回去。** Registry 不订阅 reducer、不订阅总线。reducer 折出 `AgentView` 是它自己的事；恢复路径显式调 `load()`。装填之后 Registry 只读自己内存，绝不回落 scan 事件——与 `rebuild_hitl` 同一条纪律。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_agent_registry_load.py
"""恢复是「喂进来」——recover_session 折出 AgentView，显式装填进 Registry。

替代 tests/unit/test_agents_from_projection.py：那个函数已并入 load()。
行为差异（有意的）：装填出来的 record 带**真正的 template 配置**，
而不是 agents_from_projection 留下的 dataclass 默认值。
"""
from __future__ import annotations

import pytest

from ctx_weft.core.control.types import AgentView
from ctx_weft.core.orchestrator.lifecycle_manager import LifecycleManager
from ctx_weft.core.orchestrator.template_lookup import TemplateLookup
from tests.integration.test_minimal_loop import (
    InlineAgentTemplateProvider,
    make_echo_template,
)

pytestmark = pytest.mark.asyncio

TPL = "agent__tpl_echo"


def _lm():
    provider = InlineAgentTemplateProvider([make_echo_template()])
    return LifecycleManager(
        template_lookup=TemplateLookup(providers=[provider]), event_bus=None)


async def test_load_fills_records_from_views():
    lm = _lm()
    views = {
        "agt_a": AgentView(id="agt_a", spawn_depth=0, parent_agent_id=None,
                           template_id=TPL),
        "agt_b": AgentView(id="agt_b", spawn_depth=1, parent_agent_id="agt_a",
                           template_id=TPL),
    }
    n = await lm.load(views, session_id="s1", tenant_id="default",
                      fallback_template_id=TPL)
    assert n == 2
    assert lm.has("agt_a") and lm.has("agt_b")
    assert lm._agents["agt_b"].spawn_depth == 1
    assert lm._agents["agt_b"].parent_agent_id == "agt_a"


async def test_load_resolves_template_config_not_dataclass_defaults():
    """行为变化，且是变正确了：agents_from_projection 留的是 dataclass 默认值。"""
    lm = _lm()
    tmpl = make_echo_template()
    views = {"agt_a": AgentView(id="agt_a", template_id=TPL)}
    await lm.load(views, session_id="s1", tenant_id="default",
                  fallback_template_id=TPL)
    got = lm.materialize("agt_a", context_limit=1, reserved_output_tokens=1)
    assert got.memory_config == tmpl.memory_config
    assert got.loop_config == tmpl.loop_config


async def test_empty_template_id_falls_back():
    """存量事件流里子 agent 没发过 AgentInstantiated → template_id 为空。

    回落而非报错是刻意的：授权按模板做策略，重启后把未知模板判成
    「无权限」会让老会话直接跑不动。
    """
    lm = _lm()
    views = {"agt_a": AgentView(id="agt_a", template_id="")}
    await lm.load(views, session_id="s1", tenant_id="default",
                  fallback_template_id=TPL)
    assert lm.template_id_of("agt_a")


async def test_load_is_idempotent():
    lm = _lm()
    views = {"agt_a": AgentView(id="agt_a", template_id=TPL)}
    await lm.load(views, session_id="s1", tenant_id="default",
                  fallback_template_id=TPL)
    await lm.load(views, session_id="s1", tenant_id="default",
                  fallback_template_id=TPL)
    assert len(lm._agents) == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_agent_registry_load.py -v`
Expected: FAIL —— `AttributeError: ... has no attribute 'load'`。

- [ ] **Step 3: 实现 `load`**

对每个 view：`register_session` 一次（幂等），解析 `view.template_id or fallback_template_id` 拿 template，登记 `_AgentRecord`。模板解析失败 → `logger.warning` + 用 `MemoryConfig()` / `LoopConfig()` 默认值继续（**绝不抛**，这条路在 `recover()` 的每个 session 上都要过）。

- [ ] **Step 4: 换终点，删缓存**

`runtime.py:1495`：`pre_resolved = agents_from_projection(...)` → `await self._agent_registry.load(view.agents, session_id=session_id, tenant_id=sess_proj.tenant_id, fallback_template_id=template_id)`。
删 `_run_session_tasks` / `TaskRunner` 的 `pre_resolved_agents` 参数（`:1322` `:2607`）与 `self._resolved_agents`（`:2698` `:2804` `:2817`）。
`:2711` 的父查找 `self._resolved_agents.get(t.creator_agent_id)` → 直接把 `t.creator_agent_id` 作为 `parent_agent_id` 传给 `instantiate`（Task 3 已把签名改成收 id）。
删 `converters.py` 的 `agents_from_projection` 与 `runtime.py:1403` 的 import。
`git rm tests/unit/test_agents_from_projection.py`。

- [ ] **Step 5: 跑测试**

Run: `uv run pytest tests/unit/test_agent_registry_load.py -v && uv run pytest`
Expected: 全 PASS。**重点确认恢复相关的集成测试**——若有测试依赖「恢复出来的 agent 带 dataclass 默认 config」，那是本 Task 有意修正的行为，更新该测试的期望并在提交信息里点名。

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(agent): 恢复装填改喂进 Registry，删 agents_from_projection

链路今天已经存在，只是终点接在 TaskRunner 的 _resolved_agents 缓存上：
recover_session → agents_from_projection(view.agents) → pre_resolved。
换成 registry.load(view.agents)，Registry 就是那个缓存。

行为变化（有意）：装填出来的 agent 从 template 拿到真正的 memory_config /
loop_config，而不是 agents_from_projection 留下的 dataclass 默认值。

Registry 不订阅 reducer 也不订阅总线——装填之后只读自己内存，
绝不回落 scan 事件（与 rebuild_hitl 同一条纪律）。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: 改名 `LifecycleManager` → `AgentRegistry`

**纯文本替换，单独一次提交。**前五步都是行为性改动，混在一起 diff 没法审。

**Files:**
- Rename: `src/ctx_weft/core/orchestrator/lifecycle_manager.py` → `agent_registry.py`
- Modify: `src/ctx_weft/core/orchestrator/__init__.py:1,5,13`
- Modify: `src/ctx_weft/core/orchestrator/session_manager.py:12,73,197`（字段名 `lifecycle_manager` → `agent_registry`）
- Modify: `src/ctx_weft/core/runtime.py:3,62,607,...`（`self._agent_registry`、`TaskRunner._lm` → `_registry`）
- Modify: `src/ctx_weft/core/orchestrator/capability_cache.py:3`（注释）
- Modify: `ARCHITECTURE.md`、`docs/events-v2.md`
- Modify: Task 1–5 新建的四个测试文件的 import

> 类名**不受** V2 §7「新名字绝不复用曾发射过的字符串」约束——那条规则管的是事件类型名。
> `SpawnDepthExceeded` / `UnknownCapabilityError` 随文件移动。后者定义在本文件但本文件不抛它，要不要挪去 `core/errors.py` 是另一件事，本计划不处理。

- [ ] **Step 1: git mv + 全局替换**

```bash
git mv src/ctx_weft/core/orchestrator/lifecycle_manager.py \
       src/ctx_weft/core/orchestrator/agent_registry.py
grep -rl "LifecycleManager\|lifecycle_manager" src tests docs ARCHITECTURE.md \
  | xargs sed -i 's/LifecycleManager/AgentRegistry/g; s/lifecycle_manager/agent_registry/g'
sed -i 's/self\._lm\b/self._registry/g' src/ctx_weft/core/runtime.py
```

- [ ] **Step 2: 跑全量 + lint**

Run: `uv run pytest && uv run ruff check src tests`
Expected: 全 PASS，无 lint 报错。

- [ ] **Step 3: 确认没有漏网**

Run: `grep -rn "LifecycleManager\|lifecycle_manager" src tests docs ARCHITECTURE.md`
Expected: 无输出。

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor(agent): LifecycleManager 改名 AgentRegistry

它不「管理生命周期」——它是 agent 的登记处兼工厂。改名后与旁边两个
「有行为的 Manager」（SessionManager 是状态机、TaskManager 是调度器）
区分得更清楚。纯文本替换，无行为变化。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

# 批次 B · LLM 归属（**host 同批次**）

### Task 7: `ModelChoice` / `ResolvedModel` 与解析收口

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/agent_registry.py`
- Modify: `src/ctx_weft/core/loop/llm_gateway.py:78-96`
- Modify: `src/ctx_weft/core/loop/state.py`（`LoopState` 加 `resolved_model`）
- Delete: `src/ctx_weft/core/runtime.py:762-784`（`_sync_session_llm_window`）+ 两处调用、`:2589-2594`（回填）
- Test: `tests/unit/test_model_resolution.py`（新建）

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class ModelChoice:
      account: str = ""      # "" = 用 resolver 的默认
      model: str = ""

  @dataclass(frozen=True)
  class ResolvedModel:
      client: LLMClient
      account: str           # 实际身份 —— 事件报这个
      model: str
      context_limit: int
      reserved_output_tokens: int

  class ModelResolver(Protocol):
      def __call__(self, account: str, model: str) -> LLMClient: ...

  # AgentRegistry
  model_resolver: ModelResolver              # 构造期注入，无默认值
  def resolve_model(self, agent_id: str) -> ResolvedModel
  def materialize(self, agent_id: str) -> tuple[Agent, ResolvedModel]   # 签名变了
  ```
  `_AgentRecord` 加 `llm: ModelChoice`。`instantiate` 加 `llm: ModelChoice | None = None`（`None` → 从 `parent_agent_id` 的 record 继承；无父则 `ModelChoice()`）。

**三样东西各归各位**：

| | 是什么 | 存哪 |
|---|---|---|
| **选择** | `(account, model)`，**可以全空** = 用账号默认 | `_AgentRecord.llm`，进事件 |
| **client** | 解析出的 `LLMClient` | 不存，派发时现解 |
| **身份** | `(client.account, client.model)` | 不存，进 LLM 事件 |

> **回填必须删。** `runtime.py:2625-2630` 把「身份」写回「选择」，它的注释自陈理由是「事件层 `resolve_llm_identity` 以 session 为真值，空则误报 `"mock"`」——为了修一个读错对象的问题去写另一个对象。副作用是**账号默认被冻结**：`("", "")` 的含义是「跟随账号默认」，回填把它钉成具体模型名，此后 host 改账号默认对这个进程里的会话不再生效。

> **Registry 不缓存 client。** 缓存客户端是 `LLMClientResolver` 的职责；Registry 再存一份就有第二个缓存和它自己的失效问题（host 换了账号凭据 → 陈旧 client 继续被用）。`get_client` 是纯查表（`runtime.py:913` 注释），每次派发一次，与今天开销相同。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_model_resolution.py
"""「用哪个 client」和「窗口多大」是同一次解析的两面。

LLMClient 协议本来就把 context_limit / output_reserve 定义成抽象属性
（protocols/llm.py:270-279），不是 duck-type 的额外物——所以窗口永远
从 client 现读，一次都不必存。
"""
from __future__ import annotations

import pytest

from ctx_weft.core.orchestrator.agent_registry import (
    AgentRegistry,
    ModelChoice,
)
from ctx_weft.core.orchestrator.template_lookup import TemplateLookup
from tests.integration.test_minimal_loop import (
    InlineAgentTemplateProvider,
    make_echo_template,
)

pytestmark = pytest.mark.asyncio

TPL = "agent__tpl_echo"


class _Client:
    def __init__(self, account="acct_default", model="mdl_default",
                 context_limit=200_000, output_reserve=8192):
        self.account, self.model = account, model
        self.context_limit, self.output_reserve = context_limit, output_reserve


class _Bus:
    def __init__(self):
        self.events = []

    async def emit(self, ev):
        self.events.append(ev)


def _reg(resolver=None):
    provider = InlineAgentTemplateProvider([make_echo_template()])
    reg = AgentRegistry(
        template_lookup=TemplateLookup(providers=[provider]),
        event_bus=_Bus(),
        model_resolver=resolver or (lambda a, m: _Client()),
    )
    reg.register_session("s1", tenant_id="default", fallback_template_id=TPL)
    return reg


async def test_empty_choice_takes_identity_from_client():
    """("", "") 的含义是「跟随账号默认」——身份由 client 报，不回写 choice。"""
    reg = _reg()
    agent, _ = await reg.instantiate(
        template_id=TPL, session_id="s1", tenant_id="default")
    rm = reg.resolve_model(agent.id)
    assert (rm.account, rm.model) == ("acct_default", "mdl_default")
    assert reg._agents[agent.id].llm == ModelChoice()   # 选择仍是空，未被冻结


async def test_explicit_choice_wins():
    reg = _reg()
    agent, _ = await reg.instantiate(
        template_id=TPL, session_id="s1", tenant_id="default",
        llm=ModelChoice(account="acct_x", model="mdl_x"))
    rm = reg.resolve_model(agent.id)
    assert (rm.account, rm.model) == ("acct_x", "mdl_x")


async def test_window_comes_from_client_and_is_stamped_into_loop_guard():
    reg = _reg(resolver=lambda a, m: _Client(context_limit=42, output_reserve=7))
    agent, _ = await reg.instantiate(
        template_id=TPL, session_id="s1", tenant_id="default")
    got, rm = reg.materialize(agent.id)
    assert rm.context_limit == 42
    assert got.loop_guard.context_limit == 42
    assert got.loop_guard.reserved_output_tokens == 7


async def test_child_inherits_parent_choice():
    """决定①：继承**派生它的那个 agent**，不是 root。"""
    reg = _reg()
    root, _ = await reg.instantiate(
        template_id=TPL, session_id="s1", tenant_id="default",
        llm=ModelChoice(account="a1", model="m1"))
    child, _ = await reg.instantiate(
        template_id=TPL, session_id="s1", tenant_id="default",
        parent_agent_id=root.id, task_id="tsk_1")
    assert reg._agents[child.id].llm == ModelChoice(account="a1", model="m1")


async def test_registry_does_not_cache_the_client():
    """缓存客户端是 LLMClientResolver 的职责；Registry 存第二份就有第二个失效问题。"""
    calls = []

    def resolver(a, m):
        calls.append((a, m))
        return _Client()

    reg = _reg(resolver=resolver)
    agent, _ = await reg.instantiate(
        template_id=TPL, session_id="s1", tenant_id="default")
    calls.clear()
    reg.resolve_model(agent.id)
    reg.resolve_model(agent.id)
    assert len(calls) == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_model_resolution.py -v`
Expected: FAIL —— `ImportError: cannot import name 'ModelChoice'`。

- [ ] **Step 3: 实现三个类型与两个方法**

```python
def resolve_model(self, agent_id: str) -> ResolvedModel:
    choice = self._agents[agent_id].llm
    client = self.model_resolver(choice.account, choice.model)
    return ResolvedModel(
        client=client,
        account=choice.account or getattr(client, "account", ""),
        model=choice.model or getattr(client, "model", ""),
        context_limit=client.context_limit,
        reserved_output_tokens=client.output_reserve,
    )
```

`materialize` 改成返回 `(Agent, ResolvedModel)`，`loop_guard` 从 `rm` stamp。`instantiate` 的 `llm=None` → 继承 `parent_agent_id` 的 record，无父则 `ModelChoice()`。

- [ ] **Step 4: 接线并删三处**

`runtime.py`：`self._agent_registry` 构造时传 `model_resolver=self._resolve_llm`。
`TaskRunner.assemble` 把 `materialize` 返回的 `ResolvedModel` 放进 `AgentBinding.model`（`AgentBinding` 加 `model: ResolvedModel | None = None` 字段）。
`_execute_task` 从 `binding.model.client` 取 llm，不再调 `_resolve_llm`；`LoopState` 加 `resolved_model` 并由此赋值。
**删**：`_sync_session_llm_window`（`:759-781`）+ `recover_session` / `_resume_in_existing_tm` 两处调用；回填（`:2589-2594`）。
`llm_gateway.py:78-96`：

```python
def resolve_llm_identity(state) -> tuple[str, str]:
    """本次 LLM 调用实际使用的 (model, account)。

    真值是 state.resolved_model —— 派发时由 AgentRegistry 解出的那一个。
    此前读 session.llm_model 并两级兜底到 agent.runtime / "mock"，那两级
    永远命中不了（runtime 从不填 agent.runtime["llm_model"]），于是未配置
    时恒报 "mock"。ResolvedModel 永远是解析过的确定值，报不出假数据。
    """
    rm = state.resolved_model
    return rm.model, rm.account
```

- [ ] **Step 5: 更新 Task 3 留下的 materialize 测试**

`materialize` 的签名在本 Task 变了（去掉两个窗口参数、返回 `(Agent, ResolvedModel)`）。改 `tests/unit/test_agent_registry_materialize.py`：

```python
async def test_materialize_carries_template_config():
    reg = _reg()
    agent, tmpl = await reg.instantiate(
        template_id=TPL, session_id="s1", tenant_id="default")
    got, rm = reg.materialize(agent.id)          # 不再收窗口参数
    assert got.template_id == tmpl.id
    assert got.memory_config == tmpl.memory_config
    assert got.loop_guard.context_limit == rm.context_limit   # 从 client 派生
```

`test_materialize_is_a_fresh_object_each_call` 与 `test_materialize_unknown_id_falls_back_and_never_raises` 同样去掉窗口实参、解包两个返回值。

- [ ] **Step 6: 跑测试**

Run: `uv run pytest tests/unit/test_model_resolution.py tests/unit/test_agent_registry_materialize.py -v && uv run pytest`
Expected: 全 PASS。**若有测试断言 `LLMRequestStarted.model == "mock"`，那是旧兜底的产物**——改成断言 mock adapter 实际公开的 model。

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat(agent): LLM 的真相源从 session 移到 agent record

三样东西此前挤成一样：选择（可空，= 跟随账号默认）、解析出的 client、
实际身份。runtime.py:2625 把身份回填进选择，唯一目的是让事件层报得出
模型名——为了修一个读错对象的问题去写另一个对象，副作用是账号默认被冻结。

现在：choice 住在 _AgentRecord.llm，client 由注入的 ModelResolver 现解、
不缓存（那是 LLMClientResolver 的职责），窗口从 client 的 context_limit /
output_reserve 派生并 stamp 进 loop_guard。子 agent 继承派生它的那个 agent。

删除：_sync_session_llm_window（窗口跟着 client 走，没有「对齐」这个动作）、
session 真值回填、resolve_llm_identity 的两级兜底。
_resolve_llm 从 5 个调用点收成 1 个。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: 事件与投影

**Files:**
- Modify: `src/ctx_weft/protocols/events.py`（新增 `AGENT_LLM_CHANGED`）
- Modify: `src/ctx_weft/core/control/types.py:70-80`（`AgentView` 加两个字段）
- Modify: `src/ctx_weft/core/control/reducers.py:474-481`（`AGENT_INSTANTIATED` 分支）+ 新增 `AGENT_LLM_CHANGED` 分支
- Modify: `src/ctx_weft/core/orchestrator/agent_registry.py`（`AgentInstantiated` payload 加字段、`load` 读新字段）
- Test: `tests/unit/test_agent_llm_replay.py`（新建）

**Interfaces:**
- Produces：
  - `EventType.AGENT_LLM_CHANGED = "AgentLlmChanged"`（**S 档**，从未发射过的新字符串 ✓）
  - `AgentInstantiated.payload` 加 `llm_account` / `llm_model`（S 档只可加字段 ✓）
  - `AgentView` 加 `llm_account: str = ""` / `llm_model: str = ""`

> **host 同批次**：`AgentLlmChanged` 进 `STATE_EVENT_TYPES`，host 的 `projection_updater.py` 要同步加分支（V2 §6 不变式 3：唯一测不严的地方）。**必须写进 host 迁移文档。**

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_agent_llm_replay.py
"""agent 的模型选择跨重启存活——这是 D1「切换在重放里不存在」的修复。

D1：SessionResumed 的 payload 带 llm_model，但 reducers.py:405-412 那个
分支不读它；全仓唯一写 SessionView.llm_model 的地方是 SessionCreated。
于是任何一次切换都不进投影，recover_session 每次把会话拉回创建时的模型。
"""
from __future__ import annotations

import pytest

from ctx_weft.core.control.reducers import reduce_events
from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols.events import Event, EventType

pytestmark = pytest.mark.asyncio


def _ev(t, agent_id, payload):
    return Event(
        id=generate_id("evt"), run_id=None, sequence=0, session_id="s1",
        type=t, timestamp=now_utc(), agent_id=agent_id, payload=payload,
    )


def test_instantiated_carries_choice_into_view():
    view = reduce_events([
        _ev(EventType.AGENT_INSTANTIATED, "agt_a",
            {"template_id": "tpl", "template_version": "1",
             "llm_account": "acct_a", "llm_model": "mdl_a"}),
    ])
    av = view.agents["agt_a"]
    assert (av.llm_account, av.llm_model) == ("acct_a", "mdl_a")


def test_llm_changed_overrides():
    view = reduce_events([
        _ev(EventType.AGENT_INSTANTIATED, "agt_a",
            {"template_id": "tpl", "template_version": "1",
             "llm_account": "acct_a", "llm_model": "mdl_a"}),
        _ev(EventType.AGENT_LLM_CHANGED, "agt_a",
            {"llm_account": "acct_b", "llm_model": "mdl_b",
             "reason": "user_selected"}),
    ])
    av = view.agents["agt_a"]
    assert (av.llm_account, av.llm_model) == ("acct_b", "mdl_b")


def test_llm_changed_does_not_touch_task_or_session_state():
    """纯赋值：不入队、不改任何 task 状态、不触发调度。"""
    view = reduce_events([
        _ev(EventType.AGENT_INSTANTIATED, "agt_a", {"template_id": "tpl"}),
        _ev(EventType.AGENT_LLM_CHANGED, "agt_a",
            {"llm_account": "a", "llm_model": "m", "reason": "user_selected"}),
    ])
    assert view.session_status == "RUNNING"   # 未被这条事件改动
    assert view.tasks == {}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_agent_llm_replay.py -v`
Expected: FAIL —— `AttributeError: type object 'EventType' has no attribute 'AGENT_LLM_CHANGED'`。

- [ ] **Step 3: 加事件类型与投影字段**

`events.py`：在 agent 域附近加 `AGENT_LLM_CHANGED = "AgentLlmChanged"  # payload: {llm_account, llm_model, reason}`。
`types.py` 的 `AgentView` 加两个 `str = ""` 字段。
`reducers.py`：`AGENT_INSTANTIATED` 分支补写两个字段（**空值不覆盖**，与 `template_id` 的既有处理同口径）；新增

```python
elif t == EventType.AGENT_LLM_CHANGED and ev.agent_id:
    # 纯赋值：agent 的模型选择变了。不改任何 task / session 状态——
    # 「换模型」和「让 task 跑起来」是两件事（见 spec §06 的三条命令）。
    slot = _agent_slot(view, ev.agent_id)
    slot.llm_account = p.get("llm_account", "")
    slot.llm_model = p.get("llm_model", "")
```

把 `AGENT_LLM_CHANGED` 加进 `STATE_EVENT_TYPES`。

- [ ] **Step 4: 发射侧与装填侧**

`agent_registry.instantiate` 的 `AGENT_INSTANTIATED` payload 加 `"llm_account": choice.account, "llm_model": choice.model`。
`load()` 从 `view.llm_account` / `view.llm_model` 填 `_AgentRecord.llm`。

- [ ] **Step 5: 跑测试**

Run: `uv run pytest tests/unit/test_agent_llm_replay.py -v && uv run pytest`
Expected: 全 PASS。

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(events): AgentLlmChanged（S 档）+ AgentInstantiated 携带模型选择

修 D1：切换在重放里不存在。SessionResumed 的 payload 带 llm_model 但
reducer 不读它，全仓唯一写 SessionView.llm_model 的是 SessionCreated——
于是每次 recover_session 都把会话拉回创建时的模型，host 不重新传就静默降级。
这才是 ResumeHint 的真实职责，现在它没有存在的理由了。

BREAKING(host): AgentLlmChanged 是 S 档，projection_updater.py 需同批次加分支。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: 两条切换命令 + 切断续跑路径

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/agent_registry.py`
- Modify: `src/ctx_weft/core/runtime.py`（新增两个转发方法；删续跑路径的 llm 参数）
- Modify: `src/ctx_weft/protocols/hitl.py:112-128`（删 `ResumeHint` 与 `HitlReply.resume_hint`）
- Modify: `src/ctx_weft/protocols/__init__.py:76,176`
- Test: `tests/unit/test_set_llm_commands.py`（新建）

**Interfaces:**
- Produces:
  ```python
  # AgentRegistry
  async def set_agent_llm(self, agent_id: str, choice: ModelChoice, *,
                          reason: str, causation_id: str | None = None) -> bool
  async def set_session_llm(self, session_id: str, choice: ModelChoice, *,
                            reason: str) -> int

  # CtxWeftRuntime（host 入口）
  async def set_agent_llm(self, agent_id, *, llm_account="", llm_model="",
                          reason="user_selected") -> bool
  async def set_session_llm(self, session_id, *, llm_account="", llm_model="",
                            reason="user_selected") -> int
  ```

**`llm_*` 参数只留在创建路径：**

| 保留 | 删 |
|---|---|
| `RunParams` / `RunParams.create`（`:389,405`） | `recover_session` + 两个内部转发（`:1356,1380,1404`） |
| `start_session` / `run_session`（`:901`） | `_resume_in_existing_tm`（`:1674`） |
| `_resolve_llm`（`:628`）→ 注入的 `ModelResolver` | `_execute_task`（`:2610`） |
| | `TaskRunner._llm_account` / `_llm_model`（`:2704`） |
| | `HitlReply.resume_hint` + `ResumeHint` 类型 |

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_set_llm_commands.py
"""换模型只有两条命令，且都是纯赋值。

set_session_llm 发的是 N 条 AgentLlmChanged，不是一条会话级事件——
真相源因此仍然唯一，reducer 不必处理「一条事件改 N 个实体」。
host 要展示「这是一次会话级切换」→ 按 causation_id 聚合。
"""
from __future__ import annotations

import inspect

import pytest

from ctx_weft.core.orchestrator.agent_registry import AgentRegistry, ModelChoice
from ctx_weft.core.orchestrator.template_lookup import TemplateLookup
from ctx_weft.protocols.events import EventType
from tests.integration.test_minimal_loop import (
    InlineAgentTemplateProvider,
    make_echo_template,
)

pytestmark = pytest.mark.asyncio

TPL = "agent__tpl_echo"


class _Client:
    account, model = "a0", "m0"
    context_limit, output_reserve = 1000, 100


class _Bus:
    def __init__(self):
        self.events = []

    async def emit(self, ev):
        self.events.append(ev)


def _reg():
    provider = InlineAgentTemplateProvider([make_echo_template()])
    reg = AgentRegistry(
        template_lookup=TemplateLookup(providers=[provider]),
        event_bus=_Bus(),
        model_resolver=lambda a, m: _Client(),
    )
    reg.register_session("s1", tenant_id="default", fallback_template_id=TPL)
    reg.register_session("s2", tenant_id="default", fallback_template_id=TPL)
    return reg


async def test_set_agent_llm_emits_and_returns_true():
    reg = _reg()
    a, _ = await reg.instantiate(template_id=TPL, session_id="s1", tenant_id="default")
    reg.event_bus.events.clear()
    changed = await reg.set_agent_llm(
        a.id, ModelChoice(account="x", model="y"), reason="user_selected")
    assert changed is True
    assert [e.type for e in reg.event_bus.events] == [EventType.AGENT_LLM_CHANGED]
    assert reg._agents[a.id].llm == ModelChoice(account="x", model="y")


async def test_same_choice_is_a_noop():
    """host 重复点击不刷屏。"""
    reg = _reg()
    a, _ = await reg.instantiate(template_id=TPL, session_id="s1", tenant_id="default")
    await reg.set_agent_llm(a.id, ModelChoice(account="x"), reason="user_selected")
    reg.event_bus.events.clear()
    changed = await reg.set_agent_llm(a.id, ModelChoice(account="x"), reason="user_selected")
    assert changed is False
    assert reg.event_bus.events == []


async def test_set_session_llm_emits_n_events_sharing_causation_id():
    reg = _reg()
    a, _ = await reg.instantiate(template_id=TPL, session_id="s1", tenant_id="default")
    b, _ = await reg.instantiate(template_id=TPL, session_id="s1", tenant_id="default")
    other, _ = await reg.instantiate(template_id=TPL, session_id="s2", tenant_id="default")
    reg.event_bus.events.clear()

    n = await reg.set_session_llm(
        "s1", ModelChoice(account="x", model="y"), reason="user_selected")

    assert n == 2
    evs = reg.event_bus.events
    assert [e.type for e in evs] == [EventType.AGENT_LLM_CHANGED] * 2
    assert len({e.causation_id for e in evs}) == 1
    assert evs[0].causation_id is not None
    assert {e.agent_id for e in evs} == {a.id, b.id}
    # 另一个 session 不受影响
    assert reg._agents[other.id].llm == ModelChoice()


async def test_set_llm_touches_no_task_state():
    """纯赋值：不入队、不改任何 task 状态、不触发调度。"""
    reg = _reg()
    a, _ = await reg.instantiate(template_id=TPL, session_id="s1", tenant_id="default")
    await reg.set_agent_llm(a.id, ModelChoice(account="x"), reason="user_selected")
    assert [e.type for e in reg.event_bus.events][-1] == EventType.AGENT_LLM_CHANGED


def test_resume_hint_is_gone():
    import ctx_weft.protocols as protocols
    from ctx_weft.protocols.hitl import HitlReply
    assert not hasattr(protocols, "ResumeHint")
    assert "resume_hint" not in inspect.signature(HitlReply).parameters


def test_recover_session_takes_no_llm_params():
    from ctx_weft.core.runtime import CtxWeftRuntime
    sig = inspect.signature(CtxWeftRuntime.recover_session)
    assert "llm_account" not in sig.parameters
    assert "llm_model" not in sig.parameters
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_set_llm_commands.py -v`
Expected: FAIL —— `AttributeError: ... has no attribute 'set_agent_llm'`。

- [ ] **Step 3: 实现两条命令**

```python
async def set_agent_llm(self, agent_id, choice, *, reason,
                        causation_id=None) -> bool:
    rec = self._agents.get(agent_id)
    if rec is None or rec.llm == choice:
        return False                       # 未登记 / 同值 → no-op，不发事件
    rec.llm = choice
    await self._emit_llm_changed(agent_id, rec, reason, causation_id)
    return True

async def set_session_llm(self, session_id, choice, *, reason) -> int:
    """作用于该 session 下 registry 持有的**全部** record。

    不去问 TaskManager「哪些还会被派发」——零查询依赖是 Registry 的设计属性。
    已跑完的 agent 改了也无害（不会再被派发），代价只是多几条事件。

    N 条事件共享一个 causation_id：host 要展示「这是一次会话级切换」按它聚合，
    因此不需要第三种事件类型。
    """
    cid = generate_id("cau")
    ids = [k for k, r in self._agents.items() if r.session_id == session_id]
    return sum([
        await self.set_agent_llm(aid, choice, reason=reason, causation_id=cid)
        for aid in ids
    ])
```

runtime 上加两个转发方法（把 `llm_account` / `llm_model` 包成 `ModelChoice`）。

- [ ] **Step 4: 切断续跑路径**

删 `recover_session` / `_run_session_tasks` / `_resume_in_existing_tm` / `_execute_task` / `TaskRunner` 的 `llm_account`、`llm_model` 参数与它们的全部实参。
删 `_resume_after_hitl` 的 `hint` 参数与 `reply_to_hitl` 里的 `reply.resume_hint`。
删 `protocols/hitl.py` 的 `ResumeHint` 类与 `HitlReply.resume_hint` 字段，以及 `protocols/__init__.py` 的 import 与 `__all__` 条目。

- [ ] **Step 5: 跑测试**

Run: `uv run pytest tests/unit/test_set_llm_commands.py -v && uv run pytest`
Expected: 全 PASS。**若有测试构造 `HitlReply(..., resume_hint=...)`，删掉那个实参。**

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(agent): 换模型收敛成 set_agent_llm / set_session_llm 两条命令

续跑路径一概不碰模型：recover_session / _resume_in_existing_tm /
_execute_task / TaskRunner 的 llm_account、llm_model 参数全部删除，
HitlReply.resume_hint 与 ResumeHint 类型一并删除。
规则一句话：llm_* 只出现在「建一个 session」的入参里。

set_session_llm 发 N 条 AgentLlmChanged 而不是一条会话级事件——真相源
因此仍然唯一，host 要展示「这是一次会话级切换」按 causation_id 聚合。
作用于 registry 持有的全部 record，不去问 TM 哪些还会被派发（零查询依赖）。

两条都是纯赋值：不入队、不改任何 task 状态、不触发调度。

BREAKING(host): reply_to_hitl 不再接受 resume_hint。原来一次调用做两件事，
现在是两条：set_agent_llm(view.agent_id, ...) 然后 reply_to_hitl(reply)。
会话级选择器映射到 set_session_llm。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

# 批次 C · task 解除阻塞（独立，可任意时候插入）

### Task 10: `TaskHumanResolved` + `TaskResumed` 映射订正

两个缺陷同源：**「解除阻塞」和「开始执行」是两件事，今天挤在一起。**

- **D4**：`TaskResumed` 全仓只有一个发射点（`task_manager.py:1227`，子任务全终态时父解挂）。HITL 唤醒走的 `resume_task()` 直接 `t.status = "PENDING"`，`_inject_user_reply` 也直接改 `target.status`——两处都不发事件，重放之后这些 task 还停在 `AWAITING_HUMAN`。
- **D5**：`ACTIVE` 的正主是 `TaskStarted`（`task_manager.py:~449（TM 派发时发 TASK_STARTED）`，TM 派发时发并回填 `assigned_agent_id`）。但 `_try_resume_parent` 在**入队之前**就把父任务置成 `ACTIVE`，reducer 的 `TaskResumed → ACTIVE` 是在镜像这个抢跑。

**Files:**
- Modify: `src/ctx_weft/protocols/events.py`（新增 `TASK_HUMAN_RESOLVED`）
- Modify: `src/ctx_weft/core/control/reducers.py:59`（`TASK_RESUMED` 映射）+ 新增 `TASK_HUMAN_RESOLVED` 分支
- Modify: `src/ctx_weft/core/orchestrator/task_manager.py:1254-1272`（`resume_task`）、`:1125`（`_try_resume_parent`）
- Modify: `src/ctx_weft/core/runtime.py:1905-1910`（`_inject_user_reply`）
- Test: `tests/unit/test_task_unblock_events.py`（新建）

**Interfaces:**
- Produces: `EventType.TASK_HUMAN_RESOLVED = "TaskHumanResolved"`（**S 档**，payload `{hitl_id}`，→ `PENDING`）。
  `resume_task(task_id, *, hitl_id: str)` 加一个必填关键字参数。

> **为什么不复用 `TaskRequeued`**——不是因为状态效果不同。两者都是 → `PENDING` 并清旧产出（`_inject_user_reply` 今天做的正是这件事，`runtime.py:1905` 的注释就写着「清旧进展、置 PENDING」）。理由是 `TaskRequeued` **已经背着两义**，各自靠 payload 区分：`{outcome:"retry", retry_count}`（observer 判重试）与 `{reason, user_prompt}`（reopen 重做）。塞进第三义，消费方就得读 payload 才能分辨「重试」「重做」「人答了」——这正是 V2 花一次重构把 `TaskSuspended` 的三义拆成三个类型时反对的东西。**判据是类型，不是 payload。**
> 第二个理由是配对：`TaskAwaitingHuman{hitl_id}` 需要一个带同样 `hitl_id` 的解除事件，这段被挡住的区间才括得起来、才查得出「有 Awaiting 无 Resolved」。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_task_unblock_events.py
"""「解除阻塞」与「开始执行」是两件事。

三对括号里，AwaitingHuman 那一对此前只有左半边：
  TaskSuspended（等子任务）      ←→ TaskResumed
  TaskAwaitingHuman{hitl_id}     ←→ (缺)
  RunInterrupted                 ←→ (缺，范围外)
"""
from __future__ import annotations

from ctx_weft.core.control.reducers import TASK_STATUS_BY_EVENT, reduce_events
from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols.events import Event, EventType


def _ev(t, task_id, payload=None):
    return Event(
        id=generate_id("evt"), run_id=None, sequence=0, session_id="s1",
        type=t, timestamp=now_utc(), task_id=task_id, payload=payload or {},
    )


def _created(task_id):
    return _ev(EventType.TASK_CREATED, task_id,
               {"task": {"id": task_id, "session_id": "s1", "status": "PENDING"}})


def test_human_resolved_returns_task_to_pending():
    view = reduce_events([
        _created("tsk_1"),
        _ev(EventType.TASK_AWAITING_HUMAN, "tsk_1", {"hitl_id": "hit_1"}),
        _ev(EventType.TASK_HUMAN_RESOLVED, "tsk_1", {"hitl_id": "hit_1"}),
    ])
    assert view.tasks["tsk_1"].status == "PENDING"


def test_awaiting_and_resolved_share_the_hitl_id():
    """配对：同一个 hitl_id 把被挡住的区间括起来。"""
    evs = [
        _created("tsk_1"),
        _ev(EventType.TASK_AWAITING_HUMAN, "tsk_1", {"hitl_id": "hit_1"}),
        _ev(EventType.TASK_HUMAN_RESOLVED, "tsk_1", {"hitl_id": "hit_1"}),
    ]
    opened = [e for e in evs if e.type == EventType.TASK_AWAITING_HUMAN]
    closed = [e for e in evs if e.type == EventType.TASK_HUMAN_RESOLVED]
    assert opened[0].payload["hitl_id"] == closed[0].payload["hitl_id"]


def test_task_resumed_maps_to_pending_not_active():
    """ACTIVE 的正主是 TaskStarted —— 它由 TM 在派发时发并回填 assigned_agent_id。
    解挂之后、派发之前，task 在队列里，投影不该说它在跑。"""
    assert TASK_STATUS_BY_EVENT[EventType.TASK_RESUMED] == "PENDING"


def test_active_still_comes_from_task_started():
    view = reduce_events([
        _created("tsk_1"),
        _ev(EventType.TASK_SUSPENDED, "tsk_1", {"summary": "", "spawn_titles": []}),
        _ev(EventType.TASK_RESUMED, "tsk_1"),
    ])
    assert view.tasks["tsk_1"].status == "PENDING"
    view2 = reduce_events([
        _created("tsk_1"),
        _ev(EventType.TASK_STARTED, "tsk_1", {"assigned_agent_id": "agt_1"}),
    ])
    assert view2.tasks["tsk_1"].status == "ACTIVE"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_task_unblock_events.py -v`
Expected: FAIL —— `AttributeError: ... has no attribute 'TASK_HUMAN_RESOLVED'`。

- [ ] **Step 3: 加事件与映射**

`events.py`：`TASK_HUMAN_RESOLVED = "TaskHumanResolved"  # payload: {hitl_id}`。
`reducers.py:59`：`EventType.TASK_RESUMED: "ACTIVE"` → `"PENDING"`；`TASK_STATUS_BY_EVENT` 加 `EventType.TASK_HUMAN_RESOLVED: "PENDING"`。
把 `TASK_HUMAN_RESOLVED` 加进 `STATE_EVENT_TYPES`。

- [ ] **Step 4: 补发射点，去掉抢跑**

`task_manager.py` 的 `resume_task(task_id, *, hitl_id)` 末尾 `await self._emit(EventType.TASK_HUMAN_RESOLVED, task_id=task_id, payload={"hitl_id": hitl_id})`。
`_try_resume_parent`（`:1125`）删 `parent_task.status = "ACTIVE"`，改 `"PENDING"`。
`runtime.py` 的 `_inject_user_reply` 与 `_resume_in_existing_tm` 的调用点传 `hitl_id=req.id`。

- [ ] **Step 5: 跑测试**

Run: `uv run pytest tests/unit/test_task_unblock_events.py -v && uv run pytest`
Expected: 全 PASS。

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(events): TaskHumanResolved + TaskResumed 映射订正

D4：TaskResumed 全仓只有一个发射点（父解挂）。HITL 唤醒走的 resume_task /
_inject_user_reply 直接改 status 不发事件，重放后 task 还停在 AWAITING_HUMAN。
新事件与 TaskAwaitingHuman{hitl_id} 配对，同一个 hitl_id 把被挡住的区间括起来。

不复用 TaskRequeued：不是因为状态效果不同（两者都 → PENDING 并清旧产出），
而是它已经背着两义、各自靠 payload 区分。判据是类型，不是 payload
（docs/events-v2.md §2.3 拆 TaskSuspended 三义时的同一条原则）。

D5：ACTIVE 的正主是 TaskStarted。_try_resume_parent 在入队之前就置 ACTIVE，
reducer 的 TaskResumed → ACTIVE 是在镜像这个抢跑。映射改 PENDING。

BREAKING(host): TaskHumanResolved 是 S 档，projection_updater.py 需同批次加分支。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## 完工验收

- [ ] **全量测试**：`uv run pytest`
- [ ] **Lint**：`uv run ruff check src tests`
- [ ] **V2 不变式 1**（定义即必须发射）：`AGENT_LLM_CHANGED` / `TASK_HUMAN_RESOLVED` 各有发射点且被测试覆盖；无新增的从未发射类型。
- [ ] **无残留引用**：`grep -rn "LifecycleManager\|lifecycle_manager\|_default_agent\|agents_from_projection\|_sync_session_llm_window\|ResumeHint\|resume_hint\|AgentStatus\|_flush_tracking_memory\|pre_resolved_agents\|_resolved_agents" src tests` → 无输出。
- [ ] **`_resolve_llm` 只剩一个调用点**：`grep -rn "_resolve_llm(" src` → 仅 `AgentRegistry` 的注入处。
- [ ] **host 迁移文档**：新建 `docs/upgrade/2026-09-02-agent-llm-ownership.md`，列出三条 host 破坏性变更——`projection_updater.py` 加 `AgentLlmChanged` / `TaskHumanResolved` 两个分支；`reply_to_hitl` 不再接受 `resume_hint`，改发 `set_agent_llm` + `reply_to_hitl` 两条；会话级模型选择器映射到 `set_session_llm`。

## 有意不做

**`delegate_task` 不能指定子 agent 的模型。** 结构上支持（`NormalTaskSettings` 已有 `subagent_template`，加一个 `llm_model` 是同类字段，派生时进 `AgentInstantiated`），但那等于**让 LLM 自己选模型**——`delegate_task` 是模型调的控制工具。这是产品决定，不是结构决定。**先不开**：随时可以加，收回来是破坏性的。子 agent 一律继承派生它的那个 agent，模型只能由人经两条命令改。

## 已知风险

1. **`materialize` 动的是派发热路径。** `_default_agent` 每次派发都跑。Task 3 的缓解是先让 `materialize` 输出与它逐字节一致（含那时还没删的字段），确认等价后再改内部。
2. **`load()` 是行为变化，不是纯搬迁。** 恢复出来的 agent 会从 template 拿到真正的 `memory_config` / `loop_config`，而不是 dataclass 默认值。**这是变正确了，但仍是变化**——Task 5 Step 5 必须确认没有测试依赖那个默认值；若有，更新期望并在提交信息里点名。
3. **`load()` 每个 agent 一次 `template_lookup`。** 会话里 agent 数量通常是个位数，但恢复慢的场景值得留意。
4. **批次 B、C 各自破坏 host 契约**，必须与 host 同批次上线；批次 A 不需要。
