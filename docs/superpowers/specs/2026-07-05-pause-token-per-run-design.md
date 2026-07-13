# Pause 令牌 per-run 化 + pause_session 弃子留 root + pause_task 定向暂停

日期：2026-07-05
分支：refactor/hitl-id-unify
状态：设计定稿，待实现

## 1. 背景与现状

### 1.1 现行机制

- **令牌本体**（`src/ctx_weft/core/control/tokens.py:34`）：`PauseToken` 为一对
  asyncio.Event；`resume()` / `wait_if_paused()` 在现行流程中是死代码——令牌从不复位，
  只被整体丢弃换新。
- **存放粒度**（`runtime.py:473`）：`_pause_tokens` / `_cancel_tokens` 均为
  `dict[session_id → token]` 的 **session 单值**。三处造新并覆盖：`start_session`、
  `recover_session` 冷重建、`_resume_in_existing_tm`。
- **注入**（`runtime.py:1655-1656`）：`_SessionTaskRunner.execute` 派发时从 session dict
  现读一次，焊进本 run 的 `LoopContext`，run 存续期间不刷新。
- **触发**：host `POST /sessions/{id}/interrupt` → `runtime.pause_session(sid)` → 单
  token `.pause()`。
- **消费**：act 三类协作检查点（turn 开头 `act.py:65`、流式逐 chunk `act.py:223`、工具
  中/间 `act.py:381`）。命中 → `_park_wait_for_user`（`act.py:596`）→ 冷 `wait` HITL、
  session PAUSED、task SUSPENDED、抛 `HitlPark`。
- **回收**：会话空闲 `_on_idle` 弹掉两 token（`runtime.py:757-758`）。

### 1.2 并发下的问题

TaskManager 默认 `max_concurrent=4`，root + 子 agent 任务并行时：

- **P1｜一次 pause 扇出 N 个 wait 气泡**：`_interrupt_checkpoint` 对每个在跑 task 独立
  park，用户按一次"暂停"拿到 N 个续跑点，多 pending 面板弹 N 个气泡。
- **P2｜令牌跨代失联**：任务 A park、兄弟 B 在跑时冷应答 A →
  `_resume_in_existing_tm` 整体换新两 token（`runtime.py:982-983`），B 焊的旧令牌永远
  收不到后续 pause/cancel。cancel 同病（`cancel_all` 只清队列，在途全靠 CancelToken）。
- **P3｜旧代 inflight 失联**：`recover_session` 允许旧 TM 的 inflight 任务跑完
  （`runtime.py:917-922`），它们不在任何现行令牌的覆盖范围内。

根因：**信号作用域（session 单值）与消费者（per-run 快照）粒度错位**。

## 2. 设计

### 2.1 令牌层：runtime 级 per-run registry

- 新增 `CtxWeftRuntime._run_tokens: dict[session_id → dict[task_id → RunTokens]]`，
  `RunTokens = (cancel: CancelToken, pause: PauseToken)`。
  **放 runtime 不放 TM**：旧 TM 被顶替后其 inflight run 仍登记在册（根治 P3）。
- 登记/注销：`_SessionTaskRunner.execute` 派发时造一对新 token 登记，`finally` 注销。
  `LoopContext.cancel_token/pause_token` 结构不变，装的对象改为本 run 的 token。
- 出生信号（2026-07-05 M-1 修订）：若该 session 的 `_pausing` 闩锁（见 §2.2）为真，
  按本次派发的执行 agent 分流——root agent 的 run 出生即 `pause()`，非 root run 出生即
  `cancel()`。原设计一律 born-pause：多级委派时被 `_try_resume_parent` 重排的**中间**
  agent 父任务会在检查点先命中 pause（检查点 pause 先于 cancel）而 park，气泡落中间
  agent 抢走续跑点；改 born-cancel 后中间层协作取消并逐级级联到 root agent 的任务。
- 续跑点名额一次性（2026-07-05 再修订，`_pause_claimed`）：root agent 可同时拥有多个
  任务（后继任务/多条消息各起一个 root scope 任务），同 agent 串行只保证同时至多一个
  run。名额在 pause_session pause 到在途 root run、或闩锁窗口内首个 root run born-pause
  时认领；此后窗口内再派发的 root run（如子任务死光被重排的 SUSPENDED root 任务）一律
  born-cancel——"一次暂停恰一个续跑点"在多 root 任务拓扑下也成立。park 中的 run 非终态，
  其祖先不会被 `_try_resume_parent` 重排，合法等待中的父任务不受误伤。名额与闩锁同
  生命周期（`_on_idle` / `_release_session` / pause_session 兜底一起清）。
- 删除 session 级 `_pause_tokens` / `_cancel_tokens` 及其三处创建、`_on_idle` /
  `_release_session` 中的弹除；`pause_session` / `cancel_session` 改走 registry。
- `_SessionTaskRunner` 构造参数中的 `cancel_token` / `pause_token` 及兜底逻辑删除。
- **连带**：`compact_session` 现以 `session_id in self._cancel_tokens` 做 idle-guard 并
  借该 dict 占位（`runtime.py:1025-1028`）。换成独立 `_busy_sessions: set[str]`（同步
  claim / finally release），语义不变。
- `cancel_session` 重写：遍历该 session registry 中**所有**在途 run 的 CancelToken 逐一
  cancel + `cancel_all` 清队列（修 cancel 侧 P2/P3）。idle 回收判定沿用现行逻辑。

### 2.2 pause_session：弃子、只留 root agent 当前那一轮

对外 API（`POST /sessions/{id}/interrupt` → `runtime.pause_session`）不变。语义：

> 放弃该 session 其余全部在途/排队任务（硬取消终态），只保留 **root agent 正在进行的
> 那一 run** 作为唯一续跑点（park 一个 wait 气泡）。

划分标准是**执行 agent**，不是 `parent_task_id is None`：真相源为 TM 的
`_running_agents: dict[task_id → 真实执行 agent_id]`（`task_manager.py:71`）；同 agent
串行判定保证 root agent 同时至多一个 run 在跑——"那一轮"天然唯一。该 run 跑的可能是
同 agent 子任务而非 root task；park 在哪就从哪续跑，延续的是 root agent 的对话。

流程：

1. 置 runtime 级 `_pausing[sid]` 闩锁 → 此后新派发 run 按执行 agent 分流出生信号：
   root agent run 出生即 paused，非 root run 出生即 cancelled（M-1 修订，见 §2.1）。
2. 在途 run 中 `_running_agents[task_id] == session.root_agent_id` 的那个（≤1 个）→
   pause 其 PauseToken → 既有检查点 park（一个 wait 气泡）。
3. 其余在途 run → 逐个 cancel 其 CancelToken → 协作取消 → 终态 CANCELED（工具补
   「已取消」合成结果）；队列中所有 pending 任务清除标 CANCELED（不分 agent——只保
   "正在进行的"那一轮）。队列弃子不经 on_task_finished，故 abandon_pending 对每个被
   弃任务补触发 `_try_resume_parent`（2026-07-05 修订）：否则某 SUSPENDED 父任务的
   子任务若在暂停瞬间**全部**还在排队（无一在途），无人重排父任务 → 会话滞留
   RUNNING 且无气泡。重排检查幂等：兄弟仍在途时 all_done 不成立、由其
   on_task_finished 接力。
4. root agent 当下没在跑（其 task SUSPENDED 等子任务）：子任务取消后
   `_try_resume_parent` 沿链逐级上溯——多级委派时中间 agent 的父任务先被重排，其新 run
   出生即 cancelled、在首检查点协作取消（M-1 修订，不 park、不烧 LLM），再触发上一级
   重排；直到 root agent 的 SUSPENDED task 重排，其新 run 出生即 paused → reconcile
   归位悬挂 tool_call → act 第一轮 checkpoint park，**不烧 LLM**。
5. root agent 的 run park → session PAUSED → `is_done` + pending HITL →
   `_fire_session_idle` → 清 `_pausing` 闩锁。竞态兜底：若 root run 在信号抵达前恰好
   正常收尾、会话走向终结，`_release_session` / `_on_done` 路径同样清除闩锁，防止残留
   影响同 session 的下一轮。
6. `on_task_finished` 的 CANCELED 分支（`task_manager.py:599-601`）加守卫：
   `_pausing` 期间的取消**不**把 session 置 CANCELED（是 pause 弃子，不是用户取消）。

root agent 续跑时经既有 reopen prompt 可见子任务 CANCELED 状态，由模型自行决定是否
重派（硬取消终态语义，不做半截产出汇总）。

### 2.3 pause_task：定向暂停指定 task

- core：`runtime.pause_task(session_id, task_id) -> bool`。task 在跑 → pause 该 run 的
  PauseToken → 该 task 自己 park 一个 wait 气泡；不在跑 → False。
  act 的 park 逻辑**零改动**：谁被 pause 谁 park；pause_session 场景下非 root run 走的
  是 cancel，根本到不了 park。
- host：`POST /sessions/{sid}/tasks/{task_id}/pause`；session 不存在 404、task 不在跑
  409。
- 恢复：复用现成链路——多 pending 面板回复该 wait → 冷应答 →
  `_resume_in_existing_tm` → `resume_task(task_id)` 就地重驱，无新代码。
- 不做"暂停子树"：只停指定 task 本身的 run，其在跑子任务不受影响。

### 2.4 明确不做

- 不保留 PauseToken 的 `resume()` / `wait_if_paused()`（死代码删除）。
- 不做 pause_session 弃子的半截产出汇总（选定硬取消终态）。
- host UI 的 pause_task 入口不在本次范围（只出 API）。
- 不改 act park / HITL 解析 / 多 pending 面板任何行为。

## 3. 不变式

- `/interrupt` 点一次：会话转 PAUSED，恰好新增**一个** wait 气泡（root agent 的）；
  既有 parked HITL（如 ask_user）不受影响。
- pause / cancel 信号必达该 session **所有**在途 run（含旧 TM inflight）。
- 暂停引发的子任务取消不改变 session 终态语义（不出现 CANCELED 会话）。
- token 生命周期与 run 严格对齐：run 结束即注销，无滞留、无跨代残影。

## 4. 测试要点

- per-run registry：并发多 run 各自令牌独立；run 结束注销；`_pausing` 下新 run 出生即
  paused。
- pause_session：root agent 在跑 → 恰一个 wait 气泡 + 其余 CANCELED；root agent 空闲
  （等子任务）→ 子任务取消 → 祖先重排 → 出生即 paused → park 且无 LLM 调用；
  session 状态 PAUSED 而非 CANCELED。
- P2 回归：A park + B 在跑 → 冷应答 A → 再 pause → B 能收到（park 或按新语义
  cancel）。
- pause_task：在跑 task park 自己的 wait 气泡、兄弟不受影响；不在跑 → False/409；
  回复该 wait 恢复该 task。
- cancel_session：多在途 run 全部协作取消（含旧 TM inflight）。
- compact_session busy-guard 行为不变（`_busy_sessions` 等价替换）。
