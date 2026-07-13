# 07 · HITL 热/冷两层等待模型（Phase B + C-1 + C-2/D 已实现）

> **状态：完整热/冷模型（Phase B + C-1 + C-2/D）已实现。**
>
> **Phase B 落地内容**：HitlCancelled 事件、`pending_hitl` reducer fold、
> `request()` 按 tool_call_id 幂等去重、`ReconcileStep` + `_dangling_tool_calls` 检测、
> `request_human_input` input-kind 冷路径短路（已解决 HITL 直接复用答复、不再 park）。
>
> **Phase C-1 落地内容**：`recover_session()` 从 `pending_hitl` 投影重建 `HitlManager`；
> `TaskManager.restore()` 区分"HITL 挂起"与"等子任务挂起"（前者不重入队，后者视子任务状态决定）；
> host 投影层新增 `PAUSED_HITL` 状态（对应 HitlRequired/HitlResolved 事件转换），重启后不再降为 `INTERRUPTED`；
> `recover()` 对 `PAUSED_HITL` session 跳过自动重入队，由冷路径应答触发；
> crash-mid-batch（LLM 批量 tool_call 中途崩溃）经 `_task_has_dangling_tool_call` 统一路由到 ReconcileStep。
>
> **Phase C-2/D 落地内容**：`HitlPark` 专用 park 信号（`BaseException` 子类，穿透 gateway `except Exception`）；
> `AuthorizationDecision.defer` 字段（gateway 见 defer → 不调 provider.invoke + 抛 HitlPark）；
> `tool_call_id` 透传进 `authorize()` 签名；`_run_loop` 捕获 `HitlPark` → task `SUSPENDED`（非 `FAILED`）；
> 超时 = hot→cold 驱逐（Future 移出 `_futures`，request 仍 `pending`，单一权威锁保证驱逐 vs. 应答互斥）；
> approval-kind 冷路径：`resolve_approve` 写决定缓存，`HumanConfirmationAuthorizer.authorize`
> 按 `tool_call_id` 键命中已解决 HITL 后直接返回（短路 `wait()`，重启后无 future 时不 KeyError）。
>
> 真相源：`core/orchestrator/hitl_manager.py`、`core/auth/authorizer.py`、
> `core/loop/capability_gateway.py`、`core/loop/steps/reconcile.py`、
> `core/orchestrator/task_manager.py`、`core/runtime.py`（`_task_has_dangling_tool_call`）
> 关联：[06-memory-layers-and-compaction.md](./06-memory-layers-and-compaction.md)（上下文两 source 重建）

---

## 1. 动机

现 HITL 用 `asyncio.Future` 阻塞，整个 agent 调用栈驻留内存直到应答或超时；超时语义是**失败**
（`status=timeout` + 拦截）。三个问题：

1. **要么泄漏、要么误失败。** 永久等待 → 协程永驻内存（占内存、占事件循环 task）；加超时 →
   把"人类没及时回"误判成"拒绝/失败"。两者都不对。
2. **不跨进程存活。** park 的全部状态在活协程栈里，进程重启即丢，恢复只能标 `INTERRUPTED`。
3. **HITL 未持久化。** `_requests` 是内存 dict，投影层只有 session 状态 `PAUSED_HITL`，
   没有"pending 请求"的可恢复读模型。

**目标**：超时不再表示失败，而表示**「从热内存等待降级为冷持久挂起」**——释放内存、保留 pending，
应答晚到也能 resume；且**请求即持久化**，热窗口内崩溃同样可恢复。

---

## 2. 核心：热/冷两层

| 路径 | 触发 | 续跑方式 | 代价 / 收益 |
|------|------|----------|-------------|
| **热（hot）** | 请求后协程仍在等（Future 存活） | 应答 set Future，原 `invoke()` **就地续跑** | LLM message 列表 / loop 位置 / 局部变量原样保留，**零重建、零保真风险**；适合人类秒级~分钟级回复（常见情形） |
| **冷（cold）** | 超时驱逐内存对象，或进程重启 | 应答 → 写结果 + 重新入队 task → `drain()` → 从持久 memory 重建上下文续跑 | 不占内存、跨重启可恢复；保真依赖 spec/06 §5 重建 |

冷路径即 task 挂起，复用本仓**委派**已端到端实现的机制（`submit_task` 设 `SUSPENDED` →
`_run_task` 见 `SUSPENDED` 移出 running set、不驻留协程 → 重新入队 → `drain` 重跑 →
从 memory 重建 message 列表）。**HITL 只是 task 挂起的一个新「原因」。**

### 热/冷判别 = `_futures` 是否存活（关键、自洽）

- `request()`：持久登记 pending + 建 Future 入 `_futures` + 启动超时计时。
- 超时驱逐：Future 移出 `_futures`，**但 `_requests`/持久化保持 `pending`**，task 落 `SUSPENDED`。
- `answer/approve/reject`：`_futures.get(id)` 命中 → **热**（set Future）；缺失 → **冷**（写结果 + requeue）。
- **进程重启后 `_futures` 全空 → 一切皆冷 → 全走 resume**，无需特判恢复路径。

---

## 3. 状态机：超时是内部降级，不是终态

```
pending ──answer(text)────▶ answered    （HitlAnswered；写 tool_result）
        ──approve─────────▶ approved     （HitlApproved/HitlModified）
        ──reject──────────▶ rejected     （HitlRejected）
        ──cancel──────────▶ cancelled    （session 关闭 / interrupt / GC；不 requeue）

        ──(超时)──▶ 仍是 pending          （内部 hot→cold：驱逐 Future + task SUSPENDED；不发终态事件）
```

- **去掉 `timeout` 终态与"超时即失败"语义。** 超时只切换运行模式（热→冷），请求仍 `pending`，
  应答的**投递方式**随之从"唤醒活协程"变"resume 会话"。
- `pending` 有两个**运行期子模式**（hot=Future 活，cold=已驱逐/重启后），但**持久状态只有 `pending`**。
- 新增 `cancelled` 终态：session 中断 / 进程回收 / 显式放弃时收口悬挂的 pending；**不**重新入队 task。
- 仍保留 resolve 幂等：已入终态再调 answer/approve/reject 是 no-op。

---

## 4. input-kind 流程（`request_human_input` / `needs_user_input`）

**请求**：ingest 控制工具 `TOOL_INVOCATION`（已发生）→ `request(kind="input")` 持久登记 + 建 Future +
启动超时 → `session.status=PAUSED_HITL`（**热窗口内也是 PAUSED_HITL**，投影全程一致）→ 协程
`await` 该 Future（受超时约束）。

**热应答**：`/answer` 命中存活 Future → set result → 原 `_handle`/`invoke()` 续跑，
**由它写 `TOOL_RESULT`** + 发 `CapabilityFinished`（与今天一致）→ LLM 续上当前 ReAct 轮。

**超时驱逐**：超时把"park 信号"上抛（见 §7），loop 设 task `SUSPENDED` 并 unwind；`TOOL_RESULT` 未写。

**冷应答**：`/answer` 未命中 Future → 置 `answered` → 重新入队 task → reconcile 步（§6）经
`gateway.invoke` 执行该 dangling 调用（首次执行）、由它写 `TOOL_RESULT`（人类文本）→ 转 act → LLM 续上。

> reject 同理：热=续跑写「`Human declined: {message}`」；冷=resume 写同样文本后 requeue。

---

## 5. approval-kind 流程（gateway bash 门控）

**热路径绕过最难的部分。** 人类在热窗口内 `approve`：Future set → `HumanConfirmationAuthorizer.authorize`
拿到决定返回 gateway → gateway 原地用 `modified_arguments` 执行 `provider.invoke()`。**不进任何重入机制。**

**冷路径走精确重入（见 §6）。** 超时驱逐后工具**尚未执行**；晚到的 `approve` 走 resume，由
reconcile 步把这次被门控调用按 §6 机制精确执行（**首次执行**——门控住时从未跑）。决定来自持久 HITL 记录（按 `tool_call_id` 键），
不重新发 HITL。

---

## 6. 精确重入：dangling tool_call 对账（热/冷统一）

ActStep 一个 turn 发**一批** tool_calls 顺序执行（`act.py:212`）。两个既有事实让冷恢复可精确续跑：

- 含**全部** tool_calls 的 assistant 消息在执行前已落 `LLM_RESPONSE`（`act.py:162/180`）。
- 每个工具的 `TOOL_RESULT` 在**执行时**由 `gateway.invoke` **逐个**落 memory（`capability_gateway.py:236`），
  与 ActStep 的局部 bookkeeping 无关。

故 park 在批次中间时，memory 里：assistant turn（N 个 tool_calls）+ 仅**已执行**调用的 `TOOL_RESULT`；
被 park 的调用、以及该批次里其后的调用，**都没有 result**。

### reconcile 步（resume 后、任何 LLM turn 之前；必须）

```
先 resolve_and_bind(state, ctx)：把 capability 绑入 per-agent cache（reconcile 跑在 reason 之前,
  否则 gateway.invoke 命中空 cache 找不到工具——见下「两个曾经的端到端缺陷」）。
对最近一个 assistant turn 的 tool_calls 按原顺序：
  若 memory 已有该 tool_call_id 的 TOOL_RESULT → 跳过，直接复用该结果（不重跑）
  否则（dangling，仅这些）→ gateway.invoke(tc) 执行：
     authorize() 见该 tool_call_id 已有「已解决」HITL → 直接用该决定，不再发新 HITL
       approval approved → 用 modified_arguments 执行被门控工具
       input    answered → control provider 产出人类文本作 result
       rejected          → 写拦截/拒绝文本
     无对应 HITL（批次里其后从未执行的普通调用）→ 正常执行
  gateway.invoke 照常写 TOOL_RESULT
→ next_step = "reason"：reason 用补齐的 memory 重装 assembled_prompt（并再次绑定 capability），
  随后 act 调 LLM 续跑。**不可直接 "act"——ActStep 要求 ReasonStep 先装配 assembled_prompt。**
```

> **两个曾经的端到端缺陷（已修，钉在 `test_crash_recovery_reconcile.py`）**：
> ① reconcile 原 `next_step="act"`,但 `ActStep` 需 `ReasonStep` 先装配 `assembled_prompt` → 崩溃恢复任务直接报错。
> ② capability 原仅在 `ReasonStep` 绑入 cache,reconcile 跑在它之前 → 空 cache,dangling 工具无法重跑。
> 修复：reconcile 先 `resolve_and_bind` 再 invoke、且 `next_step="reason"`。隔离单测只调 `ReconcileStep.execute`、
> 从不跑真 `ActStep`,故曾长期漏掉这两个缺陷——务必保留端到端用例。

> **只补缺口、不重跑**：已落 `TOOL_RESULT` 的调用直接复用其持久结果（跳过）；只有 dangling 的才 `gateway.invoke`。
> 且对 dangling 的也**不是"重跑"而是"首次执行"**——被 park 的工具严格在 `provider.invoke` 前就被门控住、从未执行
> （见下方 exactly-once 不变式）。唯一可能真·重跑一个已执行工具的，是崩溃砸在 `provider.invoke` 执行中途
> （结果未落 memory）——HITL 路径无此情况。

**统一点**：**act 层** input（`request_human_input`）/ approval 的冷恢复是**同一条路**——只把 dangling
tool_call 经 `gateway.invoke` 执行，HITL 记录（按 `tool_call_id` 键）作**权威决定缓存**短路门控，
`gateway.invoke` **恒为唯一 TOOL_RESULT 写入点**（热=原 invoke 写、冷=reconcile 经同一 invoke 写，
形状一致、exactly-once）。

### 6.1 唯一例外：observe 的 ask_human（不走 reconcile，走注入）

`submit_task_assessment(ask_human)` 在 **observe** 阶段 park，reconcile **覆盖不到**它，因为两个前提都不成立：

- observe 的 ReAct 循环是**纯内存**（`observe.py` 的 `current_messages` 局部列表），**不往 task 层写
  `LLM_RESPONSE`**——dangling 检测（扫最近 task 层 `LLM_RESPONSE` 的 tool_calls）找不到锚点。
- `submit_task_assessment` 是 **`_SILENT_TOOLS`**（`capability_gateway.py:42`）——既不写 `TOOL_INVOCATION`
  也不写 `TOOL_RESULT`，更无 dangling。

故其冷路径**镜像 finalize 的 ask_human 分支**（`finalize.py:138`）而非 reconcile：冷应答到达
→ `HitlManager.on_cold_resolve(req)` → `runtime._resume_after_cold_hitl` 据 `capability_id` 判出
ask_human → `recover_session(..., ask_human_reply=req)` → **drain 前**把人类回复（reject 则
`Human declined: …`）作 `USER_PROMPT` 注入 task 层 + task 置 `PENDING`（`runtime._inject_ask_human_reply`）
→ 重排后从 `reason` 续跑、读到回复。**判别键 = `capability_id`**（`control:submit_task_assessment`
→ 注入；`control:request_human_input` / approval → reconcile）。注入恰好一次（绑在「冷应答」这个一次性
事件上，幂等 resolve 不重入）。

**reconcile 非可选**：带 dangling tool_call（assistant 有 tool_call、无对应 tool result）的消息序列喂给
LLM 会直接报错——reconcile 正是把序列重新配平的前置步。

### exactly-once 不变式（必须）

park **只发生在 `authorize()` 内等人类时，绝不在 `provider.invoke` 执行期间**。鉴权严格先于执行，故被门控
工具要么热执行（无 park、无 reconcile），要么 reconcile 执行（从未热执行过），二者互斥、以 `TOOL_RESULT`
存在性为界——**副作用恰好一次**。（执行中途的取消是 cancel-token 的事，与 HITL 超时无关。）

### reconcile 触发：基于 memory 的 dangling 检测（统一 HITL 与崩溃）

reconcile 是**独立 step**（注册进 `_build_step_driver`，runtime.py:922）。触发判定放在 `_resolve`
（runtime.py:660）——**所有 resume 的共同漏斗**（正常 drain、HITL 应答、`recover_session` 崩溃恢复
都经它选 `initial_step`）：

```
_resolve(task) 选出基础 initial_step（normal → "reason"）后：
  若该 task 最近一个 assistant turn 存在「有 tool_call、无对应 TOOL_RESULT」→ 覆盖为 "reconcile"
```

- **检测基于 memory，不依赖标记。** 因崩溃恢复没有机会在 park 时写任何标记（进程已死），检测必须能从
  memory 自证；而一旦基于 memory，HITL-park 也被同一条规则覆盖——**无需 `pending_tool_reentry` 之类专用标记**。
- **dangling 的唯一来源** = HITL-park 或**批次中途被中断**（崩溃 / kill）。委派 `submit_task` 是 DISPATCH
  工具，已被排除出 assistant turn 的 tool_calls（`act.py:161`）、走 TASK_DISPATCH_RESULT 回填，**不产生
  dangling**。故"有 dangling"⇔"上次在工具批次中途停下"，reconcile 一并补完。
- **语义级别按成因不同**（必须知悉）：HITL-park 是 exactly-once（park 严格在 invoke 前）；崩溃在
  `provider.invoke` 执行**中途**被 kill 则是 at-least-once（结果未落 → reconcile 会重跑该工具）。reconcile
  以 `TOOL_RESULT` 存在性为界，保证**已完成**部分不重跑，但无法把半成品副作用变原子——这是崩溃恢复的固有现实。

### 一处 plumbing

approval 的 authorizer 需 `tool_call_id` 才能按它键 HITL。该 id 已经 `ctx.extra["tool_call_id"]`
透传给 provider（`capability_gateway.py:191`），但**当前未传进 `authorize()`**——需补一处签名/透传。
input-kind 的 control provider 已能从 `ctx.extra` 取到，无需改。

---

## 7. park 信号管线（热→冷降级与显式挂起共用）

驱逐一个**热等待的协程**不是丢 Future——协程正悬在 `await` 上，必须把调用栈干净 unwind 到 `SUSPENDED`：

- 超时不抛"普通 cancel/异常"，而抛一个**专用 park 信号**，从 authorizer / control-provider 一路上抛，
  **必须穿过 `CapabilityGateway` 的 `except Exception`**（否则被当成 `[Exception: …]` 错误结果），
  最终落到 loop 设 task `SUSPENDED`（**不是 FAILED**）。
- 这条 park 管线与 approval-kind 冷路径的「authorizer 返回 `defer`」是**同一套基础设施**——
  `AuthorizationDecision` 加 `defer: bool`，gateway 见 `defer`/park 信号即「不调 `provider.invoke`
  （守住安全不变式）+ task 挂起 + unwind」。合并实现。
- `_run_task` 现已在 `BaseException` 分支丢弃未 flush 的 staged 子任务（`task_manager.py:224`），
  park 信号需落在「SUSPENDED 正常返回」而非「异常失败」分支——需与真正的 interrupt 可区分。

---

## 8. 竞态：热→冷驱逐 vs. 应答到达（必须）

超时触发、正在 unwind 的瞬间，`/answer` 可能同时进来。必须**单一权威转移**：

- 以**内存 HITL 请求状态 + 锁**为单一权威（事件为 durable trail）；`set Future`（热续跑）与
  `驱逐 + 改走 resume`（冷）**二选一、不双投**。
- hot→cold 的转换对 answer 原子：要么 answer 先到（命中 Future、走热、取消驱逐），
  要么驱逐先到（Future 已移出、answer 走冷）。
- **驱逐本身不 requeue**——只释放内存 + 留下持久 `SUSPENDED` task + `pending` HITL；
  唯有**应答**才 requeue。杜绝"驱逐后自己跑起来"。

---

## 9. 持久化与跨重启（**要求：durable**，**已定：事件重放，不加表**）

**请求即持久化**（t=0）——但**不新增表**：HITL 的写模型本就是事件（`HitlRequired` / `HitlAnswered` /
`HitlApproved` / `HitlModified` / `HitlRejected` / 新增 `HitlCancelled`），由 `EventPersister` 落 `events`
表（EventModel）。恢复时**沿用既有事件重放**重建内存态，不引入第二真相源——与全仓 event-sourcing 与
README「事件 + reducer 是唯一真相」一致。

### reducer 折叠 HITL（`rebuild_view` 扩展）

`recover_session`（runtime.py:751）已 `rebuild_view(event_store, session_id)` 回放事件重建 Session/Task。
**扩展 reducer**（`core/control/reducers.py`）把 HITL 事件折进 view 的一个新分量：

```
pending_hitl: { request_id → HitlRequest }   # 仅未解决的
  HitlRequired              → 新增一条 pending（kind/cap/tool_call_id/question/context）
  HitlAnswered/Approved/Modified/Rejected/Cancelled(同 request_id) → 从 pending_hitl 移除
```

> view 里只留 **pending** 的即可（已解决的对恢复无意义）。`tool_call_id` 随 `HitlRequired` payload 落事件
> （§6 短路门控的键），回放即得，跨重启可用。**无需把 arguments 入事件**——approval reconcile 的原参走
> §6 从 memory 的 tool_call 取。

### 运行期 listing 不依赖表

`/hitl/pending`、`list_pending` 仍读**内存** `HitlManager.list_pending`——运行期本就在内存。重启后由下方恢复
重建内存 HitlManager，listing 自然恢复。故**既不需要表、也不需要查事件**来支撑运行期列表。

### 重启恢复（必须）

- 启动 `recover`（`runtime.recover`，**core 据事件决策、不全量回放、启动不 drain/不跑任何东西**）：对每个 active
  session,用 event store 轻查询 `read_session_events_of_types(HITL_STATUS_EVENT_TYPES)` 取 HITL 类事件、
  `fold_pending_hitl` 折叠出仍未解决的 `{id: HitlRequestView}`：
  - **有未解决 pending HITL** → **只 `hitl_manager.rebuild_pending`（仅重建内存 HitlManager）**,使
    `/hitl/pending`、`/messages`、`/hitl/{id}/*` 三个应答入口重启后即可命中;状态留 `PAUSED_HITL`、**不**标
    `INTERRUPTED`、不显示恢复条。**task 重建 + drain 推迟到应答到达时的 `recover_session`**（应答前不跑任何 task）。
  - **否则**（崩溃前在跑）→ **emit `SessionStatusChanged(new_status="INTERRUPTED")`**(等用户 `/resume`)。
  故**启动期对称**:PAUSED 等应答、INTERRUPTED 等 `/resume`,两者都不在重启时跑任何机器工作。
  分层（**事件驱动、无回调**）:host 调 `runtime.recover()` → core 据事件决策 → 中断**只是一条事件**,由 host
  既有订阅者(`ProjectionUpdater` 写持久 `sessions.status`)处理,和别的事件一条路。
  内存 `_sessions` 缓存不是总线订阅者(无连接时无 per-session consumer),故 **`load_sessions_from_db` 必须排在
  `recover()` 之后**,从已更新的投影把 `INTERRUPTED`/`PAUSED_HITL` 灌回缓存(见 `run_startup`)。
  **早先几版已废弃**:"跳过 PAUSED_HITL"→ 重启后 HitlManager 为空 → 应答入口 404;"host 回调标 INTERRUPTED"→
  core 反向调 host、与事件溯源不一致;"启动即 `recover_session` 全量重建"→ 会 drain、把 PAUSED 会话的并行兄弟
  任务在重启时自动跑起来(与 INTERRUPTED 需点击不对称)。恢复策略从事件导出也比 host 投影更准(多 HITL 部分解决时
  投影会误回 RUNNING,事件折叠仍判 pending)。`sessions.status=PAUSED_HITL` 仍由 §9.1 持久化,只用于灌回 UI 状态,非决策依据。
- `/resume → recover_session`：在已做的 `rebuild_view` 里顺带拿到 `pending_hitl`，**据它重建内存 HitlManager**
  （`_futures` 为空 → 后续应答自动走冷 resume）。
- `restore` 判"该 task 有无未解决 pending HITL" = 查 **同一 replayed view 的 `pending_hitl`**（按 task_id 过滤），
  **不查库、不再回放**——view 已在手。

### 9.1 host 须持久化 session 级 `PAUSED_HITL`（已定：持久化）

session 级暂停状态分两个 host read-model，现状割裂、须补齐持久层：

| read-model | 现状 | 处置 |
|------------|------|------|
| 内存 `api/models/session.py:281` | **已**处理 `SessionPausedHitl` → 置 `PAUSED_HITL`（SSE/API 实时） | 不动 |
| 持久投影 `projection_updater` | **未**处理——`PAUSED_HITL` 仅在白名单（projection_updater.py:26），无 handler 写它 | **须补 handler** |

- `load_sessions_from_db` 启动时从持久 `sessions` 表灌回内存；持久层不记 PAUSED_HITL → 跨重启暂停态丢失、
  且被 `recover` 误标 `INTERRUPTED`。故**须在 `projection_updater._handle` 补**：`SessionPausedHitl` → `status=PAUSED_HITL`；
  HITL resolve（session 回 `RUNNING`）→ 反向写回 `RUNNING`。
- 这**不是新表、不是 HITL 细节持久化**——只是把已有事件接到已有 `sessions.status` 列，仍是**从事件维护的读模型**
  （可重建、非第二真相源）。HITL 的**细节**仍走事件重放（§9 顶部）。

### 与既有崩溃恢复的接驳（必须）

既有崩溃恢复两段：`recover`（runtime.py:814）启动时把无终态事件的 session 标 `INTERRUPTED`；
`recover_session`（runtime.py:743）在 `/resume` 时 `rebuild_view` → `restore` → 重设同一 runner → drain。
**与本提案接驳两点：**

1. **复用 reconcile（白赚）**：崩溃恢复重跑的 task 经同一 `_resolve` 漏斗；若它崩在工具批次中途，
   memory 里就是 dangling，§6 检测自动路由到 reconcile 补完——**修掉现状里"崩溃后带 dangling 直接
   reason→act、喂给 LLM 非法序列"的潜在 bug**，零额外成本。

2. **`restore` 必须区分两种 SUSPENDED（必须修）**：`restore`（task_manager.py:117）现把 `SUSPENDED`
   且子任务全终态的 task 重新入队为 `PENDING`。**HITL-park 的 task 是 `SUSPENDED` 且无子任务 → 空集恒真
   → 会被误重排、在人类未应答时跑起来。** 须改为：该 task 若有**未解决的 pending HITL**（§9 持久化已记），
   则保持 parked（不 requeue），等 `/answer` 触发 resume。判据复用 HITL 持久化，无需新增 task 字段。

---

## 10. host / API 影响

- `/answer`、`/approve`、`/reject` 端点签名不变；内部据 `_futures` 命中与否分流热（set Future）/冷
  （写结果 + 触发该 session 恢复）。
- `HumanConfirmationAuthorizer(hitl_manager=runtime.hitl_manager)` 等装配点不变（仍单实例共享，
  cli.py:95 / main.py:28）。前端不变：据 `request.kind` 渲染审批/答题，`HitlRequired`/`HitlAnswered` 驱动 UI。
- **持久投影补 session 状态维护（§9.1，非新表）**：`projection_updater` 加 `SessionPausedHitl` → `PAUSED_HITL`、
  resolve → `RUNNING` 的 `sessions.status` 维护，使跨重启暂停态不丢、`recover` 不误标 `INTERRUPTED`。

---

## 11. 取舍小结

| 维度 | 现（纯阻塞，超时=失败） | 本提案（热/冷两层，超时=降级） |
|------|------------------------|--------------------------------|
| 快速回复 | 就地续跑 | 就地续跑（热路径，等价） |
| 慢回复 / 久挂 | 误判失败 或 协程永驻 | 驱逐内存、保留 pending、晚到照常 resume |
| 跨重启 | 丢失 → INTERRUPTED | 可恢复（请求即持久化 + 全走冷） |
| input-kind 实现量 | 现成 | 中（热=现成；冷=复用委派挂起） |
| approval-kind 实现量 | 现成 | 热=现成；冷=高（`defer` + act-step 重入） |
| 与架构一致性 | 唯一阻塞等待路径 | 冷路径与委派/事件溯源一致 |

---

## 12. 未决问题

> 已定：① approval 冷路径走**精确重入**（§6 reconcile 只对 dangling 调用 `gateway.invoke`；已完成的复用 `TOOL_RESULT`）；
> `TOOL_RESULT` 写入点统一为 `gateway.invoke`（热/冷同一）。
> ② reconcile 作**独立 step**，触发判定在 `_resolve` 漏斗、基于 memory 的 dangling 检测，**无专用标记**。
> ③ reconcile **统一接驳崩溃恢复**（§9）：同一 step 补完 HITL-park 与崩溃中途批次；`restore` 须区分
> "SUSPENDED-on-children" 与 "SUSPENDED-on-HITL"。
> ④ HITL 持久化走**事件重放、不加表**（§9）：写模型本就是 HITL 事件，reducer 折叠出 `pending_hitl`，
> 恢复沿用既有 `rebuild_view`；运行期 listing 仍读内存。

- **新事件 `HitlCancelled`**：§3 的 `cancelled` 终态需新增此事件并登记进 `EVENT_TYPES`（spec/01 冻结清单，
  须先改 01 再实现）；projection_updater 据它把行置 `cancelled`。
- 超时阈值的定位：纯**内存/存活**旋钮（热窗口多久后驱逐），与 UX 无关——默认值？可配置粒度？
- 是否引入独立 `RunSuspended` 事件，还是复用 `RunFinished` + session `PAUSED_HITL`？
- `cancelled` 触发面：仅 session 关闭 / interrupt，还是也含「pending HITL 的 task 被 reopen/replan 作废」？

---

## 13. 对跨语言一致性清单（05 §5）的影响

落地后需在 05 增删：

- 删：`wait` 超时即 `timeout` 失败态 / `HitlTimeout` / resolve-after-timeout 幂等（改为热/冷分流）。
- 改：状态机为 `pending → answered/approved/rejected/cancelled`；**超时 = 内部 hot→cold 降级，不改持久状态**。
- 增：请求即持久化；热=set Future 就地续跑、冷=写结果 + 重新入队 + drain + 上下文重建。
- 增：`_futures` 命中与否作热/冷判别；重启后全冷。
- 增：park 信号管线（穿过 gateway except、落 SUSPENDED 非 FAILED）；竞态单一权威转移。
- 增：pending HITL 跨重启恢复（PAUSED_HITL 续跑，非 INTERRUPTED）。
- 增：reducer 折叠 HITL 事件出 `pending_hitl`（恢复用，沿用 `rebuild_view`）；运行期 listing 仍读内存；新事件 `HitlCancelled`。
- 增：host 持久投影维护 session 级 `PAUSED_HITL`↔`RUNNING`（§9.1，`projection_updater` 现缺，非新表）。
- 增：独立 `reconcile` step + `_resolve` 基于 memory 的 dangling 检测；统一补完 HITL-park 与崩溃中途批次。
- 增：`restore` 区分 SUSPENDED-on-children / SUSPENDED-on-HITL（后者有未解决 pending HITL → 不 requeue）。
- 增（若做 approval 冷路径）：`AuthorizationDecision.defer` + gateway「defer→挂起、绝不 invoke」。
- 增：冷应答分流在 core（`HitlManager.on_cold_resolve(req)` → runtime）；host 只转发回复、不感知 `was_hot`。
- 增：observe 的 `ask_human`（`submit_task_assessment`）冷路径走**注入**而非 reconcile（§6.1）——
  按 `capability_id` 判别,recover 时把回复作 `USER_PROMPT` 注入 task 层 + 置 PENDING（镜像 finalize）。

---

## 14. 复用边界 / 风险与回归面

> 落地心智：本提案**不是**"把崩溃恢复代码搬过来"。恢复的**重建+漏斗骨架**可复用，但 HITL 持久化是
> 净新增、`restore` 是承重改动、park/驱逐是新路径，外加一处会改变副作用语义的行为变更。风险从
> "纯新增 feature"挪到了"动崩溃恢复主路径"——后者回归会炸**所有 session 的恢复**，不只是 HITL。

| 类别 | 内容 | 风险 |
|------|------|------|
| **A · 可直接复用** | resume 漏斗 `_resolve → run_task → _make_task_runner`；`recover_session` 的 `rebuild_view`（回放/快照+增量）+ projection→domain 转换；`drain`/`_run_task` 挂起处理；`restore` 重排队机制本身 | 低（现成、跑过） |
| **B · 净新增（孤立）** | ① `reconcile` step 全新（扫 last assistant turn、找 dangling、仅对 dangling `gateway.invoke`、已完成复用、短路门控）；② **HITL 持久化**——老 `recover_session` **根本不带 HITL**，须先持久化 pending HITL 并在恢复时重建（老代码无此段可搬） | 中（孤立、可单测） |
| **C · 改承重老代码** | ① `restore`（task_manager.py:117）加 SUSPENDED-on-children / on-HITL 区分——改的是**所有崩溃恢复都走**的路径，空子集恒真陷阱即在此（改错=人未应答就跑任务）；② park/驱逐路径——老恢复"进程死了从头重建"，**从不需要把活协程干净卸载成 SUSPENDED**，热→冷驱逐是全新代码 | **高（回归面=全体恢复）** |
| **D · 语义变更（需明确接受）** | reconcile 把崩溃中途批次从"PENDING→reason→act、**靠 LLM 重判**"改成"**确定性重跑** dangling 工具"。对非幂等副作用是确定性 **at-least-once 重放**（可能比现状更激进）。只保证已写 `TOOL_RESULT` 的不重跑，半成品副作用无法原子化 | 中（取舍，须写死） |

**依赖前提（落地前先确认）**：整套依赖 spec/06 assembler（两 source 重建）能产出干净序列、且 memory 能
廉价回答"某 `tool_call_id` 有无 `TOOL_RESULT`"。06 在 README 标"未实现"，但 git log 显示 step 4/5/6
已部分落地——**其实际成熟度直接决定 reconcile 检测可不可靠**，不能假设已齐。

**建议落地顺序**（按风险递增、可独立验证）：
1. B：HITL 持久化（§9）+ `reconcile` step（§6），先只接 input-kind 冷路径——孤立、可单测。
2. C-①：改 `restore` 的 SUSPENDED 区分 + 崩溃恢复接驳（§9）——单独一批，重跑全部恢复回归。
3. C-②/D：park/驱逐 + 超时降级 + approval `defer` 冷路径——最后上，受 D 语义取舍约束。
