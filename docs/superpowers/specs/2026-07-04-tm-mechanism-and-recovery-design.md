# TaskManager 机制全景 + 恢复/HITL 自洽方案

> 日期：2026-07-04
> 目的：先把整套 TM 机制梳理成体系，再据此给出一个围绕**根因**的自洽方案，取代此前逐点打补丁的思路。
> 关联：`2026-07-03-session-hitl-park-recovery-fixes.md`（该文的缺陷 A/B/C 是本文根因的症状）。

---

# Part 1 · TM 机制全景（reference）

## 1.1 组件与职责

| 组件 | 位置 | 职责 |
|---|---|---|
| **TaskManager**（TM） | `orchestrator/task_manager.py` | 单 session 的任务调度：队列、并发闸、派发、父子 resume、会话 done/idle |
| **TaskQueue** | `orchestrator/task_queue.py` | LIFO 队列 + `_running` + `_completed` 三集合；`has_pending`=`bool(_entries)` |
| **runner / loop / steps** | `runtime._make_task_runner` → `_run_loop` → `driver.StepDriver` → `loop/steps/*` | 真正执行一个 task：装配 prompt、调 LLM、跑工具、观察、收尾 |
| **HitlManager** | `orchestrator/hitl_manager.py` | HITL 请求/等待/解决；**热/冷分流的唯一真相**（`was_hot`） |
| **runtime 入口** | `runtime.py` | `start_session`（新建/续聊）、`recover_session`（崩溃恢复+冷应答）、`recover`（启动批量） |
| **reducers / 投影** | `control/reducers.py` + `events/types.py` | 事件→状态投影；崩溃恢复的"真相"来源 |

## 1.2 一个 task 的执行控制流

```
drain() ──pop──► _run_task(task_id)
  set ACTIVE + emit TASK_STARTED
  await runner(sid, task_id)                    ← runtime._make_task_runner 的 run_task
       └─ _resolve(agent, template, initial_step, run_id)   initial_step = reconcile(有dangling) | prepare
       └─ _execute_task → _run_loop → StepDriver.run(state, ctx)
             while next_step:  STEP_STARTED → step.execute() → apply patch → STEP_COMPLETED
  ← runner 返回后按 task.status 分派（见 1.5 的 _run_task 四分支）
```

**Step 路由图**：
```
reconcile ─►prepare          (resume 补 dangling tool_call 后必回 prepare，不直达 act)
prepare ──►act
            ├ task.status==SUSPENDED ─►suspend ─►None(结束，等子/等应答)
            └ 否则 ───────────────────►observe ─►finalize ─►None(结束)
                                                   ├ success/fail：终态
                                                   └ retry：TASK_REQUEUED, status=PENDING
旁路(fire-and-forget，不在主循环)：recognize_intent(root首轮)、background_observe(段边界)
```

## 1.3 两种 park —— **这是理解一切的关键**

系统有**两条完全不同的 HITL 挂起路径**，状态记账不同：

| | **热 park** | **冷 park** |
|---|---|---|
| 典型触发 | `ask_user`、审批门控（bash_exec 等） | `wait_for_user`/interrupt(`_park_wait_for_user`)；热等超时驱逐；gateway defer |
| 机制 | 协程 `await hitl.wait(future)` **阻塞不退出** | `raise HitlPark`(BaseException) → unwind |
| runner 是否返回 | **否**（一直挂在 `await runner`） | **是**（HitlPark 在 `_run_loop` 被吞，runner 正常返回） |
| **task.status（内存）** | **ACTIVE**（没人改） | **SUSPENDED**（act.py `_park_wait_for_user` / `_run_loop` 置） |
| **task 投影状态** | **ACTIVE**（最后事件 TASK_STARTED） | **ACTIVE**（！）——冷 park 走 `RUN_FINISHED(SUSPENDED)`，**不发 TASK_SUSPENDED**，且该 RUN_FINISHED 的 SUSPENDED 被 reducer 显式丢弃 |
| session 投影 | `SESSION_PAUSED_HITL` → PAUSED_HITL | 同左（wait_for_user → PAUSED；审批 → PAUSED_HITL） |
| 应答路径 | `was_hot=True` → `future.set_result` **就地续跑，不重建 TM** | `was_hot=False` → `_on_cold_resolve` → **recover_session（重建 TM）** |

> 唯一例外：`delegate_*` 派生子任务后父任务走 `next_step="suspend"` → SuspendStep 才发 `TASK_SUSPENDED`（投影 SUSPENDED）。这是"等子任务"的挂起，与 HITL 无关。

**两个由此而来的关键不一致（后面所有 bug 的源头）：**
- **不一致 A（投影层）**：无论热 park 还是冷 park，**task 投影状态都是 ACTIVE**（冷 park 的 SUSPENDED 只在内存、不进投影）。崩溃恢复读的是投影 → 恢复时看到的 parked 任务几乎都是 ACTIVE。
- **不一致 B（内存层）**：冷 park 内存 SUSPENDED、投影 ACTIVE，二者漂移；`_emit_task_failed` 的注释已承认这种漂移会让任务"在投影里停留 ACTIVE、被 restore 误复活"。

## 1.4 状态机（精简）

**Task**（`TASK_STATUS_BY_EVENT`, events/types.py:177-185 是投影唯一真相）：
`TASK_STARTED/RESUMED→ACTIVE`、`TASK_SUSPENDED→SUSPENDED`、`TASK_REQUEUED→PENDING`、`TASK_FINISHED/FAILED/CANCELED→终态`。
注意：**HITL 冷 park 不发 TASK_SUSPENDED**；热 park 不发任何 task 事件 → 两者投影都停在 ACTIVE。

**Session**（只由 `SESSION_*` 事件驱动投影）：
- `PAUSED`（软待命，wait_for_user）vs `PAUSED_HITL`（等决策，ask_user/审批）——**同一事件 `SESSION_PAUSED_HITL`**，reducer 按 `capability_id` 分流。
- `INTERRUPTED`：只由 `_emit_session_interrupted` 发（崩溃恢复无 pending HITL 的 active session；或 LLM outage）。
- `SUCCEEDED`/`FINISHED`：`on_task_finished` 在 `is_done()` 为真时发 `SESSION_STATUS_CHANGED(SUCCEEDED)` + `SESSION_FINISHED`。
- HITL 解决事件（APPROVED/ANSWERED/…）把 PAUSED* 拉回 RUNNING（仅当仍是暂停态）。
- **单标量、last-writer-wins**：多任务并发时会在 RUNNING↔PAUSED_HITL 间横跳，表达不了"部分任务在等审批"。

**done vs idle**（判据同为 `is_done()`，语义相反，靠调用位置区分）：
- `is_done()` = `not queue.has_pending() and not _running_tasks`。
- `_fire_session_done`（终结、发 SESSION_FINISHED、`_release_session`）：`on_task_finished` 里任务终结后 is_done。
- `_fire_session_idle`（不发事件、保留 TM、回收 per-run token）：`_run_task` 的 SUSPENDED 分支里 is_done。

## 1.5 TaskManager 生命周期与"多 TM 是常态"

`_register_and_drain` 是唯一"挂 TM + drain"的地方，它**无条件覆盖** `_task_managers[sid]` 并注入 `_is_current` 谓词。三个入口都经它：

| 入口 | 何时建新 TM | 备注 |
|---|---|---|
| `start_session`（create / resume=new-run） | 每次新建会话、每次 terminal→new-run 续聊 | host `/messages`、`POST /sessions` |
| `recover_session`（崩溃恢复 + **每次冷 HITL 应答**） | **每次冷应答/`/resume` 都新建一个** | 已加 per-session resume 锁串行化 |
| `recover`（启动批量） | **不建 TM**：有 pending HITL→只 rebuild HitlManager 并 defer；否则发 INTERRUPTED | 建 TM 交给应答的 recover_session |

**所以"同一 session 多个 TM"是设计常态**。协调机制：
- `drain()` 顶部 `_is_current` 守卫：被顶替的旧 TM 立即停止派发（本次已加）。
- `_fire_session_done` 在 gather 后再判 `_is_current`：旧 TM 迟到收尾 no-op。
- `_on_done` compare-and-clear：只有仍是 owner 才 `_release_session`。
- per-session resume 锁：串行化重叠的 recover_session（本次已加）。

**未覆盖的缝**：旧 TM **已 create_task 出去、正在跑的 `_run_task` 协程不会被取消**；新 TM 又从投影重排同一个非 parked 任务 → **跨 TM 双跑**。

## 1.6 父子 / spawn 调度
- `delegate_*` → `stage_task`（投 `_staged` 桶、不立即入队）+ 父 `SUSPENDED`；runner 正常返回后 `_flush_staged` 才入队（同批 FIFO）。
- 父 SUSPENDED 等子；子 `on_task_finished` → `_try_resume_parent`（**判定+翻 ACTIVE+push 必须同临界区**，防两子并发双 resume）。
- 同 agent 串行：`drain` 用 `_effective_agent` 跳过忙 agent；跨 subagent 并行保留。
- spawn-inherit：`_copy_memory_for_inherit` 把父召回视图镜像进子 scope。

## 1.7 cold-resume 触发链
`host approve/answer/reject` → `HitlManager._resolve`（`was_hot=False`）→ `_on_cold_resolve` → `runtime._resume_after_cold_hitl` → `recover_session`。热应答（was_hot=True）**不走这条**，就地 `future.set_result` 续跑。

---

# Part 2 · 两个根因 + 自洽方案

## 2.1 根因 I —— "parked（在等人）"不是一等、一致表示的状态

真相"这个任务在等人类输入"实际存在于 **HitlManager 的 pending / HITL 事件**里；但系统各处却用**别的、彼此不一致的代理指标**去判断它：

| 判断点 | 用的指标 | 结果 |
|---|---|---|
| `restore()` 保持 park | `task.status == "SUSPENDED"` | 漏掉 ACTIVE 的审批 park（**缺陷 A**）→ 重派 → churn |
| `is_done()` 判完成 | queue + running 两集合 | 漏算 parked（既不在 queue 也不在 running）→ 多任务下**会话虚假完成**、孤立 parked 任务、释放 TM |
| session 投影状态 | last-writer-wins 单标量 | 并发 HITL 时横跳成 RUNNING，藏住"在等审批"（**缺陷 C**、"显示运行中却卡死"） |
| task 投影状态 | TASK_* 事件 | 冷/热 park 都停在 ACTIVE，与"在等人"脱节 |

四个症状同一个病：**park 的真相在 HITL 层，判断却在别处**。

## 2.2 根因 II —— recover_session 混淆"崩溃恢复"与"应答续跑"，且每次都重建 TM

`recover_session` 一身二职：(a) 崩溃后从事件从零重建 TM；(b) 每次冷应答的续跑。因为它**总是新建 TM 覆盖映射**、从不复用已存在的活 TM：
- 崩溃恢复若有多个 pending HITL → 逐个应答 → 多次 recover_session → **多个 TM 相互顶替**；
- 旧 TM 在跑的协程没被取消、新 TM 又重排同任务 → **跨 TM 双跑**；
- 这也是"崩溃恢复为什么会有 TM 顶替"的答案：不是崩溃本身，是"应答续跑复用了崩溃恢复的重建路径 + 从不复用活 TM"。

## 2.3 自洽方案（围绕根因，不再逐点打补丁）

### 方案 I：让"有无未决 HITL"成为 park 的唯一判据

统一原则：**凡是"该不该继续跑 / 会话算不算完成 / 该显示什么状态"的判断，都以"该 task 是否有未决 pending HITL"为准，不看 task.status 这个实现细节。** parked 集合已经现成（`recover_session` 里的 `parked_task_ids`，源自 `fold_pending_hitl`）。

具体落点：
1. **restore()**：把 `if t.id in parked: continue` 提到状态分支之前，对任何非终态任务生效（缺陷 A）。
2. **is_done() / 完成判定**：TM 持有 parked 集合，`is_done()` 增加"无未决 HITL 的 parked 任务残留"这一条；有 parked 任务时会话停 idle、绝不 done。（否则缺陷 A 修好后会暴露"多任务虚假完成"。）
3. **session 投影状态（缺陷 C）**：`recover()`/恢复时，有 pending HITL 就 emit `SESSION_STATUS_CHANGED(PAUSED_HITL)`，让读模型如实反映"在等审批"；不被 task 级事件刷回 RUNNING。
4. **（可选、更彻底）消除两种 park 的表示不对称**：让冷 park 也发 `TASK_SUSPENDED`（或让恢复端一律以 pending-HITL 重新判定），使投影 task 状态与"在等人"一致。这条能同时收敛不一致 A/B，但改动面大，评估后再定。

> 本质：把散落在 restore/is_done/投影 里的三套"parked 假设"，收敛到"pending HITL"这一个真相上。

### 方案 II：分离"恢复"与"续跑"，续跑复用活 TM

目标：同一 session 任一时刻**至多一个活 TM**，消除顶替与跨 TM 双跑。

首选（较大但根治）：
- `recover_session` 进锁后先判 `_task_managers.get(sid)`：
  - **无活 TM**（真崩溃恢复 / 首次 `/resume`）→ 现流程（重建）。
  - **有活 TM**（冷应答续跑）→ **不重建**，把该 HITL 的解决与（必要时）user_reply **注入现有 TM**，对相关 task 重排/重驱（等价于热路径的"就地续跑"），而非造第二个 TM。

退一步（较小、防御性）：
- 保留"每次重建"，但加 **session 级"在跑 task id"闸**（提到 runtime 层、跨 TM 共享）：`_run_task` 真正执行前查"该 task 是否已在别处跑"，是则跳过。这样无论几个 TM，同一 task 不会有两个并发 runner。
- 并把顶替做成**主动 quiesce**：新 TM 接管前，取消旧 TM 在跑协程并等其 unwind（需要给 `_run_task` 存 asyncio.Task 句柄 + 可取消 HITL 等待）。

### 2.4 与已完成项的关系

| 已完成 | 作用 | 相对根因 |
|---|---|---|
| `IPMC_TASK_MAX_CONCURRENT=1`（串行默认） | 同时至多一个在跑 task → 多 parked/多 TM 场景基本凑不齐 | **掩盖**根因 I/II，非根治 |
| per-session resume 锁 | 串行化重叠 recover_session 的建立阶段 | 方案 II 的**一半**（还差"复用活 TM / 取消旧协程"） |
| `drain()` 的 `_is_current` 守卫 | 被顶替旧 TM 停止**后续**派发 | 方案 II 的一半（拦不住已在跑的协程） |

即：串行默认让问题"当前不发作"，但根因仍在；一旦放开并行（`max_concurrent>1`）就会复现。方案 I/II 才是根治，且与已完成项叠加无冲突。

## 2.5 分阶段落地建议

- **阶段 1（低风险、立即价值）**：方案 I 的 1+2+3 —— restore 用 parked 判据（缺陷 A）、is_done 感知 parked（防虚假完成）、恢复时如实 PAUSED_HITL（缺陷 C）。三者同属"以 pending-HITL 为准"，一批测试覆盖：
  - restore：ACTIVE+parked → 不入队；SUSPENDED+parked → 不入队（回归）。
  - is_done：一个任务完成 + 另一任务 parked → 会话不 done、停 idle。
  - 恢复：带 pending HITL 的会话 recover → 投影 PAUSED_HITL。
- **阶段 2（根治并发下的 TM 问题）**：方案 II —— 优先"复用活 TM"；若评估过大，先上"session 级在跑闸"作为防御。测试：多 pending 会话连答两个 → 无同任务重复 TaskStarted、无第二个 TM 接管在跑任务。
- **阶段 3（可选彻底）**：方案 I 第 4 条 —— 统一两种 park 的投影表示（冷 park 补 TASK_SUSPENDED / 恢复端一律按 pending-HITL 重判），消除内存↔投影漂移。

## 2.6 取舍与风险
- 方案 I 是"判据归一"，低风险、无并发依赖；缺陷 A 单独修**必须**配 is_done 感知 parked，否则把"虚假完成"从 SUSPENDED-park 扩大到 ACTIVE-park。
- 方案 II "复用活 TM"改动 recover 语义，需谨慎回归多轮对话/`/resume`/冷应答的既有测试；"取消在跑协程"有中断恢复中任务的风险，故 quiesce 需要可取消的 HITL 等待与 Task 句柄管理。
- 阶段 3 改事件流（新增 TASK_SUSPENDED 发射点）会影响历史事件回放的一致性，需迁移/兼容评估。

---

---

# Part 3 · 实现状态（2026-07-04 落地）

三阶段均已按 TDD（先写失败测试→改→跑绿）实现，落 master 工作区。核心测试全绿；仅存的
`test_fs_config.py` 2 例失败与本次无关（FilesystemConfig 默认值，预存）。

## 阶段 1（方案 I：判据归一，以 pending-HITL 为准）— 已实现
| 改动 | 文件 | 说明 |
|---|---|---|
| 缺陷 A：restore park 判据前移，对 ACTIVE/SUSPENDED 一律生效 | `task_manager.py restore()` | `if t.id in parked: continue` 提到 status 分支之前 |
| is_done 感知 parked：完成判定加"无未决 HITL"闸 | `task_manager.py __init__/set_has_pending_hitl/on_task_finished` | is_done 本身不改；在 `on_task_finished` 完成分支加 `if _has_pending_hitl(): fire_idle else: fire_done`，避免多任务虚假完成 |
| 谓词注入 | `runtime.py _register_and_drain` | `set_has_pending_hitl(lambda: bool(hitl_manager.list_pending(sid)))` |
| 缺陷 C：恢复如实标 PAUSED_HITL | `runtime.py recover()` + 新增 `_emit_session_status` | 有 pending HITL 的会话恢复 → `SESSION_STATUS_CHANGED(PAUSED_HITL)`，不再停在崩溃前 RUNNING |
| 测试 | `test_hitl_recovery.py` | `test_restore_keeps_active_parked_task_out_of_queue`、`test_session_not_finished_while_a_task_parked_on_hitl`、`test_session_finishes_when_no_pending_hitl`、`test_recover_emits_paused_hitl_for_pending_session` |

## 阶段 2（方案 II：消除跨 TM 双跑）— 已按**复用活 owner 架构**实现（取代 in-flight 过滤）
**决策演进**：初版取 in-flight 过滤（轻量），后经讨论认定"单 owner（复用活 TM）"才是更合理的**整体架构**
——它结构性消除根因 II（recover 混淆恢复与续跑、每次重建 TM），与事件溯源单写者/actor 范式一致，
把"至多一个调度器"从三套谓词的涌现性质变成构造性不变式。model/account 覆盖曾被误判为复用的成本，
实为 runner 因子分解问题：把**每轮执行资源改为派发时从可变 per-session 源读取**即化解（"参数是消息，
不焊进 owner"），且顺带简化重建路径。故**重实现为复用架构**。

| 改动 | 文件 | 说明 |
|---|---|---|
| run_task seam | `runtime.py _make_task_runner` | 派发时 llm 从 `session` 读、cancel/pause 从 `_cancel_tokens/_pause_tokens` dict 读（均带闭包兜底 → start/rebuild 行为不变） |
| 复用 vs 重建分流 | `runtime.py _recover_session_locked` | 冷应答 + 存活 owner 拥有该 task → `_resume_in_existing_tm`，**不重建**；否则重建 |
| 就地重驱 | `runtime.py _resume_in_existing_tm`（新） | 写回 session llm（model=会话状态）+ 重建 cancel/pause token 入 dict + 注入 wait_for_user 回复 + `tm.resume_task` + `_register_and_drain`（对同 TM 幂等） |
| 透传被应答 task | `runtime.py _resume_after_cold_hitl/recover_session` | 新增 `resumed_task_id=req.task_id` |
| TM 支持 | `task_manager.py` | 新增 `session` 属性、`resume_task()`、`is_alive()` |
| 测试 | `test_hitl_recovery.py`、`test_hitl_ask_human_cold.py` | `test_cold_answer_reuses_live_owner_instead_of_rebuilding`（rebuild_view 未调用 + 就地重派 + model 写回 session + owner 不被顶替）；fake_recover 补 resumed_task_id 断言 |

> 效果：冷 HITL 应答的常见路径不再造第二个 TM → 顶替与跨 TM 双跑从**根上**消失（单 owner）。
> 保留项：崩溃冷启动 / `/resume`（无 resumed_task_id）/ 活 TM 不含该 task 仍走**重建**路径，其
> in-flight 过滤（`running_task_ids()` 并入不派发集）作为该稀有路径的边缘防御保留。
> 取舍：run_id 复用沿用（与热恢复一致，续跑视为同一逻辑 run；重建路径仍生成新 run_id）——记录在案，
> 若 host 表现层需要每轮新 run_id 再补 seam。

## 阶段 3（统一两种 park 的投影表示）— 已实现（HITL 冷 park）
| 改动 | 文件 | 说明 |
|---|---|---|
| 冷 park 补发 TASK_SUSPENDED | `runtime.py _run_loop` HitlPark catch | 使 task 投影状态 = 内存状态(SUSPENDED)，消除"投影停 ACTIVE、与在等人脱节"漂移；parked-set 保证补发后 restore 不误重排；与委派挂起(SuspendStep)同形 |
| 测试 | `test_hitl_park.py` | `test_run_loop_catches_park_returns_suspended` 加断言：冷 park 恰发一条 TASK_SUSPENDED |

> 范围界定：只补 HITL 冷 park 路径。LLM outage 的 SUSPENDED（走 INTERRUPTED 语义）**未**补 TASK_SUSPENDED，
> 保持现状（`test_outage_resume` 断言其不发），避免混淆两种挂起语义。
> 关键澄清：reducer 的 `final_status != "SUSPENDED"` 守卫只防 SUSPENDED 污染**会话** status，不碰 task 状态；
> task 投影状态只由 `TASK_STATUS_BY_EVENT` 驱动，故补发 TASK_SUSPENDED 即修复漂移，不与该守卫冲突。

---

## 附：关键锚点索引
- 执行：`_run_task` task_manager.py:~298；`_run_loop` runtime.py:~1403；`StepDriver.run` driver.py:~213
- park：`_park_wait_for_user` act.py:~596；HitlPark catch runtime.py:~1428；`HitlManager.wait/_resolve` hitl_manager.py:180/337
- 恢复：`recover_session/_recover_session_locked` runtime.py:~895/916；`recover` runtime.py:~1205；`restore` task_manager.py:116；`_register_and_drain` runtime.py:~716
- 完成：`is_done` task_manager.py:719；`_fire_session_done/idle` task_manager.py:~642/634；`on_task_finished` task_manager.py:535
- 投影：`TASK_STATUS_BY_EVENT` events/types.py:177；session/HITL reducer control/reducers.py:~316-490
