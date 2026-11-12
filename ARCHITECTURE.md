# ctx-weft 内部实现逻辑

> 本文讲 **引擎内部如何运转**：循环引擎、Step 流水线、上下文装配、任务编排、记忆模型 v2、会话生命周期与崩溃恢复、事件系统内部。
>
> 想知道**怎么调用 SDK**，见 [README.md](./README.md)。这里的内容对编写自定义 Provider、调试事件流、扩展引擎有帮助，但日常使用不必读。

启动与装填的当前边界：同步构造完成公共接线；已有会话执行前 `await initialize(session_id)` 校准持久崩溃事实。
同一会话初始化共享 Task，成功后只保留 ID；纯查询不初始化，不枚举其他会话。
查询优先内存、回退事件临时投影，不登记运行对象。已有会话只由 `rebuild_session` 统一
装填 Session、Agent、HITL、TaskManager 及接线；开启工作入口在同一会话锁内先确保装填。
`_bind_task_manager` 保持唯一 owner 守卫，TaskManager 构造统一复用 SessionRegistry 的工厂。
装填不 drain、不恢复 recap；真正执行前才激活 deferred recovery。
`SessionStatusChanged` 已停发，仅兼容旧日志；会话状态由 host 维护，启动校准只补写 run、task、agent 与 HITL 恢复事实。
投影和快照保存未结束 run 的任务、agent 及最大 sequence；`RunFinished` 将该 run 销账，
任务裁剪不裁这份执行信息。启动校准复用快照加增量，不再另读完整 run 历史；旧版本快照回退全量日志。

前后台 observe 共用 `ObserveRequested → ObserveStarted → ObserveCompleted → ObserveApplied`。

`report_observation.task_status` 使用四态：`success` / `fail` 结束任务，`continue`
一律自动重新入队执行，`waiting` 停在 `AWAITING_HUMAN` 等待用户回复。`continue`
不消耗运行异常重试预算，也不兼容为旧的等待语义。交互任务的纯文本回合先临时 park，
后台判 `continue` 后先完成段摘要、收口本轮等待入口，再发布 `TaskRequeued` 并启动下一轮；
判 `waiting` 则保留入口。前台判 `waiting` 同样建立可应答的 HITL 入口。
后台自动续跑以 `ObserveCompleted.action="continue"` 持久化，恢复时完成原决定而不重新判决。
`max_turns` 复核会展示本段已执行轮数：明确超出能力范围可判 `fail`，其余结果
由代码收敛为 `continue`，不允许 `waiting` 或 `success`，观察失败也继续执行；其余场景
观察没有可用判决时的机械 `success` 兜底保持不变。

机械判决在前台调度入口由同步方法先返回，随后将 `verdict`、任务处置计划及
`judge_task=false` 写入 `ObserveRequested`。前台直接进入 finalize，不调用 ObserveStep；
后台沿同一个 `observe_id` 执行一次 ObserveStep 生成报告。执行是否判决只由 `judge_task` 控制。
前台收尾的崩溃恢复由 runtime 负责，observe 内不再手动推进 FinalizeStep。
LLM 执行后才确定的判决仍写入 `ObserveCompleted`。机械判决驱动的收尾允许早于报告完成，
但 `ObserveApplied` 必须晚于任务处置与报告应用。报告订阅任务状态通知后读取持久化事实，
因此晚订阅不依赖历史通知；应用时校验 finish 记录的 observe 归属，并与取消收尾串行。
执行逻辑和四个事件的组装、发送统一位于 `core/loop/steps/observe.py`；driver 在调度前调用意图持久化入口，TaskManager 在仲裁和结算位置调用 observe 提供的回调。
Requested 在执行前持久化（park 时嵌入 `HitlOpened`），Started 在观察前立即写入；Completed
保存报告及执行后确定的判决。机械判决路径由 Requested 先于任务收尾，Completed 先于报告应用；
LLM 判决路径由 Completed 先于判决应用。Applied 只在结果应用完成后销账。
`TaskRecapRequested/Started/Done` 已停发，仅供旧日志重放。快照保存 `pending_observe` 的结果、
已完成副作用与任务状态提交标记。冷恢复已有 Completed 时不再调用模型，沿原决定幂等补完。
前台仍由 TaskManager 在 RunFinished 后落任务状态；机械判决与异步报告属于同一次 observe。
收尾报告交接以完整持久化的 finish 对为准：其 metadata 保存原文保留策略和槽位数量；后台在真实报告写入、必要折叠完成后才发送 Applied。前后台先后次序仅影响等待，进程内通知只唤醒，不承载报告或恢复决定。
前台 observe 可协作暂停/取消；暂停时旧 Completed（放弃判决）、旧 Applied 与携带新
recap 请求的 HitlOpened 原子提交。气泡提交确认后才可回复；失败重试复用原事件封套。
热恢复先确认停止 run 的原始起止事件，再按已提交交接事实补齐等待状态、槽位释放和 recap。
未完成判决的冷恢复由 TaskManager 持有前台槽位和 dispatch，注册真实暂停/取消令牌；
已接受的判决只恢复收尾。root、跨 agent 和同 agent 子任务的暂停均安排 report-only recap。
observe 请求通过 `context_record_ids` 固定输入记录；memory 的 `read_records` 按原 ID
读取含 superseded 的记录，并保持租户/会话隔离。装配、短段门与折叠共用这份范围；
后续收尾已经接管的段不再追加旧摘要。旧快照遗漏引用时，从原请求事件补齐。
新 user 回合的 HitlResolved 与消息回合在同一批提交，确认后才发布 claim 状态、写入暂存
memory；首字前撤销仍丢弃整批，并恢复原气泡。
熔断的 `FailureThresholdHit` 先持久化 root 失败及清场目标；在途 root 的 `TaskFailed`
必须晚于其前台 `RunFinished`。启动校准独立闭合所有遗留 run，再兑现尚未完成的熔断决定。

---

## 目录

1. [架构总览与数据流](#1-架构总览与数据流)
2. [核心组件职责](#2-核心组件职责)
3. [Loop 引擎与 Step 流水线](#3-loop-引擎与-step-流水线)
4. [LoopState / LoopContext / StepOutcome](#4-loopstate--loopcontext--stepoutcome)
5. [上下文装配（ContextAssembler）](#5-上下文装配contextassembler)
6. [能力解析与调用（CapabilityGateway）](#6-能力解析与调用capabilitygateway)
7. [任务编排与 Agent 解析](#7-任务编排与-agent-解析)
8. [记忆模型 v2 与写入时机](#8-记忆模型-v2-与写入时机)
9. [Blackboard / Topic 机制](#9-blackboard--topic-机制)
10. [会话生命周期与崩溃恢复](#10-会话生命周期与崩溃恢复)
11. [事件系统内部](#11-事件系统内部)

---

## 1. 架构总览与数据流

```
                         ┌──────────────────────────────────────┐
                         │           CtxWeftRuntime              │
                         │  SessionRegistry   create/resume      │
  TemplateLookup ───────▶│  AgentLifecycleManager（ALM）          │
  ProviderRegistry ─────▶│  TaskManager        队列 / drain        │
   memory/cap/know/llm   │  HitlService/Registry/ReplyIntake      │
                         └───────────────┬────────────────────────┘
                                         │ 两阶段派发（task/runner.py）
                                         ▼
        ┌──────────────────────  一次 loop run  ──────────────────────┐
        │  assemble: ALM instantiate/materialize → AgentBinding        │
        │  execute : ContextAssembler ──▶ AssembledPrompt              │
        │            CapabilityGateway ──▶ invoke / 鉴权 / HITL        │
        │            LLMClient         ──▶ 流式 chunk                  │
        │                                                                │
        │  StepDriver: prepare → act → observe → finalize               │
        │     （compact 为正式前台步骤；recognize_intent 旁路并发）        │
        └───────────────────────────┬────────────────────────────────────┘
                                    │ RunOutcome（execute 的返回值）
                                    ▼
                          TaskManager.apply_run_outcome
                                    │ make_event(...) / emit
                                    ▼
                EventBus（+ CommitGate：先确认提交再通知，spec: event-commit）
                                     │ 订阅
                          ┌──────────┴──────────┐
                     EventStore            外部订阅者（SSE / 日志）
              (InMemory / SQL + 快照)
```

**一句话**：`CtxWeftRuntime` 把 task 交给**两阶段 TaskRunner**——`assemble` 装配执行 agent（`AgentBinding`），`execute` 驱动 StepDriver 按 `next_step` 顺序跑 Step；run 的**结局**以 `RunOutcome` 返回值交回 TaskManager（task 状态由 TM 据[处置表](#7-任务编排与-agent-解析)落地，loop 不写状态），事件全部经 `EventBus` 流出、`EventStore` 落盘。

**为什么结局走返回值而非事件**：进程内 bus 在 `emit()` 里同步 drain——TM 拿到结局后要动队列、派下一个 task、发更多事件，全嵌套在最初那次 `emit()` 里层层重入。数据走返回值，TM 在 run 返回**之后**动手（`core/orchestrator/task/runner.py:44`）。

---

## 2. 核心组件职责

> 路径相对 `src/ctx_weft/`，行号对应当前分支（`feat/multimodal`）工作区，可点击跳转。

| 组件 | 源码（file:line） | 职责 |
|------|------------------|------|
| `CtxWeftRuntime` | `core/runtime.py:522` | 顶层编排。运行入口：`run_single_task` `:1249`（compat）、`start_session` `:1387`、`send_message` `:2649`、`reply_to_hitl` `:2987`、`recover_agent` `:1950`、`compact_agent` `:2385`、`pause_session` `:828` / `pause_agent` `:1111` / `resume_agent` `:1157`、`cancel_session` `:906` / `cancel_agent` `:965`、`rebuild_session` `:1721` 等 |
| `ProviderRegistry` | `core/registry.py:35` | 四类 provider（memory 唯一 / cap 列表 / knowledge 按 priority / llm 唯一）+ per-provider authorizer（`:107`）+ memory/event blob store（`:143` / `:170`）+ skill provider 增减的 `mark_dirty`（`:115`） |
| `AgentLifecycleManager`（ALM） | `core/orchestrator/lifecycle/agent_manager.py:116` | agent 生命周期的唯一住所：`instantiate` `:566`（真新建，发 AgentSpawned/SpawnRejected/AgentInstantiated）、`materialize` `:799`（水合，零事件）、`resolve_model` `:783`、事件驱动 `load` `:421`（吃 TASK_* 事件回放装填 registry） |
| `TemplateLookup` | `core/orchestrator/lifecycle/template_lookup.py` | 模板解析；`resolve_qualified` 接受限定形式（`agent__x`），裸 id 抛 `TemplateNotFoundError` |
| `SessionRegistry` | `core/orchestrator/lifecycle/session_registry.py:68` | `create_session` `:128`（发 SESSION_CREATED，因果序 Session→Agent→Task）、`resume_session` `:231`（回放重建 + `UnfinishedTasksError` 弃轮禁止 `:262`） |
| `TaskManager` | `core/orchestrator/task/manager.py:54` | 任务队列与调度：`drain` `:502`、`_run_task` `:536`、`apply_run_outcome` `:638`、`reopen_chain` `:707` / `reopen_task` `:746`、`restore` `:143`（崩溃恢复重建）、`track_background` `:119`、未提交窗口 `begin_round` `:281` / `commit_round` / `discard_round` |
| `ContextAssembler` | `core/assembler/assembler.py:238` | 多 Source 取数 → budget 裁剪 → composer 拼 prompt（`assemble` `:246`） |
| `CapabilityGateway` | `core/loop/capability_gateway.py:193` | capability 解析、鉴权/HITL、参数校验、`invoke` `:236`、memory 写入 + 事件 |
| `CapabilityCache` | `core/capabilities/cache.py:28` | **per-session 共享**能力缓存（含 pin / clear_pins / available `:140`-`:170`）；run 结束 `evict`（`core/runtime.py:3907`），pin 清理挂 task 终态（`core/runtime.py:1548`） |
| `StepDriver` | `core/loop/driver.py:253` | 按 `initial_step` 起步，`run` `:270` 循环执行 Step，发 StepStarted/Completed/Failed |
| `EventBus` / `InProcessEventBus` | `protocols/events.py:320` / `providers/events/bus/in_process/bus.py:41` | `emit` `:53`、`subscribe` `:115`（支持 provisional）、未提交窗口 `begin/commit/discard_provisional` `:141`-`:155`、`stream` `:157`。进程内 emit **同步 drain**、不跨进程 |
| `EventStore` / `InMemory` + SQL | `protocols/events.py:477` / `providers/events/store/in_memory/store.py:29` / `providers/events/store/sql/` | `append_batch`、`read_range`、`committed_head`、`read_last_of_type`——**四个方法全必需，没有可选扩展档** |
| HITL 三件套 | `core/hitl/registry.py:183`（决定缓存）、`core/hitl/service.py:80`（`open` `:96` / `resolve` `:158` / `cancel` `:179`）、`core/hitl/reply_intake.py:29`（人类答复入核） | 人工介入请求/应答；等待协程栈在 `core/loop/hitl_waiter.py` |

**构造期自动注册的内置 Capability**（`CtxWeftRuntime.__init__`，`core/runtime.py:575`-`590`）：

- `ControlCapabilityProvider`（`core/capabilities/control_tools.py:623`）— 控制工具：`delegate_task` `:167` / `delegate_plan` `:234` / `finish_task` `:309` / `report_task_outcome` `:391` / `update_task_metadata` `:525` / `collect_process_report` `:504` / `ask_user` `:600`。（旧名 submit_task / submit_plan / submit_task_assessment / request_human_input 已改名。）
- `SkillExecutorCapabilityProvider`（`core/capabilities/skill_executor.py:151`）— 把 LLM 的 skill 调用路由到对应 `SkillCapabilityProvider`；`mark_dirty` `:167` 重建索引。
- `MediaCapabilityProvider`（`core/media/capability.py:341`）— `media:get_image` 等媒体工具。

构造期还硬校验**至少一个 `AgentCapabilityProvider`**（`core/runtime.py:608`-`603`）。

---

## 3. Loop 引擎与 Step 流水线

`StepDriver.run()`（`core/loop/driver.py:270`）是引擎心脏。流程：

1. **起步前持久化 user_prompt**（`_persist_user_prompt` `core/loop/driver.py:208`）：落库的是**原样 raw content**（多模态无损，`MemoryKind.CONVERSATION_TURN / TASK / role=user`）；`## Current Task` / `## Current Message` 框架由 composer **渲染期**生成、不落库。记录 id 存 `task.user_prompt_memory_id` 供丢弃路径 fold。
2. **起步钩子**：`ensure_dispatch_frame_at_start`（见 [§8](#8-记忆模型-v2-与写入时机)）——子任务此刻在父 scope 铸派发框 + running ack。
3. 从 `initial_step` 开始循环：
   - 每轮先检查 `cancel_token`，已取消则抛出。
   - 发 `StepStarted` → `step.execute(state, ctx)` → 应用 `outcome.state_patch` → 发 `outcome.events` → 发 `StepCompleted`；异常发 `StepFailed` 再 raise。
   - `next_step = outcome.next_step`；为 `None` 时循环结束。

### Step 注册表与跳转

`_build_step_driver(initial_step)`（`core/runtime.py:3729`）注册 **8 个** Step：

| Step | 源码（name= 行） | 作用 | `next_step` |
|------|------------------|------|-------------|
| `prepare` | `core/loop/steps/prepare.py:103` | 能力解析/绑定 + token 估算 + 命中阈值时切换 compact，完成后带 after_compact 回本步重装配 + 装配 prompt + guidance/resume cue 注入 | `"act"` 或 `"compact"` |
| `act` | `core/loop/steps/act.py:60` | ReAct 循环：调 LLM → gateway 执行 tool_call → 写 memory，直到 actor_done / max_turns / context_limit / 纯文本 | `suspend_requested` → `"suspend"`（`core/loop/steps/act.py:156`），否则 `"observe"` |
| `observe` | `core/loop/steps/observe.py:216` | 裁决：root/跨 agent 任务走机械判决 + 后台 observe；子任务走 LLM observe（`report_task_outcome` 为终止工具） | `"finalize"`（`core/loop/steps/observe.py:293`） |
| `finalize` | `core/loop/steps/finalize.py:686` | 写 finish 对 / 派发结果回填父 scope / blackboard 发布 / 发 TASK_FINALIZED；**不写 task 状态** | `None`（`core/loop/steps/finalize.py:801`） |
| `suspend` | `core/loop/steps/suspend.py:22` | 当前 task 等子任务，挂起；TaskManager 在子任务完成后重新入队 | `None` |
| `compact` | `core/loop/steps/compact.py:733` | 记忆压缩（统一由 StepDriver 执行；options.after 指定接续） | `"prepare"` 或 `None` |
| `recognize_intent` | `core/loop/steps/recognize_intent.py:99` | 旁路意图识别：回填 root task 的 title/description/session goal（`update_task_metadata`） | `None`（单发） |
| `reconcile` | `core/loop/steps/reconcile.py:23` | resume 后补 dangling tool_call：已有结果的复用、悬挂的经 gateway 重执行 | `"prepare"` |

**`initial_step` 的选定**（`_SessionTaskRunner.assemble` 里的 `_reconcile_or`，`core/runtime.py:4172`）：最近 assistant turn 有 dangling tool_call → `"reconcile"`（探测函数 `_task_has_dangling_tool_call` `core/runtime.py:480`），否则 `"prepare"`。`AgentBinding.initial_step` 默认 `"prepare"`（`core/orchestrator/task/runner.py:34`）。

**step 参数与 compact 的三个入口**：

`StepOutcome.next_step_options` 指定下一次 step 的调用参数，driver 复制后写入该次
`ctx.execution.options`，不会自动继承到后续步骤。跨 step 的执行结果仍通过
`state_patch` / `LoopState` 传递。前台 `act → observe` 显式传 `boundary` 与 `judge_task`；
后台 Observe 仍经 `start_durable(..., options=...)` 发起，保留持久化请求与恢复语义。

- **Prepare 预算触发**：`prepare → compact → prepare → act`。Compact 接收
  `trigger/token_estimate/force/after`；`after="prepare"` 时返回
  `next_step_options={"after_compact": True}`。第二次 Prepare 从 source 重新装配，
  不再检查压缩阈值，避免重复触发；一次性 hint 在最终进入 Act 前消费。
- **Act 派发前触发**：`act → compact → prepare → act`。已持久化、已记账但尚未执行工具的
  回合存入 `LoopState.pending_act_turn`；压缩保护该回合并从摘要输入排除。Act 重入后先恢复
  原工具调用，不重问模型、不重复记账，不消耗新的模型回合；工具结果落定后清除接续状态。
- **runtime 手动触发**：`compact_agent` 保留 idle guard 与 `CompactReceipt`，通过 driver 的
  前台执行入口运行 Compact，`force=True, after=None`。仅结束维护 run，不启动 Act 或终结业务 task。

Compact 在可能改写记忆前清除旧的增量 token 基线；重装配使用完整估算，后续真实 Act usage
重建基线。压缩 noop 仍只回 Prepare 一次，并保留上下文恢复次数与压不动时的止损规则。

#### CompactStep 与 memory 整理边界

`CompactStep` 在 `core/loop/steps/compact.py` 内维护 L0.5 → L1 → L2 → L3、
模型预算、tokenizer、摘要调用及事件。普通模式达标即停，force 持续到适用级别结束。
进入该 step 后先通过会话的 `ExecutionRegistry` 等待同 agent 全部后台 Observe 收尾，
包括同 task 较早的运行和恢复启动的运行；不等待其他 agent 或元信息生成。等待前发出
`MemoryCompactStarted(phase="waiting_observe")`，等待不持有 memory/任务锁，取消等待不
取消 Observe。等待后重新装配未裁剪的当前任务 prompt 并计算完整预算，再决定是否压缩。

`MemoryProvider.organize` 在既有 memory 协议中定义五种操作：`demote_images`、
`reduce_history`、`fold_history`、`fold_current`、`fold_segment`。默认实现位于
`providers/memory/organize.py`，不依赖 loop、TaskManager、Agent 事件、LLM 或预算。
两个存储后端在各自事务/锁内实现条件检查和替换；`fold` 的公开契约不变。

Compact 通过 `recall_for_organize` 取得普通 MemoryRecord，将现成材料交给 assembler；
Source 不重复召回。摘要成功后以相同输入 ID 调用 organize，不把旧摘要用于新选的范围。
L3 只摘要实际前缀，近期窗口不进入摘要输入。材料召回只读；折区残留图片由 Compact
显式调用 demote_images 预处理，返回新 ID 后使用新记录。

Observe 继续决定是否折段并生成报告，以固定观察输入调用 `fold_segment`。该操作保留
用户边界与已有摘要；它和跨多段的 `fold_current` 是不同操作。收尾创建和报告回填仍在
现有 `task_memory_fold.py` / Finalize 路径，不在这次迁移中顺带改造。

运行时已有 LoopContext.memory；传入 provider 的 ProviderContext 仅携带身份和作用域。
没有新 Organizer 注册、公开 Plan/prepare/apply、ContextVar 或 CompactSnapshot。
默认实现复用有界的不可变正文缓存，始终重新检查活动成员，按租户/会话隔离缓存键。
普通工具通过调用/结果关联识别完成状态；被排除出对话协议的派发/silent 调用通过非渲染
的执行关联及完成审计记录提供相同事实，不再由 Compact 传递 protected_ids。
