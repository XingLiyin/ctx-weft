# dispatch 段边界 recap 设计

> 状态：已定稿（2026-07-16，三节均经用户确认）。

## 背景与问题

段 raw 的折叠（recap 成 `TASK_COMPACT_SUMMARY`）目前只有四个触发点：

1. **close 边界**（`finish`/`normal`）— 后台 observe 产 recap，finalize 收成胶囊（`observe.py:276-280`）
2. **park 边界**（`interrupt`/`plain_text`）— 后台 observe → `apply_compact` 写段摘要（`act.py` 多处）
3. **retry 边界** — 同步 `_fold_retry_segment`
4. **token 压力** — `_maybe_predispatch_compact`（`act.py:110`）按 `predispatch_compact_token_ratio` 门控跑 `escalating_compact`；L3 才无差别坍当前 task，非语义化段 recap

**缺口**：dispatch 本身不是段边界。父 agent 派发 sub task 前积累的探索/决策 raw，在父
SUSPENDED 期间原样保留，子结果回来 resume 后继续全量背着跑——只有等到下一次用户打断、
纯文本暂停、close 或 token 越限才被动处理。而父 SUSPENDED 等子的空窗恰是跑后台 recap
LLM 最无竞争的时机。

## 已确认的决策

| 问题 | 决定 |
|---|---|
| 折叠语义 | dispatch = 段边界，与 interrupt/plain_text 同级 |
| 作用范围 | 所有委派父（不限 root，不加 `_is_own_root` 门控） |
| resume 竞态 | 父 resume 前 `await_pending_background_observe`（强一致） |
| 实现形态 | 方案 A：后台折叠 + resume 等待（否决了挂起路径同步折叠） |

## §1 触发点与数据流

**触发**：`SuspendStep.execute` 末尾（挂起摘要已写、`TASK_SUSPENDED` 事件即将发出）追加
fire-and-forget：

```python
launch_background_observe(state, ctx, boundary="dispatch")
```

不加 `_is_own_root` 门控——所有委派父生效。这是唯一触发点：所有 delegate 路径
（`delegate_task`/`delegate_plan`，单发或批量）最终都路由到 SuspendStep，一处覆盖全部。

**分流**：`"dispatch"` 不加入 `_CLOSE_BOUNDARIES`，在 `_run_background_observe` 里自动走
非 close 分支，与 `interrupt`/`plain_text` 完全同路：

1. 重跑幂等护栏（段内无 active `LLM_RESPONSE` → 跳过）——复用
2. 短段免折门 `is_short_segment`（raw ≤ `short_segment_token_threshold` 保 raw 跨边界累积）——复用
3. `apply_compact(layer=TASK, keep_last=0, protect_types=(USER_PROMPT, TASK_COMPACT_SUMMARY))`
   写段摘要——与 interrupt 边界同参数

**挂起摘要的去留**（实现期事实修正，2026-07-16）：SuspendStep 写的 `OBSERVER_SUMMARY`
（"Delegated to sub-task(s)…Awaiting completion"）**不参与本折叠，也无需参与**。
`OBSERVER_SUMMARY` 是半僵尸类型（`protocols/memory.py:76` 枚举注释）：`EVENT_LAYER`
映射为 AGENT 层——TASK 层的 `apply_compact` 按层过滤、折不到它；且它**不进装配**、
仅影响计数/估算口径。原设计目标「resume 后窗口不残留挂起摘要」天然成立：派发事实由
agent 层派发框承载，recap 复述覆盖过程叙事。特征测试改为断言层级隔离（折叠后
`OBSERVER_SUMMARY` 原样留存、不受 TASK 层折叠影响）。

**恢复语义**（spec §5.1 对齐）：挂起期间进程崩溃 → `recover_session` 对 SUSPENDED-且-有
未完成 recap 的 task 走既有 `_relaunch_task_recap` 路径重跑（`boundary` 从持久化 info 取，
新增值 `"dispatch"` 天然兼容，兜底默认 `"finish"` 不受影响）。

## §2 resume 等待点与并发边界

**等待点**：run 启动处（`runtime._run_loop` 入口，`driver.run` 之前）统一
`await await_pending_background_observe(task.id)`。不放 PrepareStep 里——resume 的
run 有两个入口（`initial_step="prepare"` 常规 / `"reconcile"` dangling tool_call 重放，
`runtime.py:1857,2056`），放 run 启动处一次覆盖两者，且保证在任何 memory 读写之前。

**语义**：每个 run 开跑前先等自己 task 的在途 recap。对本设计的 dispatch 边界是强一致
（SUSPENDED 期间父不写 raw，折叠无干扰源；子结果回来后 run 被 recap 拦到折完才装配）。

**顺带收紧的两个既有竞态**（同一 await 点免费获得，非本设计目标但应更新文档注释）：

1. interrupt/plain_text park 后用户回复唤醒的 run，此前与在途 recap 可并发（装配可能
   读到折叠中途的状态），现在也被拦住；
2. `background_observe.py` 模块 docstring 记录的崩溃恢复 best-effort 竞态（§5.1）：
   `_relaunch_task_recap` 与 restore 重排的新 run 并发——只要 relaunch 先于 run 启动
   （恢复路径的既有顺序），新 run 现在会等 recap 完成，同进程内该竞态实质闭合。
   docstring 需相应改写。

**并发边界情况**：

- **秒回子任务**：resume 被 recap 拖住数秒——已接受的代价（决策表第 3 行）。
- **SUSPENDED 期间被取消**：统一取消胶囊闭合 funnel 不等 recap。在途 recap 可能在取消后
  写下一条 `TASK_COMPACT_SUMMARY`——良性：折的是取消前已存在的 raw，胶囊内容语义等价
  （raw 换 summary），finish 对合成不受影响；session 收尾 `_fire_session_done` 已 gather
  track_background 登记的后台任务，不会泄漏协程。接受，不加同步。
- **同一父多轮 dispatch**：每轮 SuspendStep 各触发一次；`_lock_for(task_id)` 串行化同 task
  recap，`protect_types` 保住前轮段摘要累积（与多轮 plain_text 同构）。
- **dispatch 前紧邻 interrupt recap 在途**：`_task_pending` 只存最新，旧的在锁上排队——
  run 启动 await 的是最新 pending；旧 recap 持锁跑完才轮到新的，时序安全。

## §3 错误处理与测试

**错误处理**（全部复用非 close 分支既有语义，无新增状态）：

- recap LLM 失败 / 无可用报告 → 段保 raw，log 后吞掉（降级 = 不折叠，spec §3.6）；
  dispatch ∉ `_CLOSE_BOUNDARIES`，不触碰 `_close_synth`/`_close_report`，无泄漏面。
- run 启动 await 处的 recap 异常（实现期修正，2026-07-16）：`_run_background_observe`
  只对 LLM/装配/apply_compact 主体自吞异常；护栏区（幂等护栏、短段门、事件 emit）在
  自吞 try 之外，recap task 可能以异常终结、await 会 re-raise。故 run 启动 await 外包
  防御 try/except（吞掉并 log，降级 = 不等待、段保 raw——与模块降级语义一致），保证
  run 不因 recap 异常在 RUN_STARTED 之前无声崩掉；shield 保证 run 被取消时不牵连 recap。
- 非 root scope 跑 `purpose="background_observe"` 装配若因模板/能力缓存缺失抛错 → 落入
  既有 except 分支，段保 raw——sub-agent 最坏退化为现状，不会更糟。

**测试**（对齐既有 test_background_observe*.py 布局）：

1. `test_background_observe_wiring.py` 增：SuspendStep.execute 触发
   `launch_background_observe(boundary="dispatch")`；非 root task 同样触发（无
   `_is_own_root` 门控）。
2. `test_background_observe.py` 增：boundary="dispatch" 走非 close 分支——
   `apply_compact(layer=TASK, keep_last=0, protect_types=(USER_PROMPT,
   TASK_COMPACT_SUMMARY))` 参数断言；短段免折门命中时跳过；幂等护栏（无 active
   LLM_RESPONSE）跳过；挂起摘要 OBSERVER_SUMMARY 被折入（不特护）。
3. 新增 `test_run_start_awaits_recap.py`：慢速假 recap 在 `_task_pending` 挂着时启动
   run，断言 driver 第一步在 recap 完成之后才执行（顺序断言）；无 pending 时零开销直通。
4. 集成路径（现有 orchestrator 集成测试文件里加用例）：父派发 → 子完成 → 父 resume 后
   task 层可见 TASK_COMPACT_SUMMARY、派发前 raw 已 superseded、agent 层派发框完好。
5. 取消交叉：SUSPENDED + 在途 recap 时 cancel_all——断言取消胶囊闭合正常、recap 事后
   完成不抛错、`_fire_session_done` 不悬挂。
6. 恢复路径：持久化 info 带 boundary="dispatch" 时 `_relaunch_task_recap` 正常重跑
   （沿用既有恢复测试的构造方式）。
7. docstring 更新断言不了——人工检查项：`background_observe.py` 模块头的竞态记录改写
   （§2 第 2 点）。
