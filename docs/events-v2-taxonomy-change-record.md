# 事件体系 V2 · 变更记录

> 状态：**已定案，未实施**。本文只记录结论与依据，不是 spec，也不是实施计划。
> 日期：2026-08-31
> 真相源（现状）：`src/ctx_weft/protocols/events.py`
> 受影响的权威 spec（改动落地后须同步）：`docs/spec/01-events.md`

---

## 1. 为什么要动

### 1.1 三方互不一致

| | 数量 |
|---|---|
| 代码 `EventType` 定义 | 83 |
| 其中**从未发射** | 21 |
| `docs/spec/01-events.md` 的「V1 冻结清单」 | 78 |
| 清单缺的（代码在发） | 13 |
| 清单多的（代码没有） | 8 |

代码、`spec/01`、`ctx-weft_设计文档.md` 三者**两两都不一致**。

「清单缺的 13 个」是冻结之后新增的功能（意图识别、后台观察、任务回顾）；
「清单多的 8 个」里 `MetadataFiller*` 五条在代码里连类型都没定义，`ReasonCompleted`
疑似被 `PrepareCompleted` 取代后未同步。

### 1.2 两份 spec 互相矛盾

`HitlTimeout` 在 `spec/01` 的冻结清单里，但 `spec/05-authz-and-hitl.md` 明写
「**已删除** `timeout` 状态与 `HitlTimeout` 事件」，超时改为热→冷驱逐。

### 1.3 扁平枚举丢掉了一个关键区分

83 个里只有 32 个被状态消费者折叠（承载状态），其余 51 个是纯观测/展示。
两者的契约强度完全不同——前者改了会破坏重放或 host 投影，后者随便加。
但代码里它们混在一个扁平枚举中，**看不出哪个动不得**。

`ctx-weft_设计文档.md` 的事件表其实早就有一列在标这个（`S` / `O` / `M` / `T`），
只是这个信息从没进入代码。

### 1.4 「哪个组件发的」被编码进了类型名

`observe.py` 的 `ReactEventTypes` docstring 写得很直白：

> observe 用 `LLM_*` 组；background observe 用 `BACKGROUND_OBSERVE_*` 组——
> **同形不同类型，供 host 区分前端是否渲染**。core 只发类型，不感知前端可见性。

代价是每加一个子循环就多 4 个近乎相同的类型。

---

## 2. 定下的原则

### P1 · 三档分层，判据是「是否有状态消费者」

| 档 | 判据 | 可改动程度 |
|---|---|---|
| **S** | **任一状态消费者**折叠 → 承载状态 | 语义冻结；改名须走别名表；payload 只可**加**字段 |
| **O** | 已发射、无状态消费者 → 纯观测/展示 | 自由重命名 / 合并 / 删除（但须与 host SSE 同步） |
| **X** | 从未发射 | 完全自由 |

> ⚠️ **状态消费者有两个，不是一个**（2026-08-31 核查 host 后更正）：
>
> 1. ctx-weft 的 `core/control/reducers.py`
> 2. **host 的 `netlivecowork/persistence/postgres/projection_updater.py`** —— 它有自己独立的
>    一套 `elif t == ...` 分支，维护 Postgres 投影，**不共享** ctx-weft 的 reducer
>
> 两者消费的集合**不一样**。最典型的是 `RunCanceled`：ctx-weft reducer 不碰它，host 投影却据它
> 把 session 落成 CANCELED。`projection_updater.py:86-89` 的注释明写「core reducer / SSE 层均不
> 据此改状态，**仅本投影需守卫**」——原作者清楚这个不对称。
>
> 初版本文只按 ctx-weft 的 reducer 分档，导致 `RunCanceled` 一度被判为「可删」——那会删掉一个
> 会改 host 数据库状态的事件。

依据：`ReplayEngine.replay()`（`core/control/replay.py:47`）内部只有一句
`reduce_events(events, ...)`——**在 ctx-weft 内部**replay 就等于 reducer。
会话历史真正的家是 **memory**，不是事件流。但「ctx-weft 内没有第二个消费者」不等于
「全系统没有」，host 侧的投影就是。

`content.py:192-195` 佐证这是刻意设计：纯观测事件的内容经 `redact_content_for_event`
变成带截断的预览串、**不可回读**；只有「参与状态重建」的事件才走无损 jsonable 形态。

### P2 · 准入零容忍：定义即必须发射

一个从未发射的枚举值对读者是谎言。真要实现时加回来只是一行。

### P3 · `origin` 是普遍的溯源字段

含义是「**哪个组件发出了它**」，所有事件都必须有，不是给 LLM 事件开的特例。
「哪个子循环」只是「哪个组件」的一个特例。

### P4 · 阶段标记 vs 结果记录

> **阶段标记**（开始/结束/失败/跳过）用泛化的 `Step*` + `origin` 表达，payload 固定且极小。
> **结果记录**保留独立类型，各有各的 payload 契约。

依据：`observe` 是注册在 driver 里的 step（`runtime.py:2040`），所以今天跑一次 observe，
事件流里**已经同时有** driver 发的 `StepStarted/StepCompleted` 和 observe 自己发的
`ObserveCompleted`。后者不是「observe 版的 StepCompleted」，它是一条恰好落在阶段括号里的
结果记录。两个维度正交：`origin` 说「谁发的」，结果记录说「结论是什么」。

`RecognizeIntent*` 看似反例，是因为它**两条路都走**：`runtime.py:2044` 注册成了 driver step，
但 `recognize_intent.py:37` 还有一条 fire-and-forget 路径绕过 driver。作为 driver step 跑时，
它会发出 `StepStarted` + `RecognizeIntentStarted` 两条「开始了」——真的重复。
自造生命周期事件的唯一理由是第二条路径没有括号可用，而 `origin` 消灭了这个理由。

### P5 · 新名字绝不复用任何曾经发射过的字符串

**这是一个会损坏重放的坑，必须写死为规则。**

反例：本想把 `PrepareCompleted` 改名成 `ContextAssembled`（复用那个要删的、更好的名字）。
但存量流里真有 `ContextAssembled` 事件、payload 是 `{token_count}`；改名后它会命中新分支去读
`assembled_token_count`，取不到得 0，把 `view.assembled_prompt_tokens` 覆盖成 0。

---

## 3. 最终清单 · 54 个

### S 档 · 32 个

| 域 | 事件 |
|---|---|
| Session | `SessionCreated` `SessionResumed` `SessionStatusChanged` `SessionFinished` **`SessionPaused`** |
| Run | `RunStarted` `RunFinished` `RunCanceled` |
| Step | `StepStarted` `StepCompleted` |
| Guard | `FailureThresholdHit` |
| Task | `TaskCreated` `TaskStarted` `TaskSuspended` `TaskResumed` `TaskFinished` `TaskFailed` `TaskCanceled` **`TaskOutcomeRecorded`** `TaskRequeued` |
| Agent | `AgentInstantiated` |
| Context | **`PromptAssembled`** |
| Act | `ActTurnCompleted` |
| HITL | `HitlRequired` `HitlApproved` `HitlAnswered` `HitlRejected` `HitlModified` `HitlCancelled` |
| Intent | **`IntentRecognized`** |
| TaskRecap | `TaskRecapStarted` **`TaskRecapCompleted`** |

**粗体 = 改名**（见 §4.5）。

### O 档 · 22 个

| 域 | 事件 |
|---|---|
| Step | `StepFailed` **`StepSkipped`**（新增） |
| Task | `BlackboardPublished` |
| Agent | `AgentSpawned` `SpawnRejected` |
| Context | `ContextTokensEstimated` |
| LLM | `LLMRequestStarted` `LLMPromptSent` `LLMTokenStreamed` `LLMReasoningStreamed` `LLMResponseFinished` `LLMRetryTriggered` |
| Capability | `CapabilityInvoked` `CapabilityProgress` `CapabilityFinished` |
| Act | `ActTurnStarted` `MaxTurnsReached` |
| Observe | `ObserveCompleted` |
| Memory | `MemoryIngested` `MemoryCompactStarted` `MemoryCompacted` `MemoryCompactFinished` |

### TRANSIENT · 3 个（⊂ O）

`LLMTokenStreamed` `LLMReasoningStreamed` `LLMRetryTriggered`

> 现状：代码里是 4 个（多一个 `BackgroundObserveTokenStreamed`，合并后消失），
> `spec/01` 写的是 2 个（漏了 `LLMRetryTriggered`）。两处都不对。

### `origin` 值域 · 16 个

```
orchestrator.session_manager    orchestrator.task_manager    orchestrator.hitl_manager
loop.driver      loop.prepare        loop.act           loop.observe
loop.background_observe          loop.recognize_intent   loop.compact
loop.finalize    loop.suspend        loop.capability_gateway            loop.llm_gateway
runtime
persistence.snapshot_writer
```

两级点号的理由：host 可**前缀匹配**（`loop.` 取全部循环内事件，`loop.background_observe`
精确排除后台观察的渲染）。与仓内 capability id 的 `provider:tool` + 前缀路由是同一惯用法。
分隔符用 `.` 而非 `:`，把 `:` 留给可路由的 capability id。

---

## 4. 改动清单（11 条，均**待接线**）

### 4.1 删除 21 个从未发射的 【X 档 · 零风险】

`RunPaused` `RunResumed` `AgentStatusChanged` `AgentWaiting` `AgentFinalized`
`ContextTokensMeasured` `ContextOverflowed` `CapabilityFailed` `CapabilityCanceled`
`CompactTriggered` `CompactDispatched` `MemoryCompactFailedFallback` `BlackboardSubscribed`
`HitlTimeout` `TokenBudgetWarning` `TokenBudgetExceeded` `MaxConcurrentAgentsExceeded`
`MCPServerDisconnected` `MCPServerReconnected` `EventsDropped` `SnapshotCreated`

删了不可能有人受影响——它们从未出现在任何事件流里。

其中 `HitlTimeout` 属**纠正**而非删除：`spec/05` 早已宣告它废止，`spec/01` 没同步。

### 4.2 合并 5 个进 LLM 族 【O 档 · origin 承担区分】

| 删除 | 并入 |
|---|---|
| `BackgroundObserveRequestStarted` | `LLMRequestStarted` + `origin=loop.background_observe` |
| `BackgroundObservePromptSent` | `LLMPromptSent` + `origin=loop.background_observe` |
| `BackgroundObserveTokenStreamed` | `LLMTokenStreamed` + `origin=loop.background_observe` |
| `BackgroundObserveResponseFinished` | `LLMResponseFinished` + `origin=loop.background_observe` |
| `RecognizeIntentLLMPrompt` | `LLMPromptSent` + `origin=loop.recognize_intent` |

### 4.3 阶段标记归一 2 个 【按 P4】

- `RecognizeIntentStarted` → `StepStarted(origin=loop.recognize_intent)`
- `RecognizeIntentSkipped` → `StepSkipped(origin=loop.recognize_intent)`

fire-and-forget 路径自己发**真的** `StepStarted`，不再需要伪造品。

### 4.4 新增 1 个 【O 档】

`StepSkipped` —— 把「步骤被跳过」变成通用概念，下一个需要它的 step 不必再自造类型。

### 4.5 S 档改名 5 个 + 别名表 【唯一触碰 S 档的改动】

| 现名 | 新名 | 理由 |
|---|---|---|
| `RecognizeIntentToolCall` | `IntentRecognized` | 名字说的是产生方式；reducer 实际拿它填 `session.goal` / `task.title` |
| `SessionPausedHitl` | `SessionPaused` | `reducers.py:498` 会据 `form` 落成 `PAUSED`（非 HITL）——**名字有一半时候是错的** |
| `TaskFinalized` | `TaskOutcomeRecorded` | 写 `outputs`/`error`/`finished_at`，**不改状态**；`TaskFinished` 才是状态转移。两个近义词做完全不同的事 |
| `PrepareCompleted` | `PromptAssembled` | 阶段标记职责已由 `StepCompleted(origin=loop.prepare)` 接走，它现在纯粹是 token 数的载体 |
| `TaskRecapDone` | `TaskRecapCompleted` | 全仓其余一律 `Completed`/`Finished`，只有它用 `Done` |

别名统一处理，`reducers.py` 立一张表：

```python
# 存量事件名 → V2 现名。旧事件流按新分支折叠，重放逐字等价。
LEGACY_EVENT_ALIASES: dict[str, str] = {
    "RecognizeIntentToolCall": EventType.INTENT_RECOGNIZED,
    "SessionPausedHitl":       EventType.SESSION_PAUSED,
    "TaskFinalized":           EventType.TASK_OUTCOME_RECORDED,
    "PrepareCompleted":        EventType.PROMPT_ASSEMBLED,
    "TaskRecapDone":           EventType.TASK_RECAP_COMPLETED,
}
```

`_apply` 首行归一：`t = LEGACY_EVENT_ALIASES.get(ev.type, ev.type)`。

> ⚠️ **最容易漏的地方**：`fold_pending_hitl` / `fold_pending_task_recap` /
> `fold_cold_hitl_decision` 三个函数**直接匹配 `ev.type`**，不走 `_apply`，
> 必须各自归一，否则存量流的 HITL 与 recap 折叠会静默失效。

### 4.6 合并 `RecognizeIntentCompleted` 进 `IntentRecognized`

两者在 `recognize_intent.py:167` 和 `172` **连着发**，`{title, description, session_goal}`
三字段完全一样，后者只多一个 `usage`。把 `usage` 并入 `IntentRecognized` 的 payload
（给 S 档事件**加**字段是安全的，旧事件只是没有这个键），删掉 `RecognizeIntentCompleted`。

### 4.7 删除 1 个信息冗余的**已发射**事件 【与 4.1 性质不同】

| 删除 | 理由 |
|---|---|
| `ContextAssembled` | payload 唯一字段 `{token_count}` 在同一时刻被另两条事件各带一份（`ContextTokensEstimated.assembled_tokens`、`PrepareCompleted.assembled_token_count`）。三份相同数据，它是唯一「O 档 + 零独有字段」的那个。host 侧零引用（已核查） |

> **它和 4.1 的 21 个性质不同**：它**发射过**。不过 host 全仓零引用，实际风险与 4.1 同级。
>
> 初版这里还有一条 `RunCanceled`，核查 host 后**撤销**——见 §6。

### 4.8 新增字段

- `Event.origin: str = ""`
- `EventFilter.origin: str | None = None`
- `LoopState.origin: str`
- SQL 事件表加 `origin` 列（可空，默认空串）

**存量事件的 `origin` 留空**，不做反推。少数类型（如 `LLMPromptSent`）本就有多个发射者，
反推会造出看起来精确、其实是猜的数据。守卫测试只约束新发射。

### 4.9 origin 的填充机制（防漂移）

60+ 个发射点靠每处手填必然有人忘，所以结构性填充：

1. `LoopState` 加 `origin` 字段，**driver 在每步开始前写入**（driver 本就知道在驱动哪个 step）。
   `make_event` 默认从 `state.origin` 取 —— **40+ 个循环内发射点零改动**。
2. `make_event(..., origin=...)` 保留显式覆盖，给 background_observe 这类脱离主 driver
   序列、异步跑的场景用。
3. 循环外的 5 个发射者（`session_manager` / `task_manager` / `hitl_manager` / `runtime` /
   `snapshot_writer`）各持一个模块常量，在自己的 `_emit` 里填。

### 4.10 删除 `ReactEventTypes` 抽象

它存在的唯一理由是「同形不同类型」，已被 `origin` 取代。

### 4.11 O 档 payload 去掉重复 envelope 的字段

`ObserveCompleted.task_id`、`MemoryCompactStarted/Finished.{task_id, agent_id}`
—— envelope 已有 `task_id` / `agent_id`。立起「envelope 管身份、payload 管内容」的界线，
否则同一份信息两处存，早晚不一致。

（S 档的 `StepStarted.payload.step_name` 与新的 `origin` 也重复，但 reducer 在读，**不动**。）

---

## 5. 守卫测试（5 条）

防止半年后又漂回去——今天这 21 个未发射、三方偏差，成因就是没有这些。

1. `EventType` 全集 ≡ **实际发射集合**（P2 的执行者）
2. `EventType` 全集 ≡ `STATE_EVENT_TYPES ∪ OBSERVABILITY_EVENT_TYPES`（加枚举必须显式选边）
3. `STATE_EVENT_TYPES` ≡ **两个状态消费者折叠集合的并集**
   （ctx-weft `reducers.py` ∪ host `projection_updater.py`）。跨仓，故须以 host 侧的
   对应测试补足——ctx-weft 单仓测不到 host 的分支，这是本条唯一测不严的地方
4. 所有发射出的事件 `origin` 非空
5. `LEGACY_EVENT_ALIASES` 的每个 key 都**不在** `EventType`（防新旧同名撞车），每个 value 都在

> ⚠️ **第 1、3 条的扫描器必须处理间接发射**，否则会有假阴性。本次整理中实测踩到两种：
> - HITL 那批把 `EventType` 当**变量**传进 `_resolve(req, status, evt, ...)`
> - `ReactEventTypes` 那批先打包成元组、再由 `run_observe_react` 统一发
>
> 只认「`emit(` 附近的 `EventType.X` 字面量」会漏掉 9 个。间接发射器需登记白名单。

---

## 6. 明确**不做**的，及依据

| 候选 | 为什么不做 |
|---|---|
| `ObserveCompleted` → `StepCompleted` | 它是**结果记录**不是阶段标记（P4）。且 `outcome` 含 `retry` / `needs_user_input` 等**非终态**取值——一个重试 3 轮的 task 会有 3 条 `ObserveCompleted(outcome=retry)`，而 task 状态事件前两轮什么都不说。`used_llm` 亦全仓独一份 |
| `MemoryCompactStarted/Finished` → `Step*` | 同上，携压缩指标（`freed_tokens`/`levels`/`est_before,after`）。且 compact **有时内联跑**，不总是 driver step。代码注释说明 `Started` 必须 live 发，否则前端错过整个「压缩中」窗口 |
| `MaxTurnsReached` 合并 | 曾疑与 `act_exit_reason` 重复，但 `act.py:158` 显示后者只进 `state_patch`（内存态）、**从不进事件流**。它是唯一记录「act 因轮次上限退出」的事件 |
| `MemoryCompactFinished` 删除 | 多数字段是各条 `MemoryCompacted` 的加总，但 `est_after`（压缩后估算）独有 |
| Task 状态族改名 | `TaskCreated/Started/Suspended/Resumed/Finished/Failed/Canceled/Requeued` 是 `TASK_STATUS_BY_EVENT` 的一致家族，动任一个都破坏一致性 |
| HITL 六件套改名 | `HitlModified`（= 带改参的放行）单看不够直白，但 `spec/05` 有明确定义，六个一起看语义自洽 |
| 删 `RunCanceled` | **初版判它可删，核查 host 后撤销**。它在 host 的 `projection_updater.py:85-94` 里把 session 落成 `CANCELED`——是状态消费者，只是 ctx-weft 的 reducer 不碰它。该文件注释明写「core reducer / SSE 层均不据此改状态，仅本投影需守卫」 |
| 拆成两个独立枚举 | 事件总线 / EventStore / SSE 都只认字符串，不该分裂。用单枚举 + 分组常量 + 测试约束 |

---

## 7. 接线时的风险与顺序建议

**破坏性排序（从安全到危险）**

1. 4.1 删 21 个未发射 —— 零风险
2. 4.8/4.9 加 `origin` —— 纯新增，旧行为不变
3. 4.2/4.3/4.4 合并与归一 —— host 若按类型名过滤 background observe 会断
4. 4.7 删 `ContextAssembled` —— 已核查 host 零引用，实际同 1
5. 4.5 S 档改名 —— **唯一触碰重放路径的**，三个 fold 函数的归一是最容易漏的点

**别名表必须在 host 侧也有一份。** host 的 `projection_updater.py` 有自己独立的
`elif t == ...` 分支、**不共享** ctx-weft 的 reducer；只在 ctx-weft 加别名表，改名后
host 读存量事件会全部落空。三个待改名类型（`RecognizeIntentToolCall` /
`SessionPausedHitl` / `TaskFinalized`）都被 host 投影消费，无一例外。

**S 档的重放等价性论证**：改名后旧事件仍被别名表认出，折叠逻辑一字不改。
`origin` 对存量事件读出空串，而存量事件**全部由 driver 发出**，所以让 reducer
变成 origin-aware（只在 `origin` 为空或 `loop.driver` 时更新 `current_step`）
对存量重放**逐字等价**。

**必须同步更新的文档**

- `docs/spec/01-events.md` —— 冻结清单、TRANSIENT 集合、新增 `origin` 字段说明
- `docs/spec/05-authz-and-hitl.md` —— `SessionPausedHitl` 改名的连带引用
- `docs/ctx-weft_设计文档.md` —— 事件表（它的 `S/O/M/T` 列正是本次分层的来源，值得保留并对齐）

---

## 8. host 侧影响面（2026-08-31 已核查）

host 仓：`C:/Users/Xing/Documents/codes/IpMasterCoworkPy/src`（162 个 py 文件）。

**三类消费者，影响严重度不同：**

| 消费者 | 文件 | 影响 |
|---|---|---|
| **状态投影** | `netlivecowork/persistence/postgres/projection_updater.py` | 改错 = **数据错**（Postgres 投影） |
| **SSE 展示** | `netlivecowork/api/models/session.py` | 改错 = 前端断，数据不损 |
| **历史回填** | `netlivecowork/persistence/postgres/migrations.py` | 读历史事件，**必须保留旧名字**，不随改名走 |

**逐项核查结果：**

| 改动 | host 影响 |
|---|---|
| 4.1 删 21 个未发射 | ✓ 零引用 |
| 4.7 删 `ContextAssembled` | ✓ 零引用 |
| 4.2 合并 4 个 `BackgroundObserve*` | ✓ 零引用 |
| 4.2 合并 `RecognizeIntentLLMPrompt` | ⚠ SSE |
| 4.3 归一 `RecognizeIntentStarted` / `Skipped` | ⚠ SSE |
| 4.6 合并 `RecognizeIntentCompleted` | ⚠ SSE |
| 4.5 改名 `PrepareCompleted` / `TaskRecapDone` | ✓ 零引用 |
| 4.5 改名 `RecognizeIntentToolCall` | ⚠⚠ 投影 + SSE + 迁移 |
| 4.5 改名 `SessionPausedHitl` | ⚠⚠ 投影 + SSE + 迁移 |
| 4.5 改名 `TaskFinalized` | ⚠⚠ 投影 + `providers/capability/skills/runtime/usage.py`（技能用量上报） |

**迁移脚本的特殊性**：`migrations.py:157` 的 `_FORM_BACKFILL_TYPES` 和 `:531` 的
`RecognizeIntentToolCall` 回填读的是**历史事件**，它们的字符串**不能**跟着改名走——
改了反而读不到旧数据。但改名后新事件用新名，若迁移将来重跑，需同时匹配新旧两个名字。
`migrations.py:157` 还引用了 `HitlTimeout`（本次要删的 21 个之一），同理保留。

### 核查方法上的一个教训

初次扫描只 grep 了**字符串值**（`"RecognizeIntentStarted"`），报告「host 零引用」——
**错的**。host 用的是 `EventType.RECOGNIZE_INTENT_STARTED` 这种**常量名**。
两种形式都扫之后，受影响项从 2 个变成 9 个。

后续任何跨仓影响面核查，都必须同时匹配 `"EventValue"` 与 `EVENT_CONSTANT_NAME` 两种形式。

---

## 9. 未决

- **TS / Java 移植**：本次按「事件清单 = Python 实现清单」定案，移植按新设计重新实现。
  `spec/01` 里「三份实现须复现同一断言」的措辞届时需一并处理。
- **host 与 ctx-weft 的改动必须同批次上线**：涉及投影的三个改名没有灰度空间——
  ctx-weft 先改则 host 投影漏事件，host 先改则读不到新名字。
