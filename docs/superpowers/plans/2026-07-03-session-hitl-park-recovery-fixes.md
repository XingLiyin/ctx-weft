# 会话 HITL park / 恢复缠死 —— 系统性修复方案

> 日期：2026-07-03
> 触发：会话 `ses_01KWK86T03448TARB10J046D8K`（agent `agt_01KWK8DPHCY57W4JE6QQ736KV3` 所在）显示"运行中"但卡死，重启应用也不恢复。
> 状态：Layer 1（配置）已实施；其余待评审后按测试先行实施。

---

## 1. 背景与现象

一次"全面测试 agent 能力"的会话（workspace=`D:\test`，模型 deepseek-v4-flash）跑到一半卡死：

- 库里 `session.status = RUNNING`，4 个任务 `ACTIVE`，但**有 6 个 HITL 审批未应答**（5 个 `fs:bash_exec`、1 个 `wait_for_user`）。
- 前端显示"运行中"，实际不推进；重启应用也不恢复。

## 2. 根因连锁（这不是单个 bug，是一条链）

```
workspace 配错（数据在 workspace 外）
   → bash 每条都判"越界" → 每次都要 HITL 审批        … 源头（Layer 4）
   → 4 路并发 × 各自审批 → 同时堆一叠 pending HITL     … 放大器（Layer 1，已修）
   → 会话状态是单标量 → 显示 RUNNING、把审批藏起来      … 缺陷 C
   → 用户以为卡住 → 按 interrupt                       … 缺陷 B
   → ACTIVE 任务无视 park 被重派 → churn / 缠死          … 缺陷 A
   → 重启也不恢复（设计上 pending HITL 就是等应答）       … Layer C 的 UX 面
```

**证据要点（来自事件回放）**
- HITL 计数：22 次 `HitlRequired`，14 次 `HitlApproved`，2 次 `HitlAnswered`，`HitlCancelled = 0` → 6 条悬空。
- 卡审批的任务事件流是 `TaskStarted → SessionPausedHitl`，**没有 `TaskSuspended`** → 这些任务停在 `ACTIVE`，不是 `SUSPENDED`。
- 06:42:03 三条 `wait_for_user(context=interrupt)` 落在**已挂着 pending 审批**的任务（N3J9DN / 8B8ZAH / C21FRC）上 → interrupt 打在"正在等审批"的任务上。
- 07:45:48 一次冷应答后，4 任务被重派进 `reconcile` 后彻底停住；且每任务 `TaskStarted` 两遍、两个 root 任务共用同一 `run_id`。
- bash 命令几乎每条都引用 workspace 外路径（数据在 `D:\` 根、python 在 `E:\`、临时写 `D:\tmp\`），触发越界确认。

---

## 3. 修复分层总览

| 层 | 内容 | 位置 | 优先 | 状态 |
|---|---|---|---|---|
| 0 | 收尾眼前卡死会话（运维） | `/cancel ses_…D8K` | 立即 | 待执行 |
| 1 | 关闭默认并发（4→1 串行） | 配置多处 | ★★★ | **已完成** |
| 2A | restore 的 HITL-park 对 ACTIVE 任务也生效 | `task_manager.py` restore() | ★★☆ | 待做 |
| 2B1 | interrupt 在有 pending 审批时禁用 | `sessions.py` interrupt_session | ★★★ | 待做 |
| 2B2 | 打断时 cancel 底层审批（防御，可选） | `act.py` / gateway | ★☆☆ | 可选 |
| 2C | 恢复时会话状态如实反映 PAUSED_HITL | `runtime.py` recover() | ★★☆ | 待做 |
| 3 | resume 重入 / 同 agent 调度 | `task_manager.py` drain / `runtime.py` | — | **核心已修**（3ef2556 / e5b6aa0），仅留待确认项 |
| 4 | workspace 护栏（审批洪水的真源头） | `bash_policy` / 会话创建 | ★★☆ | 需讨论 |

推荐**最小实施集** = 0 + 1(已完成) + 2A + 2B1 + 2C。其余为可选/后续。

---

## Layer 0 · 收尾卡死会话（运维，非代码）

- **对象**：`ses_01KWK86T03448TARB10J046D8K`
- **做法**：对它调 `POST /sessions/{id}/cancel` → 状态转 `CANCELED`（memory 保留），并经 `_cancel_pending_hitl` 收口 6 个 pending HITL。
- **为何不选"逐个应答消化"**：在 2A/2B 未修前，应答会再次触发 churn/缠死。cancel 最干净。
- **依赖**：无。可立即执行，与代码修复独立。

---

## Layer 1 · 关闭默认并发（已完成）

把 `IPMC_TASK_MAX_CONCURRENT` 默认 **4 → 1**（串行）。串行后任意时刻最多一个任务在跑 → 最多一个待批，"同时多审批"这个放大器消失。

已改动：
- `.env:67`（本机实际生效值）
- `.env.example:87`、`packaging/default_data/.env.example:152`（模板默认）
- `src/ipmastercowork/config.py:175`（代码兜底默认）
- `README.md:541`（文档）
- `electron/lib/env-reconcile.js` → `ENV_MANAGED_OLD_DEFAULTS` 加 `IPMC_TASK_MAX_CONCURRENT: ['4']`（存量安装升级 4→1 自动迁移，用户自定义的 2/8 等保留）
- `tests/test_host_config.py:27`（断言 4→1）

验证：`uv run pytest tests/test_host_config.py`（9 passed）、`node --test electron/test/env-reconcile.test.js`（26 pass / 0 fail）。

> 说明：Layer 1 止住的是本次急性故障的**再发**。Layer 2 的三个缺陷即使串行下也可能在崩溃恢复/单发场景触发，属于独立正确性问题，仍需修。

---

## Layer 2A · restore 的 HITL-park 对 ACTIVE 任务也生效

**现象**：崩溃/冷应答恢复后，本该保持挂起等审批的任务被重新派发执行，reconcile 重发同一个待批工具 → churn。

**根因**：`src/ctx_weft/core/orchestrator/task_manager.py` 的 `restore()`（144-165 行），`parked_task_ids` 检查**只在 `if t.status == "SUSPENDED":` 分支内**（150 行）；卡审批的任务实际是 `ACTIVE`，走 `else` 分支（158-165），无条件设 `PENDING` 并入队，且重置 `retry_count`、重算 `blocked_by`——完全不看 `parked`。写代码时默认"被 park 的任务一定是 SUSPENDED"，与 HITL 等待场景（协程 `await` 中、状态仍 ACTIVE）矛盾。

> 复核：`3ef2556` 的"同 agent 不并发"调度修复**未触及 `restore()`**——缺陷 A 与 tm 调度问题相互独立，2026-07-04 重读代码确认仍存在。

**改法**：把 `parked` 检查提到状态判断之前，对任何非终态任务生效：

```python
for t in all_tasks:
    if t.status in _TERMINAL:
        continue
    if isinstance(t.settings, (CompactTaskSettings, MetadataFillerTaskSettings)):
        continue
    if t.id in parked:
        continue                      # 有未决 HITL → 不管 ACTIVE/SUSPENDED 都保持 park
    if t.status == "SUSPENDED":
        children = self._children_of.get(t.id, set())
        if all(cid in terminal_ids for cid in children):
            t.status = "PENDING"
            self._queue.push(QueueEntry(...))
    else:
        t.status = "PENDING"
        t.retry_count = 0
        blocked = {dep for dep in (t.dag_deps or []) if dep not in terminal_ids}
        self._queue.push(QueueEntry(..., blocked_by=blocked))
```

**修复后 HITL 事件走向（关键，不丢事件）**
1. 任务保持 parked；pending HITL 由 `recover_session` 的 `rebuild_pending()` 重建进内存 HitlManager（只新增、不清除），UI 照常列出。
2. 用户应答 → `_resolve` 置该请求为已解决 + emit `HitlApproved/Answered/Rejected`，冷应答触发 `recover_session`。
3. 该 HITL 已解决 → 不在 `view.pending_hitl` → task_id 不再进 `parked_task_ids` → restore() **这次**才把任务入队。
4. 任务续跑走 reconcile 重发 dangling 工具；authorizer `find_for_tool_call(tool_call_id)` 命中已解决决定（`_resolve` 把已解决请求保留在 `_requests`，`_gc_resolved` 默认留 1000 条）→ **短路复用缓存，不再新弹 HITL** → 工具按缓存决定执行/被拒 → 任务继续。

即：应答 HITL 成为任务续跑的**唯一正确触发点**。

**测试（先写失败测试）**
- `restore()` 传入一个 `status="ACTIVE"` 且 id ∈ `parked_task_ids` 的任务 → 断言它**不进队列**（当前实现会失败）。
- 回归：`SUSPENDED` 且 parked → 仍保持挂起；`SUSPENDED` 非 parked 且子任务全终态 → 仍重排；普通 `ACTIVE` 非 parked → 仍重排。

**风险**：低。仅新增"ACTIVE+parked 不入队"一条，既有路径不变。

**残留边界（非本层引入，可归 2C 加固）**：第 4 步的缓存是内存态；若进程恰在"已应答、reconcile 未跑"的窄窗口重启，`recover()` 只 `rebuild_pending` 重建 pending，已解决决定丢失 → 该工具会被**再问一次**（一次，非 churn）。加固方向：让已解决 HITL 决定也能从 `HitlApproved` 等事件重放重建进 HitlManager。

---

## Layer 2B1 · interrupt 在有 pending 审批时禁用（主）

**现象**：任务已卡在审批上时仍能按 interrupt，导致在其上叠加 `wait_for_user`、且不清旧审批 → 双重 HITL 缠死。

**根因**：`src/ipmastercowork/api/sessions.py` 的 `interrupt_session()`（约 390 行）只按**会话级** `status == "RUNNING"` 放行。多任务会话里只要有任意任务在跑（含"在途工具等审批"，`in_tool_loop=True`），会话就是 RUNNING → interrupt 被放行 → `pause_session` 让等审批的任务在 `act.py:383-391` 补"被打断"结果并 `_park_wait_for_user`，而底层审批从不 cancel（全程 `HitlCancelled=0`）。

**改法**：pause 前先查 pending 审批，有则拒绝/no-op。

```python
if entry.status == "RUNNING":
    hitl = deps.get_hitl_manager()
    if hitl is not None and hitl.list_pending(session_id=session_id):
        raise HTTPException(
            status_code=409,
            detail="存在未处理的审批，请先处理审批，或用 /cancel 终止会话",
        )
    runtime.pause_session(session_id)
```

前端配合：会话有 pending 审批时把"打断"按钮置灰并提示。

**测试**
- 有 pending HITL 的 RUNNING 会话发 interrupt → 断言 409、且 `pause_session` 未被调用。
- 无 pending 的 RUNNING 会话发 interrupt → 仍正常 pause。
- 非 RUNNING（PAUSED/PAUSED_HITL/INTERRUPTED）→ 仍 no-op（保持现有语义，见 sessions.py:400-404 注释的不变式）。

**风险**：低。代价是"真在跑的别的任务也无法打断"——串行前提下基本无实害；要放弃走 `/cancel`。

---

## Layer 2B2 · 打断时 cancel 底层审批（防御，可选）

**思路**：interrupt 取消"正在等审批的在途工具"时，把该 tool_call 的底层审批 HITL 也 `cancel`（emit `HitlCancelled`），再决定是否叠加 wait_for_user，避免双重 HITL。

**注意**：`sessions.py:402-404` 有既有不变式——"interrupt 不得 cancel pending HITL（软暂停的 wait_for_user 是任务唯一续跑入口）"。本层针对的是**另一种 HITL（底层 approval）**，需在实现上严格区分 `wait_for_user` 与 `approval`，只 cancel 后者。有了 2B1 堵住入口后，本层实害基本消失，故列为**可选**。

**测试**：模拟"任务等审批中被打断" → 断言底层 approval 被 `HitlCancelled`、不产生悬空 pending。

**风险**：中。触碰 act 打断路径与既有不变式，需谨慎；建议在 2B1 之后单独评估。

---

## Layer 2C · 恢复时会话状态如实反映 PAUSED_HITL

**现象**：崩溃/卡死后前端显示 `RUNNING`，其实在等审批 → "看着在转却不动"。

**根因**：会话级 `session_status` 是单标量（`ctx-weft/.../control/reducers.py` 约 318-490），被 `RunStarted/TaskStarted/HitlApproved` 等事件来回刷成 RUNNING；且 `runtime.py` 的 `recover()`（约 1180-1211）在**有 pending HITL 时只 `rebuild_hitl`、不 emit 任何状态事件**，投影停在崩溃前的 RUNNING。

**改法（最小侵入）**：`recover()` 里当某会话有 pending HITL（`rebuild_hitl` 返回 >0）时，emit `SessionStatusChanged(PAUSED_HITL)`，让投影/SSE 如实显示"等待审批"。

```python
n = await self.rebuild_hitl(session_id)
if n:
    await self._emit_session_status(session_id, "PAUSED_HITL")  # 复用 _emit_session_interrupted 的形态
    logger.info("Recovery: session %s → PAUSED_HITL (%d pending)", session_id, n)
else:
    await self._emit_session_interrupted(session_id)
```

**测试**：recover 一个带 pending HITL 的会话 → 断言投影 `session_status == "PAUSED_HITL"`（当前会停在 RUNNING）。

**中期可选（不在最小集）**：把 `session_status` 改为派生——只要有 pending HITL，就不让 task 事件把它刷回 RUNNING。侵入较大，本次先用最小侵入版。

**风险**：低。只多发一条状态事件。

---

## Layer 3 · resume 重入 / 同 agent 调度（核心已修）

**已修复（提交在册）**
- `3ef2556 修复task并发时的一些调度问题`：`drain()` 增加"同 agent 不并发"——用 `busy_agents` + `skip=` 跳过"目标 agent 已有任务在跑"的队列条目；`_effective_agent()`（`task_manager.py:247-262`）为串行键：subagent 各自独立 agent（跨 subagent 并行保留），root task + 普通非 subagent 子任务回退 `session.root_agent_id` → 同键串行。
- `e5b6aa0 被顶替的旧 TaskManager 迟到收尾不再冲掉新一轮会话`：`_fire_session_done` 在 gather 后用 `_is_current` 判定，被顶替的旧 TM 收尾变 no-op，不发 SessionFinished、不冲新一轮状态。

这两处覆盖了本次事件里"root agent 同时跑两个任务、共用同一 run_id"的异常。

**副作用（值得记住）**：有了"同 agent 不并发"，即使 `max_concurrent>1`，同一 agent 的任务也自动串行，只有**跨 subagent**才真并行。故 Layer 1（默认串行）现在是"再收紧一档"，非唯一防线。

**resume 重入锁（已实现，2026-07-04）**
之前的残留——`recover_session` 无重入锁，两次重叠的 recover 会有两套 drain 竞争派发同一批任务——已加固：
- `runtime.py`：新增 per-session `asyncio.Lock`（`self._resume_locks`）；`recover_session` 变薄壳，持锁调用 `_recover_session_locked`（原 body 原样搬入），使整段 rebuild+register 原子化，同一 session 的重叠 resume 串行执行。
- `task_manager.py`：`drain()` 循环开头加 `_is_current` 守卫——被新 TM 顶替（`_task_managers` 映射被覆盖）后，旧 drain 立即停止派发，无声（不发事件、不改状态）；在跑协程仍靠 `_fire_session_done` 处的 `_is_current` 收敛。
- 测试（`tests/unit/test_superseded_task_manager.py`）：`test_superseded_tm_drain_does_not_dispatch`、`test_current_tm_drain_dispatches`、`test_recover_session_serialized_per_session`（峰值并发=1）、`test_recover_session_different_sessions_not_serialized`（不同 session 仍并发）。

**残留（更窄，已知）**：`_is_current` 守卫拦住旧 TM 的**后续派发**，但一个已 create_task 出去、正在跑的 `_run_task` 协程不会被强制取消——它会跑完当前这一步再由收尾守卫收敛。彻底取消在跑协程需额外 plumbing、且有中断恢复中任务的风险，暂不做。

---

## Layer 4 · workspace 护栏（审批洪水真源头，需讨论）

本次审批爆炸的根因不是缺陷，而是**数据放在了 agent workspace 之外**（workspace=`D:\test`，数据在 `D:\`根、python 在 `E:\`、临时写 `D:\tmp\`），触发 `bash_policy.classify()` 的越界确认（`src/ipmastercowork/auth/bash_policy.py`）。串行后"每条命令都要审批"仍在。备选（要设计判断）：

- (a) 会话开始/运行时检测"命令引用路径持续落 workspace 外" → 给出提示（"把数据放进 workspace 就不用逐条审批"）。
- (b) 对齐给 agent 的 workspace 提示，消除 `/workspace` 与实际 `D:\test` 的错位。
- (c) 重评 auto 模式越界策略粒度（如只读越界更偏 ALLOW）。

**优先级**：中。建议单独立项讨论，不并入本次代码修复。

---

## 4. 实施顺序与依赖

1. **Layer 0**（cancel 卡死会话）——随时，独立。
2. **Layer 1**——已完成。
3. **Layer 2A + 2C**——一起做：都属"恢复正确性"，2A 保证不误重派、2C 保证状态如实；无相互依赖，可并行提交但建议同一批测试覆盖。
4. **Layer 2B1**——独立，随时可做。
5. **Layer 3**——可与 2A 顺带。
6. **Layer 2B2 / Layer 4**——评审后再定，单独立项。

全部按 `superpowers:systematic-debugging` Phase 4 与 `superpowers:test-driven-development`：**先写复现失败的测试，再改，最后回归。**

## 5. 验收标准（最小集完成时）

- restore 一个 ACTIVE+parked 任务不入队（2A）；HITL 应答后能干净续跑、不重复弹审批（2A 集成）。
- 有 pending 审批时 interrupt 返回 409、不 pause（2B1）。
- 带 pending HITL 的会话 recover 后投影为 PAUSED_HITL（2C）。
- 全量：`uv run pytest`（core + host）绿；`node --test electron/test/*.test.js` 绿。

## 6. 关联记忆 / 规格

- `workspace-reregister-on-resume`（resume 须先注册 workspace）
- `env-reconcile-on-upgrade`（Layer 1 的 managed/oldDefaults 机制）
- spec/07 §6/§9（HITL 冷分流、reconcile 决定缓存、park 恢复语义）
