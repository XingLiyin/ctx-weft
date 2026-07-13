# 设计：TaskManager 两阶段派发——装配（assemble）与执行（execute）分离

日期：2026-07-04
状态：已获用户批准（brainstorming 定稿）
前置阅读：`docs/superpowers/specs/2026-07-04-tm-mechanism-and-recovery-design.md`（单 owner 架构 / run_task seam 背景）

## 1. 动机

当前 `TaskRunner = Callable[[session_id, task_id], None]` 是单一闭包契约：`runtime._make_task_runner`（runtime.py:773）把 session/template/LifecycleManager/memory/token 全部捕获进 130 行闭包，内部 `_resolve`（装配执行 agent）与 `_execute_task`（执行任务）焊在一次调用里。由此产生五个结构性病灶：

1. **装配状态藏在闭包里**：`_resolved_agents` 是闭包私有变量，恢复路径只能靠 `pre_resolved_agents` 参数重播种，agent 身份没有一等归属地。
2. **串行键预测重复**：`TaskManager._effective_agent`（task_manager.py:263）必须与 `_resolve` 非 subagent 分支（runtime.py:866）手工保持一致（两处注释互相指认）；根因是「任务跑在哪个 agent」的决策发生在执行时，调度器只能提前复算。
3. **TASK_STARTED 契约靠注释维系**：只有 `_resolve` 之后才知道真实 agent id，事件必须由 runner 发，TM 侧只能注释声明契约（99edd41 修的双发 bug 即此耦合产物）。
4. **装配失败与执行失败共用重试路径**，不可区分。
5. **单 owner seam 是在闭包上打的洞**：token/model 派发时从 per-session dict 读、闭包值作兜底，是闭包捕获与「参数是消息」原则的冲突补丁。

## 2. 约束与决策记录

- 方案选型（用户拍板）：**TM 派发时两阶段**——改 TaskRunner 契约，TaskManager 在派发点先装配、再执行；而非仅在 runtime 内提取组件。
- 上游同步（用户拍板）：**不考虑上游 LoomeX-00 回灌，本仓 core 自由演进**。
- host（`src/ipmastercowork/`）零改动；`run_single_task`（Phase 1 兼容路径）不动。

## 3. 架构

两阶段协议替换单闭包契约，TaskManager 派发序列变为：

```
drain() 选中 entry（串行判定见 §5）
  → binding = await runner.assemble(task_id)          # 装配：决定并实例化执行 agent
  → TM 回填 task.assigned_agent_id / started_at；TM 发 TASK_STARTED
  → await runner.execute(binding, task_id)             # 执行：step loop 跑任务
  → 收尾分流（SUSPENDED / PENDING / 终态）不变
```

runtime 侧 `_make_task_runner` 闭包工厂改写为 **`SessionTaskRunner` 类**：闭包捕获变显式字段，`_resolved_agents` 变实例属性；恢复播种 = 构造参数（语义同现状 `pre_resolved_agents`）。

## 4. 组件与接口

- **`AgentBinding`**（core 新 dataclass）：`agent: Agent, template: AgentTemplate, initial_step: str, run_id: str`——即现 `_resolve` 返回 tuple 落名分。TM 只读 `binding.agent.id`，其余字段对 TM 不透明。
- **`TaskRunner` 协议**（core/orchestrator）：`async assemble(task_id) -> AgentBinding` + `async execute(binding, task_id) -> None`。TM 的 `set_runner` 接受实现该协议的对象。
- **`effective_agent_id(task, root_agent_id) -> str` 纯函数**（core/orchestrator 模块级）：「任务跑在哪个 agent scope」的单一真相。`TaskManager._effective_agent` 与 `SessionTaskRunner.assemble` 的非 subagent 分支都调它。subagent 未定时返回 `__sub__{task.id}` 占位（只作串行键，永不碰撞，同现状）。
- **`SessionTaskRunner`**（runtime 侧）：
  - `assemble` = 原 `_resolve`（subagent template 解析、`lm.instantiate_agent`、loop_guard 替换、tracking memory flush、inherit memory 复制、reconcile 探测 initial_step）+ 写入实例 registry；
  - `execute` = 单 owner seam（派发时从 `self._cancel_tokens`/`self._pause_tokens`/`session` 读 token/model，闭包值兜底）+ `_execute_task` + `handle._state` 更新。seam 语义原样保留。

## 5. 串行判定收紧

`drain()` 的 busy_agents 现全靠预测；改造后：

- **在跑任务**：用装配时的真实 agent id（TM 派发时记 `task_id → binding.agent.id`）。
- **队列候选**：仍需预测（装配发生在 pop 之后），但预测与装配共用 `effective_agent_id` 同一函数——漂移在结构上不可能。

## 6. 事件与错误处理

- TASK_STARTED 归 TM：每次派发（含 retry/resume）恰好一条，payload 带真实 `assigned_agent_id`；「runner 必须自发」的注释契约删除。
- `assemble` 抛错 → 现有 `_handle_task_failure` 重试路径不变，但 TASK_REQUEUED/日志标注 `assembly_failure` 与运行失败区分（最小处置，不引入独立重试策略）。装配失败时 TASK_STARTED 未发出，投影不会出现幽灵 ACTIVE。
- `execute` 内 HitlPark / LLMOutageError / CancelledError / ContextOverflowError 分流全部不动（都在 `_run_loop` 内部）。

## 7. 不动的部分

TaskQueue（LIFO+DAG）、staged/flush/detach 机制、`on_task_finished` 收尾、`_try_resume_parent`、`reopen_task/chain`、`restore` 规则、`recover_session` 两条恢复路（仅 `pre_resolved_agents` 改传类构造器）、`_resume_in_existing_tm`、`run_single_task`、host 层。

## 8. 测试

- 现有 TM 单测的 stub runner（test_hitl_*、test_superseded_task_manager 等）改为 stub 两阶段对象，tests 加共用 helper；删除各测试里「stub 需自行补发 TASK_STARTED」的噪音。
- 新增：
  1. TASK_STARTED 每次派发恰好一条（锁死 99edd41 双发不回归）；
  2. `effective_agent_id` 纯函数单测（subagent / assigned / creator / root / per-task 兜底五分支）；
  3. 装配失败走 retry 且不发 TASK_STARTED。
- 全量 `uv run pytest` 作行为不变回归网（事件序、恢复、HITL park 等既有测试）。
