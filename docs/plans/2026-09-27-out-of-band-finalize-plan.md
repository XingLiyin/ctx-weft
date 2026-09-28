# 带外 finalize，以及 finish_task 也交给 observer 判定

> 状态：设计，未实施。
> 前作：[2026-09-22-plain-text-observe-park-plan.md](./2026-09-22-plain-text-observe-park-plan.md)
> 与它的 [实施计划](./2026-09-22-plain-text-observe-park-impl.md)。

## 0. 两件事，前者是 bug，后者是需求

1. **S5/S6 之后 park→success 走带外判决终结 task，而 park 会整步跳过 `FinalizeStep`。**
   park 在 `ActStep` 里抛 `HitlPark`，driver 根本走不到 observe / finalize；带外那条
   （`_decide_and_write` → `TaskFinished` → `_settle` → `on_task_finished`）只做队列 / DAG /
   failure_counter 记账。而 `finalize.py` 是四件事的**唯一**发生地。
2. **root 的 `finish_task` 仍走前台机械判决**：`_should_use_llm` 对 `parent_task_id is None`
   降级，`_mechanical_verdict` 把 `actor_done` 无条件映射成 `success`，护栏
   （success-without-outputs）长在 `report_task_outcome` 里、不在这条路上。于是 root 上
   `finish_task` 是自证，成了纯文本被纳入判定之后**唯一不被复核的出口**。

2 的落地前提是 1：`finish_task` 恰恰是交付链最重的那条出口（`deliverables_summary` /
finish 对 / blackboard / parent bubble 全为它而设），不先补带外 finalize 就把它挪到 park，
等于静默掐断交付链。

2026-09-27 裁定：**A 路线（park + 后台判定），S-a 单独排一步先修。**

---

## S-a · 带外 finalize

### S-a.1 丢的是什么

| 丢掉的 | 后果 |
|---|---|
| `finalize_task_memory` → `_close_one` | 跨 agent bubble 不给 parent、同 agent 的派发 ack 停在 running、`build_finish_slots` 的 finish 对不写、末段 raw 不折 |
| `BLACKBOARD_PUBLISHED`（topic=task.id） | parent `recall_topic(task.id)` 读不到子任务结果 |
| `TASK_FINALIZED` | host 的 `tasks.outputs_json` / `error` 两列恒空（`projection_updater.py` 的 TASK_FINALIZED 支） |

root 的 plain_text 漏得轻（没有 parent 可 bubble、答复已流式给用户看过），只是
`outputs_json` 从 S5 起一直空。**S6 把 park 推广到子任务之后就不轻了**：一个 park 中被判
success 的子任务，parent 醒来只看到「派发框 + 停在 running 的 ack」，拿不到任何产出——
正是 `_dispatch_ack` docstring 里描述的那个形态。

### S-a.2 插在状态转移的哪一侧：三段顺序

`apply_out_of_band_verdict` 今天已经是「锁内仲裁 + 写状态 → 锁外发事件 + `_settle`」。
finalize 插在这两段**中间**：

```
async with self._lock:          ① 仲裁（只接受 AWAITING_HUMAN）+ 写 observer 字段
                                  + _decide_and_write（task.status = FINISHED）
                                ② finalize 副作用（memory 写 + 三条事件）   ← 新增
await self._emit(TaskFinished)  ③ 发状态事件
await self._settle(...)            + 队列收尾（→ on_task_finished → _try_resume_parent）
```

三条约束各自钉住一侧，顺序不能动：

- **finalize 必须在仲裁之后**。判决被拒（人先开口）时一个字都不能写——bubble 出去了、
  自身对话被软删了，而 task 还要继续跑。
- **finalize 必须在 `_settle` 之前**。`_settle` → `on_task_finished` → `_try_resume_parent`
  会唤醒 parent，parent 不能在子任务 bubble 落地之前醒过来装配。
- **状态写留在锁内**，这是竞态的关门点。`task.status = FINISHED` 一落，人的消息在
  `send_message` 层就按 `_task_is_terminal` 走 `_start_task_for_agent` 新建分支，不会再
  `requeue_for_message` 这个正在 finalize 的 task。所以 finalize 那一段（有 IO、不能进锁）
  跑在锁外是安全的。

### S-a.3 形状：回调参数，不是 runtime 钩子

`apply_out_of_band_verdict(..., finalize: Callable[[], Awaitable[None]] | None = None)`，
闭包由 `background_observe._submit_verdict` 传进去。TaskManager 只管在 ② 那个位置调它，
不碰 memory——「task 状态的唯一改写者」的地位不变，新增的只是一个回调点。

**不走 runtime 钩子**（`cancel_finalizer` / `on_task_terminal` 那种）的理由：`_close_one`
与 `_is_short_leaf` 要 `state.scope` / `state.agent.loop_config` / `ctx.llm.tokenizer` /
`ctx.provider_ctx`，后台 observe 手里全有；runtime 钩子只拿得到 `task` + `session`，得再
手写一份镜像分支——`synthesize_cancel_closure` 就是那样来的，它的 docstring 自己承认在
镜像 `_close_one`，两份必然漂。

### S-a.4 不需要改 Task schema

`state.extra["final_body"]` / `["final_summary"]` 在 park **之前**就写好了
（`_finish_plain_text_turn` 先 `_synthesize_final_outputs` 再 `_park_await_user`），而
`launch_background_observe` 的 `dataclasses.replace(state, ...)` 原样带上同一个 `extra`
字典。`_readonly_ctx` 是整份 ctx 的 replace，`memory` / `llm` / `event_bus` /
`provider_ctx` / `task_manager` 全在，只有 `provider_ctx.extra["control_readonly"]` 不同
（那面旗只 gate 控制工具，不 gate `ctx.memory` 直写）。

`act_recap` / `task_summary` 也不必从 task 上回读：`_submit_verdict` 手里的 `meta` 就是
observer 刚回传的那一份。

### S-a.5 抽哪一段

从 `FinalizeStep.execute` 抽出 success 分支要的四件事，两个调用方共用（不抽 retry /
retry_exhausted 两支——带外只在 success 时调）：

```
apply_task_close(state, task, ctx, *, outcome, act_recap, task_summary,
                 final_body, final_summary, has_llm_summary) -> list[Event]
  ├─ mem_content = _build_memory_content(task.outputs, task_summary or act_recap)
  ├─ finalize_task_memory(...)          # close / bubble / finish 对 / 末段 raw
  ├─ BLACKBOARD_PUBLISHED               # success only
  └─ TASK_FINALIZED                     # outputs.output / .summary 分开出核
```

`has_llm_summary=True`：后台 observer 就是 LLM，`act_recap` 是真摘要，close 即折末段
raw，不走「占位 + `_replace_finish_report` 替换」那条为「finalize 先跑、bg 后到」设计的
延迟路径——带外路径顺序正好反过来，真摘要已在手。

**要验的两点**：
- `plain_text` 边界的 `segment_fold` 已经折过一次，其后 `_supersede_final_raw_segment`
  是否 no-op（预期是，但要钉住）。
- `_close_one` 的注释说终态 finalize 天然单入口、不需要幂等守卫。带外路径下这条仍然
  成立（仲裁保证至多一次），但要在测试里钉死。

### S-a.6 失败处理

finalize 抛异常 → 记日志、**继续**发 `TaskFinished` + `_settle`。不能因此吞掉状态转移：
`task.status` 已经是 FINISHED，不发事件 host 永远不知道、`_settle` 不跑则槽位永不释放、
单交互线永久占着。降级 = 该 task 缺 bubble / blackboard，状态机不卡。与 `on_task_terminal`
钩子的 best-effort 口径一致。

### S-a.7 悬空 park 气泡（✅ 已落地 `8bc9b65`）

与 S-a 无因果关系，但同属「park→success 留了尾巴」这一类。2026-09-27 逐环验完。

**链条**：带外判 success → `TaskFinished`（`_emit` 回落 `task.assigned_agent_id`，带的是**子**
agent 的 id）→ ALM `SETTLED` → 子 agent `waiting_human` → `idle`，而 wait 气泡按 `faedd25`
留着 → host 的 `_addressable_agent_id` 过滤 `waiting_human` 找不到它 → 回落 root →
`_start_task_for_agent(root)` 的 `_cancel_pending_hitl_of` 第一行就是
`if v.agent_id != agent_id: continue`，**收不掉子 agent 名下那个气泡**。没有别的收口点：
`on_task_terminal` → `close_resolved` 只盖**已终局**的（遍历 `resolved_for_session`）。

**后果（成立的那一条）**：`SessionStatusFold.status` 的优先级是
`running > pending_hitl > interrupted > terminal`，悬空气泡让 `_terminal` 永远浮不出来。
于是父任务 `finish_task` 收尾、root task FINISHED 之后，会话**永久停在 PAUSED**——SSE
的「终态即刻收口本轮流」不触发，投影里 `sessions.status` 一直 PAUSED，重启后还被
`list_active_session_ids` 当活会话捞回来。每个 park 后被判 success 的子任务留一个，会累积。
（`_terminal` 只认 root agent 的 task 终态，所以子任务的 `TaskFinished` 连它都不写——这个
气泡是它留下的唯一痕迹。）

**不成立的两条**（查完推翻，别再顺着它们想）：
- 重启后被 `resume_agent` 的 `_pause_bubble_of` 误当暂停气泡放行冷续跑——两道门都挡住：
  该函数按 `delivery.preface in (AFTER_INTERRUPT, AFTER_INTERRUPT_EDIT)` 过滤而 park 用
  `PREFACE_NORMAL`；`resume_agent` 还要求 agent 是 `waiting_human` 而它已是 idle。
- 「消息投错 agent」是当前的可见故障——大半被 host 的 409 闸门挡住：判 success 后
  `_settle` → `_try_resume_parent` 立刻唤醒 parent → RUNNING → 拒消息。挤进那一瞬也不丢
  （投 root → 父任务无活子任务 → `requeue_for_message` 连带重排）。且 `text_delta` 帧不带
  `agent_id`、不按 agent 过滤，用户看不出刚才是谁说的。**是隐患，不是故障。**

**触发面**：**只有**带外判 success 这一条路会让 agent 在气泡还挂着时离开
`waiting_human`。其余全都不泄漏——retry/fail 维持 `AWAITING_HUMAN`（agent 仍
`waiting_human`）；暂停键 park 由 `resume_agent` 经 `_pause_bubble_of` 收；cancel / purge /
熔断由 `_cancel_session_hitl` 覆盖；重启 `/resume` 不匹配 `_pause_bubble_of`（park 用
`PREFACE_NORMAL`）但 agent 保持 `waiting_human`，下一条消息就收掉它。

**修法（2026-09-27 定）：复活 `2ee524b` 的 `_close_park_bubble`，加一道门。**

那个实现本身是对的——按 **task** 过滤（不按 agent：同一 agent 上可能还挂着子任务的
`ask_user`）、只对 `success`、仲裁被拒不收、best-effort 不抛。`faedd25` 删它给的两个理由
只有一个站得住：

- 「`2ee524b` 的诊断不成立」——**对**。它归因于「前端把输入当成对气泡的应答投过来」，
  真因是 host 的 `GET /hitl/pending` 没滤 `form=wait`，已由 `7e99c00` 修在正确的地方。
- 「气泡一收，SUCCEEDED 当场浮出来」——**只对 root 成立**。`SessionStatusFold` 折终态时
  有一道门：`if self.root_agent_id and agent_id != self.root_agent_id: return False`，
  **子任务的 `TaskFinished` 压根不写 `_terminal`**。收掉子任务的气泡之后 `_pending_hitl`
  空、无 running、`_terminal` 仍是 None（本轮 `AGENT_RUNNING` 已清）→ 落到兜底
  `return "RUNNING"`，而此刻 `_try_resume_parent` 正要唤醒 parent。不会跳成已完成。

所以那次回滚是「用一个只对 root 成立的理由，删掉了一个对子任务正确的修复」。

**门的判据：`task.parent_task_id is not None`**，语义是「这条线判 success 之后交回给谁
了」。子任务 → 交回 parent，没人会再来答那个气泡 → 收。`parent_task_id is None`（会话
root、每条用户消息新开的 task）→ 没有别的线接管，用户下一句还是给它 → 留（`faedd25` 要保
的正是这个）。

⚠️ **不要写 `_is_own_root`**：它对跨 agent 子任务（parent 非空、creator≠assigned）返回
True，而那种任务的线同样交回 parent，会被漏掉。这个仓里 root 判定习惯性写 `_is_own_root`,
这里是那个习惯会出错的地方。

**插入点**：与 S-a 的 finalize 钩子共用——`apply_out_of_band_verdict` 里「锁内仲裁 + 写
状态」之后、「发 `TaskFinished` + `_settle`」之前。`2ee524b` 那版放在 `_submit_verdict`
末尾（`_settle` 之后），中间有一段「气泡还在、无 agent 在跑」的窗口会折出一帧多余的
PAUSED，挪到前面顺手消掉。

**剩下一格由显式寻址兜住**（见 S-0）：`parent_task_id is None` 但不在 root agent 上——
用户显式对某个子 agent 说话开出的 task，park 后判 success，气泡按上面的门留着，而 host
今天的扫描会回落 root。显式寻址之后前端仍寻址那个子 agent，它的气泡在下一条消息时被收。
两个修法正交且互补，合起来没有残留泄漏。

---

## S-0 · 显式寻址：park 信号的 agent_id 不许在 host 侧丢掉

> ✅ 已落地：core 侧 `8bc9b65`（S-a.7）、host 侧 `NetliveCoworkPy@34e0887` +
> vendor 同步 `60462dd`。**实际做了四处**，第四处见本节末尾。

2026-09-27 定。**core 侧已经满足**：`HitlService.open` 把 `agent_id` 登记进 `PendingHitl`，
`HITL_OPENED` 出核时信封（`_emit(agent_id=req.agent_id)`）与 payload 各带一份，
`HitlRequestView.agent_id` 也在。断链全在 host——它在三个出口把这个已声明的事实丢掉，
然后在投递入口用扫描猜回来：

| host 出口 | 对 park 气泡做了什么 |
|---|---|
| SSE `translate_event` 的 `HITL_OPENED` 支 | `if form == HITL_FORM_WAIT: return None`，整条帧不发 |
| `GET /hitl/pending` | `form=wait` 被滤掉（`7e99c00`） |
| `SessionStatusFold` | 只留 `hitl_id → delivery_kind`，**agent_id 丢弃** |
| `SendMessageRequest` | 没有 `agent_id` 字段 → `_addressable_agent_id` 扫 `list_agents` 猜 |

前作设计文档 §6.4.1 当初判「`agent_id` 可选、不是必需——单线之下 host 自己能解析出唯一
那个」。**这个判断错了**：`faedd25` 让气泡活得比 task 久之后，「唯一那个」不再良定义——
单交互线不变量是按 task 定的（running 或 `AWAITING_HUMAN`），气泡在 task 终结后继续存在，
于是两个气泡可以共存而只有一个 task 在 `AWAITING_HUMAN`。

三处改动，全在 host：

1. `SessionStatusFold._pending_hitl` 从 `hitl_id → kind` 改成 `hitl_id → (kind, agent_id)`，
   暴露 `waiting_agent_id`。
2. 载体用 `session_update` 帧（park 刻意不发 `waiting_input`，这是对的），它本来就在说
   PAUSED，多带一个「在等你说话的是谁」；`GET /sessions/{id}` 同样带，供刷新后恢复。
3. `SendMessageRequest` 加 `agent_id`，前端原样回传，host 直接
   `runtime.send_message(agent_id=...)`。给了就用；没给（老客户端）才回落现有扫描。

`send_message` 那一侧不动——它本来就是 agent 寻址的，`agent_id` 全局唯一、路由只看它。

**第四处（实施中发现，必须一起改）**：`sessions.send_message` 末尾那句
`entry.root_agent_id = handle.agent_id`。这一轮可能跑在某个 park 中的子 agent 上（自
`0d35668` 起就可能），而 `root_agent_id` 的 setter 会把值喂给 `SessionStatusFold`，终态那
一档只认 root 的 task——写进一个子 agent 就等于让它的子任务终结冒充整轮结束，会话会在子任务
刚跑完的那一刻显示「已完成」。那句的本意（冷 entry 解析出 root 后回填）`_root_agent_id`
自己就做了，所以它在 root 的情形下多余、在子 agent 的情形下有害。改成只在「还不知道 root
是谁」时补一次。显式寻址会放大这个 bug（更多轮次落在子 agent 上），所以同一步修掉。

---

## S-b · act 分流：`actor_done` 并进 park 分支

`actor_done` 且 `suspend_requested` 为 False 且非 unattended → 走 park，不再去前台 observe。
delegate 那条（`suspend_requested`）仍去 suspend。unattended 照旧前台（没人可等）。
`_synthesize_final_outputs` 对 `normal` / `actor_done` 本来就都跑，不用动。

**新 boundary `finish_park`**：`_judges` 返回 True，但**不进** `_CLOSE_BOUNDARIES`。不复用
`finish`——那会触发占位 close-report 那套（`pop_close_report` / `register_close_synth`），
而带外路径不需要它。`_BACKGROUND_BOUNDARY_DESC` 补一条状态描述。

**先只对 root 生效**（`parent_task_id is None`），与 S5 同一个收窄办法：子任务的
`finish_task` 今天走前台 LLM observe（有真复核 + finalize 照常），先别动。S-a 落地并观察
一段之后再决定要不要推广。

## S-c · 装配上一处真差异

`plain_text` 刻意**不注入** `## Final output`（那段文本本身就是 assistant 回合、在重建
对话里看得见），但 `finish_task` 是 SILENT 工具、产出不写任务层对话——`finish_park`
**必须注入**，否则 observer 会虚构完成叙述。理由见 `_finish_result_section` 的 docstring。
S2 合并装配路径时把这个差异记成「前台 vs 后台」，现在要改成按 boundary 分。

---

## 代价

- **LLM 调用次数不变**：root 的 `finish_task` 今天已经跑一次后台 observe
  （`boundary="finish"` 产摘要 + `_close_report`），改后是同一次调用换 boundary、兼产
  verdict。与 S5 同样划算。
- **会话不再自动跳「已完成」**（已确认接受）。今天 `finish_task` 立刻 `TaskFinished` →
  host `SUCCEEDED`；改后先 PAUSED（park 气泡），判完 task 终结但气泡留着 → 仍是 PAUSED。
  agent 说完「我做完了」之后界面停在「等你说话」，直到用户真的开口。
- root 的 DAG 后继放行从同步变异步（root 通常没有后继）。
- `finish 对` 的写法从「占位 + 替换」变成一次写全（S-a.5），两条路径都要有测试。

## 不做的事

- **不给 root 开前台 LLM observe**（前作 impl 计划的「范围外」那条，本次编号 F）。它更便宜
  （一行判据）但要付一次前台 LLM 往返，且会与 close 边界那次后台 observe 重复。A 路线
  LLM 次数不变，这是它胜出的理由。
- **不新增「待判决」task 状态**。带外入口只认 `AWAITING_HUMAN`，而 park 已经提供了这个
  持有态；新增一个中间态要动 `TaskStatus` 值域、`_PARKED_STATUSES`、reducers、投影、host
  的 `SessionStatusFold`、`_interactive_line_held`、restore，不值。
- **不做「先终结、后台判 retry 再翻案」**：`TaskFinished` 已发、DAG 后继已放行、host 已
  SUCCEEDED、blackboard 已发布，翻不回来。
