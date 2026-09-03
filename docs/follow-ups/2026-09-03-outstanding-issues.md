# 遗留问题总账（2026-09-03）

三轮所有权重构（会话状态 → task 状态 → agent registry 与 LLM 归属）之后，
对 `src/ctx_weft/` 做了一次系统扫描。本文是**当前全部已知遗留**的清单。

每条都回源核过，带 file:line。分类按「该不该先修」而非按模块。
基线：HEAD `1223ac5`，全量 `tests/unit` 仅 1 条既有失败
（`test_default_role_prompt_uses_two_fields`）。

---

## A. 行为缺陷 —— 会产生错误结果

### A1. `TaskView.outputs` / `error` 恒为 `None`（数据损失）

**数据和读它的人在两条不同的事件上。**

- `TaskFinished` 的 payload **带 `outputs`**（`task_disposition.py:123-125`），
  但 reducer 的 `TASK_FINISHED` 走 `elif t in TASK_STATUS_BY_EVENT` 那条，
  只写 status、**不读 payload**。
- `TaskFinalized` 的 reducer 分支**读** `outputs`/`error`（`reducers.py:580-581`），
  而发射侧只发 `{task_id, outcome}`（`finalize.py:766`）→ **永远读到 `None`**。
- 而 `reducers.py:580` 是投影里 `outputs` 的**唯一非 None 写入点**
  （`:536`、`:552` 都是显式清空）。

**后果**：`converters.py:66` 把空值转回领域对象，任何「崩溃后从事件流重建任务成果」
的路径都拿不到 deliverable。同理 `TaskFailed.error_message` /
`TaskInterrupted.error_message` 也没人读，`TaskView.error` 同样恒空。

**四处测试提供虚假绿灯**：它们手工构造带 `outputs` 的 `TaskFinalized` 喂 reducer，
验证的是分支逻辑而非端到端契约。`golden/14-task-failed.json` 里那个
`{"outputs": null, "error": "boom: tool crashed"}` 的 payload **现实中不存在**。

**两条修法，选择会影响存量可重建性**：改 reducer 从 `TaskFinished`/`TaskFailed` 读
（更顺，那才是携带数据的事件），或补发射侧 payload。
注意：**存量日志里 `TaskFinalized` 从来没有过 `outputs`，只有 `TaskFinished` 有。**

### A2. `AgentView.llm_account` / `llm_model` 不进快照 —— Task 8 的跨重启存活静默失效

`serialize_view` 的 agents 段只写 4 个字段（`id` / `spawn_depth` /
`parent_agent_id` / `template_id`，`reducers.py:192-200`），
`deserialize_view` 同样只读这 4 个（`:256-264`）。

**后果**：只要该 session 存在快照，`rebuild_view` 走「快照 + delta」路径；
delta 里若没有新的 `AgentInstantiated`/`AgentLlmChanged`，两个字段就是 `""`
→ `AgentRegistry.load` 据此建 `ModelChoice("", "")`（`agent_registry.py:190`）
→ **静默回落「跟随账号默认」**。

快照每 50 条非瞬态事件（或 `SessionFinished`）就落一次，
**中等长度以上的会话必然命中**。无 warning、无测试。
即 `agent_registry.py:188-189` 注释里那条「D1 修复：跨重启存活」在有快照时不成立。

### A3. 取消可能被完全丢弃（finalize 期间）

`driver.py` 主循环的 token 检查在**循环顶部**，而 `next_step_name = outcome.next_step`
在底部。finalize 返回 `next_step=None` → 循环直接退出，**不再检查**。
而 `_run_loop` 从头到尾**没有一处读 `cancel_token.is_cancelled`**。

**后果**：取消若发生在 finalize 期间，run 以 COMPLETED 正常结束、
task 落 FINISHED/FAILED、token 随 run 注销，`RunCanceled` 与 `TaskCanceled` 一条都不发。

**相关但独立的两条**：

- `ObserveStep` 内部**零检查点**——它的 ReAct 多轮循环（`observe.py:98`）
  只在 LLM 退避重试点间接受 cancel 影响，正常无故障时一次都不撞。
  最坏要等跑满 `max_rounds` 轮完整 LLM 调用才在 observe→finalize 边界生效。
  `PrepareStep` / `CompactStep` / `SuspendStep` 同样零检查点。
- `_run_loop` 的 `except asyncio.CancelledError` **分不清取消来源**——
  它只看 `task.status`，不看 token。外部 asyncio 取消（进程 shutdown）
  与 token 取消产生的是同一个 `CancelledError`。

### A4. `(run_id, sequence)` 不唯一

`launch_background_observe` 用 `dataclasses.replace(state)` 做快照
（`background_observe.py:349`）：**`run_id` 相同，但 `sequence_counter` 是一份
独立的 int 副本**（普通 `int` 字段，不可变）。此后两边各自 `+= 1`——
后台 recap 那 6 类事件与主 run **重号**。

**后果**：host 按 `(run_id, sequence)` 排序 / 去重 / 幂等重放会撞车。
连带：`RunFinished.total_events` 取的是主 run 的 counter，**不含后台 recap 的事件**。

### A5. `tenant_id` 三处漏填，恒为 `"default"`

- `HitlService._emit`（`hitl/service.py:187`）→ `HitlOpened` / `HitlResolved`
- `runtime._announce_queue_state_as_tm_proxy`（`runtime.py:2204`）→ 恢复期的两条队列信号
- **root task 的 `TaskCreated`**：`_make_root_task_manager`
  （`session_manager.py:359-369`）先建 TM 就立刻 `push_task`，**从不调 `set_session`**；
  而 `TaskManager._emit` 取 `self._session.tenant_id if self._session else "default"`
  （`task_manager.py:1093`）。reducer 照抄 envelope（`reducers.py:523`）
  → 非 default 租户的 root task 投影租户错。

同一 session 的其它事件都带真 tenant，这几条不带。

### A6. `AgentLlmChanged` 无条件覆盖，空 payload 会抹掉已有选择

`reducers.py:498-499` 是无条件赋值，而兄弟分支 `AgentInstantiated`
（`:485/489/492`）是「空值不覆盖」。两处口径**刻意相反**且各有注释：
前者要支持「用户主动切回账号默认」（空是合法选择），
后者要保护「存量事件不带这两个字段」。

判断本身站得住，但**这个不对称目前零测试、零 golden 覆盖**——
任何一次误改都不会被发现。

### A7. `_handle_task_failure` 缺终态守卫

`apply_run_outcome` 有终态守卫（`task_manager.py:514-518`），
`_handle_task_failure` / `_suspend_task_interrupted` **一个都没有**。

**后果**：熔断 trip 已把某 task 判 FAILED（`task_manager.py:1017` 发 `TaskFailed`）后，
若该 task 的**装配**随后失败，`_suspend_task_interrupted` 会无条件写
`task.status = "INTERRUPTED"`（`:800`）并发 `TaskInterrupted`——
**把写定的终态盖回非终态**。

### A8. 非终态任务的中途产出无法从事件流恢复

`TaskView.outputs` 的唯一非空写入点是 `TaskFinished`（`reducers.py:581`）；
`TASK_CREATED` 不设它（`t.get("outputs")` 在 payload 不带这个键时是 `None`），
`TaskRequeued`/`TaskHumanResolved` 只会清它（`:536`/`:552`）。

**后果**：任何还没跑到 `TaskFinished` 的 task
（SUSPENDED / AWAITING_HUMAN / INTERRUPTED……）在崩溃恢复后，
它在这次执行里已经攒下的中途产出**必然丢失**——事件流里根本没有携带
这份数据的事件。

**这不是本次（Task 1）引入的**：改动前 reducer 挂在 `TaskFinalized` 上读
`outputs`，但那条事件的真实发射侧只带 `{task_id, outcome}`
（`finalize.py:766`），同样恢复不出中途产出，只是连*终态*的产出也读不到而已。
Task 1 只是把读取点从「一个从不携带这份数据的事件」换成
「唯一真正携带它的事件」，缺口本身的范围没有缩小或扩大。

**发现路径**：`tests/unit/test_hitl_recovery_v2.py` 里
`_resolved_user_turn_on_a_parent_with_a_live_child` 这份 fixture 曾用一条
伪造的 `TaskFinalized{outputs: {...}}`（真实发射侧不带这个键）在任意时刻
给一个 SUSPENDED 的父任务硬注入 outputs，制造出「中途产出能扛过恢复」的假象；
断言 `parent.outputs == {...}` 因此一直是绿的。Task 1 修复四处虚假绿灯时，
一度试图用一条真实的 `TaskFinished` 事件（配合后续 `TaskSuspended`）去复现
同一断言，能让测试通过，但代价是编排一段事件流里到不了的状态（父任务先
FINISHED、又被 SUSPENDED）——和被修的那四处伪造 payload 是同一类错误，
已撤销该改法，测试改为断言 `parent.outputs is None`（已实测确认，非猜测）。

**若要修**：需要一条承载中途产出的事件（或让 `TaskSuspended` /
`TaskAwaitingHuman` 携带当前 outputs 快照）。**属独立立项，不在批次一范围。**

### A9. `error_code` 不进投影，跨重启后 host 失去分流依据

`Task.error_code`（`state/models.py:222`）在 `TaskView` 上**没有对应字段**
（`control/types.py` 里查无此项），`task_from_projection` 也不还原它
（`control/converters.py` 里查无此项）。于是进程重启后，被还原的 INTERRUPTED
任务 `error_code` 为 `None`——`announce_queue_state` 只能退到兜底码
`"interrupted"`。

**后果**：三份契约（升级须知 / `docs/events-v2.md` §2.1.2 / `spec/golden/07`）
说 host 按码分流（如 `CONTEXT_OVERFLOW` → 提示换更大窗口的模型），但**重启
之后这个码就没了**，分流静默退化成「通用中断」。

**不是本批次引入的**：批次一 task 5 只是把兜底从「吐散文」改成「吐码」——
修复前，跨重启还原路径上 `error_code` 为空时会退到 `interrupted[0].error`
这项自由文本（**Task 1 / A1** 让 `TaskView.error` 可还原之后——Task 2 做的是 AgentView 快照，这项确实会有值，
真的吐出过散文）；修复后统一退到 `"interrupted"` 这个码。批次一只是使该
退化变得**明确**（恒是某个码，要么真实要么兜底码）而非混入自由文本，
`error_code` 本身跨重启丢失这件事，批次一没有引入也没有修。

**若要修**：给 `TaskView` 加 `error_code` 字段（需走批次一 Task 2 建立的
「加 View 字段」维护清单：types → 发射侧 payload → reducer →
serialize/deserialize → converters → golden）。**属独立立项。**

---

## B. 不变量与守卫

### B1. 「task 状态事件只从 TaskManager 发」已有一处实质破口

`TaskHumanResolved` 有两个发射点，其中 `runtime._inject_user_reply`
**在 TM 之外、且同时写 `task.status`**（`runtime.py:1938` 写 PENDING，`:1942` 发事件）。
它确实是一条 task 状态事件——映射到 PENDING、有专属 reducer 分支、S 档。

**两道守卫都没抓住，原因各不相同**：

- 守卫 A：形态是可见的（`EventType.` 属性访问），纯粹因为 `TASK_STATUS_EVENTS`
  那 7 个名字里**没有它**。**实证：加进清单立刻红在 `runtime.py:1944`。**
- 守卫 B：`_inject_user_reply` 的精确豁免是 **Task 4 期为「只改内存态」批的**，
  本批次在同一个函数里加了事件发射，**豁免顺带覆盖了一件它当初没被审过的事**。

绕开唯一发射者的理由，注释自陈是**为了迁就测试替身**
（「task_manager 在部分既有单测里是不带 `_emit` 的轻量 fake」）。
功能上的原始问题是真的（恢复路径上 `restore()` 先跑，`was_blocked` 判据必然落空），
但正解是**给 TM 一个不看当前状态的入口**。

**修守卫时注意**：`TASK_STATUS_EVENTS` 应补 `TASK_HUMAN_RESOLVED` 与 `TASK_RESUMED`
（后者唯一发射点在 TM，安全）；**但不要顺手加 `TASK_STARTED`**——
它会误报 `session_manager.py:114` 的查表读，说明守卫 A「属性访问即违规」
的判据对「读表」没有免疫。

### B2. 守卫 A 认不出字符串字面量形态

它只匹配 `EventType.<NAME>` 属性访问，而 `task_disposition.py:88-133`
自己就用字符串字面量表达事件类型（TM 在 `:538` 才 `EventType(...)` 转回来）。
模仿这个写法的新模块能绕过守卫 A。

### B3. 没有任何测试保证 `serialize_view` 覆盖 View 的全部字段

`serialize_view` / `deserialize_view` 都是**手写字典字面量**，
没有 `dataclasses.fields()` 遍历。golden 的 snapshot+delta 往返是唯一的间接闸门，
但它**只比对 agents 的 key 集合、不比对字段值**（`test_golden_conformance.py:145`）。

**这是 A2 能存在的结构性原因**——只修 A2 不修这条，下次加字段还会再漏一遍。

### B4. `retriable` 硬契约仍只有注释在守

`LLMOutageError.retriable` 实际是 `True`（`protocols/llm.py:93`）。
「outage 从不原地重试」靠 outage 分支硬编码 `retriable=False`
（`runtime.py:2525-2528`），崩溃分支才用 `getattr(exc, "retriable", True)`
（`errors.py:161`）。两支的分离**只靠 `except` 顺序**这一个机械保障：
`except LLMOutageError` 排在 `except Exception` 之前。

无 `__post_init__` 校验，无 `outage_run_outcome` 工厂。

**第四条缝（复审新发现）**：**装配路径**走 `_handle_task_failure` 的
`getattr(exc, "retriable", True)`（`task_manager.py:758`），三段注释一段也管不到它。
今天 `assemble` 不发 LLM 请求所以不可达，但**结构上没被封死**。

### B5. 「唯一真源」实际有七份判据

`disposition_for` 是名义真源。另有：

| # | 位置 | 状态 |
|---|---|---|
| 2 | `finalize.py:710` 的 `retry_exhausted` | 与真源**仍逐字等价**（边界 `>=`、同一 `retry_count` 实例、`error_message` 都取降级前——三处逐条核过） |
| 3 | `task_manager.py:731` 的 `_handle_task_failure` | **已三处不同构**：缺终态守卫（见 A7）、`exc=None` 的缺省方向与 `RunOutcome.retriable` **相反**、吞掉调用方传的 `reason`（见 D6） |
| 4 | `runtime.py:2603` 的 `will_retry` | **已分叉**：无终态守卫。熔断竞态下发 `will_retry=True` 而 TM 一步不动，host 据此以为「先别关流」会永远等不到。**无测试覆盖** |
| 5-7 | `runtime._inject_user_reply`（TM 之外）、`resume_task`、`restore` / `_try_resume_parent` | 各自独立的状态判断 |

准确说法：**处置表只覆盖「run 结束后」这一类**，全仓 task 状态写入点是
6 个函数 + TM 之外 1 个。「唯一真源 / 唯一改写者」这两句注释现在比重构刚结束时更失真。

---

## C. 可观测性与契约一致性

### C1. 判别值没有类型 —— `reason` 零集中定义

全是内联字面量，且重复：

| 字面量 | 出现处 |
|---|---|
| `"llm_outage"` | **同一个函数里写了 4 遍**（`runtime.py:2526/2527/2542/2548`） |
| `"run_crash"` | 3 处（`errors.py:158`、`task_manager.py:814`、`runtime.py:2587`） |
| `"failure_threshold"` | 4 处 |
| `"pause_abandon"` / `"user_cancel"` | 各 2 处 |
| `"assembly_failure"` / `"observer_review_reopen"` / `"process_restart"` / `"human_replied"` / `"resumed"` | 各 1 处 |

`RunOutcome.reason` 的类型是裸 `str`（`task_disposition.py:41`）。

`error_code` 有**半套机制**：`CtxWeftError.code` 类属性收敛了 `CONTEXT_OVERFLOW` /
`MAX_TURNS_EXCEEDED` 等，`crash_error_code` 也做了收敛——但处置表那三个码
（`TASK_FAILED_BY_OBSERVER` / `RETRY_EXHAUSTED` / `BY_THRESHOLD`）
**直接写字面量、不走那套**，其中 `TASK_FAILED_BY_OBSERVER` 在 `errors.py:191`
与 `task_disposition.py:134` **各写一份**。

**建议**：`reason` 与 `error_code` 各做一个 `StrEnum`，换掉 `RunOutcome` 的字段类型。
这件事的收益比拆 event type 大——它同时修掉「同一字面量写 4 遍」和 C2。

### C2. `TaskQueueInterrupted.reason` 的兜底会吐自由文本，违反自身契约

**这条最初记的判断是错的，已撤销**：本条曾写「`reason` 键里装的是 `error_code`，
是语义混淆最集中的一处」，建议拆成 `error_code`/`reason` 两个键。批次一 task 5
按此建议实现后，撞上三条既有测试（`test_outage_interrupt_reason.py`、
`test_run_crash_suspend.py` 两处），它们的注释明写「reason 必须是**码**，
不是自由文本，host 按码分流」，并点名**三份契约**钉住这个口径：升级须知 /
`docs/events-v2.md` §2.1.2 / `spec/golden/07`。也就是说 `TaskQueueInterrupted.reason`
**按设计就是码，不是本条最初以为的「语义混淆」**——那是读代码没查契约得出的
误判。拆键取消，不做。

真实缺陷更窄：取值链

```python
"reason": (interrupted[0].error_code or interrupted[0].error or "interrupted")
```

中间那项 `interrupted[0].error` 是**自由文本**（散文），于是当 `error_code`
为空时，本该恒为码的字段会吐出散文，与「reason 是码」的自身契约相悖——
**这条分支不是死路径**（曾有此论断，已被驳回）：进程内实时崩溃那条路上
`error_code` 确实恒非空（`crash_error_code` 兜底到 `type(exc).__name__`），
但**跨重启还原**的任务 `error_code` 必为 `None`——`TaskView` 没有这个字段、
`task_from_projection` 不还原它（见 A9）。故恢复路径上兜底链真的会走到
中间那项、真的会吐散文。

**已在批次一修复**：删掉中间的自由文本项，`error_code` 为空时兜底改为码
`"interrupted"`（本就是原先三态兜底串里的那个词，只是现在被提升成唯一的
非 error_code 兜底值）；自由文本仍走 `TaskInterrupted.error_message`，不进
`TaskQueueInterrupted.reason`。

### C3. 17 个多发射点事件，4 处 payload 不一致

| 事件 | 点数 | 不一致 |
|---|---|---|
| `MemoryCompacted` | 5 | **键集完全不同**：`compact.py` 发 `superseded_count`/`source`/`freed_tokens`，`observe.py:477` 发 `events_before`/`events_after`/`summary_event_id`/`summary_length` |
| `TaskCanceled` | 5 | 四处带 `reason`，处置表那处**可能是空 `{}`** |
| `TaskRequeued` | 4 | 语义两义（retry / reopen），payload **三种形状** |
| `LLMResponseFinished` | 2 | `tool_calls` 形状不同（act 有 `id`/`arguments`，observe 只有 `name`）；键名 `turn` vs `round` |

低风险的其余：`ActTurnCompleted`(4)、`LLMRequestStarted`/`LLMPromptSent`/
`LLMTokenStreamed`（各 2，`turn` vs `round`）、`TaskInterrupted`/`TaskFailed`/
`RunInterrupted`/`SessionFinished`/`MemoryIngested`/`RecognizeIntentSkipped`（各 2）、
跨模块所有权分裂的 `TaskHumanResolved` / `TaskQueueBlocked` / `TaskQueueInterrupted`。

### C4. 25 类事件无 `run_id`、无 `sequence`

六个发射器：`TaskManager._emit`、`SessionManager` 的两个 emit、`HitlService._emit`、
`AgentRegistry`（四处 inline 构造）、`runtime` 的两处。

**这条影响随每次收口而变大**——task 状态事件已由 9 条增至 10 条
（`TaskHumanResolved`），`AgentLlmChanged` 是本批次新增的第 25 类。
按 `run_id` 归组或按 `sequence` 排序的 host 必须改按到达顺序与 `timestamp`。

### C5. 两个孤儿 run

`recognize_intent`（`recognize_intent.py:43-51`）与 `compact_session`
（`runtime.py:1813-1826`）各自造新 `run_id`，
却**没有配套的 `RunStarted` / `RunFinished`**。
host 会看到凭空出现又凭空消失的 run。

### C6. `RunInterrupted` 两支的 payload 不对称

outage 支**缺 `error_code` 键**（`runtime.py:2547-2548` 只发
`{reason, error_message}`），crash 支有（`:2585-2590`）。
而 outage 的 `error_code` **在 `RunOutcome` 里是有的**（`"llm_outage"`，`:2527`），
只在事件 payload 里漏了。另：outage 支无条件发，crash 支受 `was_interrupted` 守卫。

**若要拆成独立事件类型**：core 侧影响面 ≈ **零**——`reducers.py` 里
`RUN_FINISHED` 是 `pass`，`RUN_INTERRUPTED` / `RUN_CANCELED` 一个分支都没有，
`TASK_STATUS_BY_EVENT` 里也没有任何 run 域事件。
改动点只有两个发射点 + 枚举 + 白名单。成本全在 host 侧与存量事件回放。

### C7. 前台 observe 有 `Completed` 无 `Started`

而后台那条链反倒是齐的（`TaskRecapStarted` / `TaskRecapDone` 成对）。
若需要「observe 开始了」这个可观测点，补一条 `ObserveStarted` 比加 task 状态便宜得多。

（注：`StepStarted{step_name:"observe"}` 已经能标记进入时刻，
且已折进 `RunStateView.current_step`——所以这条是「对称性」而非「能力缺失」。）

---

## D. 死代码与死值域

### D1. 29 个死枚举成员（零发射点）

`SessionStatusChanged` · `SessionPausedHitl` · `RunPaused` · `RunResumed` ·
`AgentStatusChanged` · `AgentWaiting` · `AgentFinalized` · `ContextTokensMeasured` ·
`ContextOverflowed` · `CapabilityFailed` · `CapabilityCanceled` · `CompactTriggered` ·
`CompactDispatched` · `MemoryCompactFailedFallback` · `BlackboardSubscribed` ·
`HitlRequired` · `HitlApproved` · `HitlAnswered` · `HitlRejected` · `HitlModified` ·
`HitlTimeout` · `HitlCancelled` · `TokenBudgetWarning` · `TokenBudgetExceeded` ·
`MaxConcurrentAgentsExceeded` · `MCPServerDisconnected` · `MCPServerReconnected` ·
`EventsDropped` · `SnapshotCreated`

其中前两个 + 六条 legacy HITL 是**有意的 L 档**（只读存量日志），不该删。
其余多数是「定义了但从没实现」。

### D2. `TaskStatus.TO_BE_OBSERVED` 是死值

从 LoomeJ baseline 导入后（`1ef4419`）**从未被写过一次**，
此后所有涉及它的 commit 都只是文档或注释。
前身文档里也只有值域枚举、**无任何语义说明**。
零写入、零读取、不在 `TASK_STATUS_BY_EVENT`。

**它想表达的东西今天已有更合适的承载**：`StepStarted{step_name}` 标记进入时刻，
已折进 `RunStateView.current_step`。层次差是关键——
`current_step` 是 run 级瞬时相位，`TaskStatus` 是 task 级持久状态；
而 observe 必然在 run 内走完，
**不存在「任务停在 TO_BE_OBSERVED 等下一次调度」的场景**。

除非将来把 observe 改成可被 TaskManager 独立调度的异步 worker——
今天没有任何代码朝这走（`background_observe` 是 fire-and-forget 协程，
做 memory 折叠，**不产 verdict**）。

### D3. `RunStateView.extra` / `snapshot_at` 死字段

`types.py:107/109`，全仓零读写。
（`snapshot_at` 的同名字段存在于 `RunSnapshot`，是另一个类。）

### D4. 三处死代码

- `driver.py:257` 的 `getattr(tok, "mode", "cancel")`——`CancelToken`
  **没有 `mode` 属性**，该条件恒真，是历史残留（曾经 CancelToken 上挂过 pause 模式）。
- `runtime.py:1750` 的 `CancelToken()`——创建后**从未传给任何人、从未 cancel**，
  纯占位（idle-guard 已改用 `_busy_sessions`）。
- `Deadline` 类（`tokens.py:52-68`）——全仓零使用，
  只在 `control/__init__.py:5` 被 re-export。

### D5. 映射表两条对 `_apply` 是死路

`TASK_REQUEUED`（`reducers.py:63`）与 `TASK_HUMAN_RESOLVED`（`:65`）
在 `TASK_STATUS_BY_EVENT` 里各有条目，但两者都有**专属分支且排在前面**
（`:530` / `:545` < `:555`）。表项只为仓外查表的消费方而存在（注释 `:64` 承认了）。

**副作用**：这两个事件走专属分支时**不参与 `failure_counter` 折叠**
（那段在 `:555` 分支体内）。今天无害，但将来若给映射表加一个映射到
FINISHED/FAILED 的事件又同时写专属分支，counter 会静默漏折。

### D6. 两处冗余 / 吞值

- `_suspend_task_interrupted` **签名里根本没有 `reason` 形参**
  （`task_manager.py:784`），发射处硬编码 `"run_crash"`（`:814`）。
  调用方传的 `assembly_failure` 只在**重试**支被用上，落到挂起支时被丢弃——
  同一次装配失败的两个出口 reason 不同源。
- outage 支的 `task.error` / `error_code` 在 `runtime.py:2542-2543` 与
  `apply_run_outcome`（`task_manager.py:531-535`）**各写一份**，值相同，纯冗余。
  崩溃支还有半份（`runtime.py:2575` 只写 error）。

---

## E. 文档与 fixture 漂移

| # | 位置 | 问题 |
|---|---|---|
| E1 | `docs/spec/03-reducer-rules.md` | **漂移 4 处**：`TaskResumed` 的映射值写 ACTIVE（代码是 PENDING）、缺 `TaskHumanResolved` 的表项与专属分支、缺 `AgentLlmChanged` 分支。**spec 无任何自动化闸门** |
| E2 | `docs/spec/golden/` | 失真：`03` 的 `TaskResumed` 与**八个 golden 里的全部 `TaskStarted`** 错带 `runId` 与非零 `sequence`。规律是「task 状态收口那轮改到的事件被订正了，没改到的留在旧形态」。golden runner 不断言发射器行为，所以测试不会红 |
| E3 | `docs/spec/golden/` | `TaskHumanResolved` 与 `AgentLlmChanged` **零 golden 覆盖**。后者尤其该补——它的 reducer 用无条件覆盖，与兄弟分支口径相反（见 A6），而这个不对称没有任何 fixture 钉住 |
| E4 | `docs/upgrade/2026-09-02-agent-llm-ownership.md:28` | **不实**：说「派生新 agent 时也发 `AgentLlmChanged`」——不发，初始选择搭载在 `AgentInstantiated` 的 payload 里。host 若按这句订阅，会漏掉每个新 agent 的初值 |
| E5 | `docs/upgrade/2026-09-02-session-status-ownership.md:49-53` | 把 `TO_BE_OBSERVED` 列进「本次新增的值域」——那次新增的是 `AWAITING_HUMAN` 与 `INTERRUPTED`，它是搭便车被列进去的 |
| E6 | `task_manager.py:928` / `runtime.py:1210` | 两处注释声称会发 `HitlCancelled`，实际 `HitlService.cancel` 发的是 `HitlResolved{outcome: cancelled}`。`HitlCancelled` 全仓零发射 |
| E7 | `act_guidance.py:61` / `:111` | docstring 枚举非终态时漏了 `AWAITING_HUMAN`/`INTERRUPTED`、多了 `TO_BE_OBSERVED`；实际代码用的是终态补集 |
| E8 | `task_manager.py:1114`、`agent_registry.py:146` | 前者（`announce_queue_state`）说调用点在「`_run_task` 的挂起出口」（已搬到 `_settle`），且漏了 compat 路径 `runtime.py:1016` 的直调与 `_try_resume_parent`；后者（`load`）说「`AgentView` 只有四个字段」，与下方第 190 行读 `llm_*` 的代码自相矛盾 |
| E9 | `docs/events-v2.md` | **不能当作当前事实来源**。它自述「其余各节仍是已定案、**未实施**的改名/合并」：`TaskOutcomeRecorded` / `TaskRecapCompleted` / `PromptAssembled` / `StepSkipped` / `IntentRecognized`、envelope 上的 `origin` 字段，代码里都不存在。TRANSIENT 集合文档说 3 个、代码是 4 个，根因也是这个未实施的合并 |

---

## 建议的处理顺序

1. **A1 / A2** —— 两条数据损失，且 A2 让一个刚做完的修复失效。
   修 A2 时**连带修 B3**（否则下次加字段还会漏）。
2. **A3** —— 取消的两个洞（finalize 期间丢失、observe 零检查点）是行为缺陷。
3. **C1 / C2** —— 判别值枚举化。收益比拆 event type 大，
   且做完之后 C6「要不要拆 event type」的答案可能变成「不用了」。
4. **B1** —— 补守卫清单（一加就红），顺便逼出决定：
   把 `_inject_user_reply` 的发射搬回 TM，还是正式承认这个例外并更新豁免注释。
5. **D2 + C7** —— 删 `TO_BE_OBSERVED`，补 `ObserveStarted`（可顺手带上）。
6. 其余按需。**E9 需要单独决定**：`events-v2.md` 要么落实，要么明确标注为设计稿。
