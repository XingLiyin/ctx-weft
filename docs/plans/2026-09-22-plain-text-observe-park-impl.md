# 纯文本回合交给 observer 判定 —— 实施计划

> 配套设计文档：[2026-09-22-plain-text-observe-park-plan.md](./2026-09-22-plain-text-observe-park-plan.md)。
> 本文只讲**怎么落**，不重复论证；每条结论的理由在设计文档里。

## 0. 总原则

**S1–S4 只加不改**（落地后行为逐字不变，可以单独合入、单独放着），**S5 是第一个
行为切换点**（先只对 root task 生效），**S6 把切换扩到子任务**，S7–S8 清理收尾。

这样切的好处：任何一步出问题都能单独回滚，而且 S5 落地后有一段只影响 root task 的
观察期——那是范围最小、最好观察的一格。

| 步 | 内容 | 行为变化 | 依赖 |
|---|---|---|---|
| S1 | drain 单交互线闸门 | 并发收紧（非 unattended） | — |
| S2 | 装配路径合并 | 无（纯重构） | — |
| S3 | terminal tool 统一 | 无 | S2 |
| S4 | 带外判决入口 + 段界水位线 + 拆掉三处 await | 无（暂无调用方） | — |
| S5 | 后台 observe 产 verdict | **root task 开始被判定终结** | S3 S4 |
| S6 | act 分流切换 | **子任务开始 park** | S1 S5 |
| S7 | 删 `interaction_mode` | 无（此时已无人读） | S6 |
| S8 | 收尾 | host 可寻址 agent、hint 一次性 | S6 |

---

## S1 · drain 单交互线闸门

**改**：`core/orchestrator/task/manager.py` 的 `drain()`。

`skip` lambda 里已有「同 agent 不并发」的谓词，再加一个同族的：

```
非 unattended 的 entry，若该 session 已有一个非 unattended 的 task
处于「在跑 或 PAUSED / AWAITING_HUMAN」→ skip
```

`_tasks` 全量在手，查得到 park 中的 task，不需要新状态。

**为什么判据不能是「在跑」**：`_settle` 对 `_PARKED_STATUSES` 先 `_release_slot` 再
立刻 `drain()`，槽位一空下一个就被派发。详见设计文档 §3.7。

**验证**
- 新单测：两个非 unattended task 入队 → 只有一个被派发；第一个终结后第二个才跑。
- 新单测：一个非 unattended task park 住 → 第二个仍不被派发（这条是判据的要害）。
- 新单测：unattended task 不受限，照 `max_concurrent` 并行。
- 回归：现有测试里若有「多个非 unattended 子任务并行」的断言，会在这里挂——**那是
  预期的**，改成串行断言。

**风险**：这一步独自落地就会让有人会话里的 DAG 变串行（设计文档 §3.7 代价一，已确认
接受）。它不依赖后面任何一步，但后面每一步都依赖它。

---

## S2 · 装配路径合并

**改**：`core/assembler/composer.py`。

把 `_build_observer_messages` 与 `_build_background_observe_messages` 合成一个参数化
函数。三处差异参数化（设计文档 §3.3.3）：

| 参数 | 前台传 | 后台传（本步） |
|---|---|---|
| `judge: bool` | `True` | `False` |
| `subtasks` | `request.extra["subtasks"]` | `[]` |
| `pre_cue_sections` | `## Final output`（outputs 非空即注入） | 仅 close 边界注入 |

`judge` 决定用哪条 cue：`True` → 前台那条判定提示；`False` → `_background_observe_cue`
（那句 "Just summarize — do not judge success/retry/fail" 留在这一支里，本步不动）。

**本步行为必须逐字不变**：两个 purpose 各自传入今天的值，产物一模一样。

**验证**
- 现有 composer 测试全绿（`test_composer_*`、`test_current_message_framing*`）。
- 新增对照测试：同一份 state，重构前后两个 purpose 的 `system` + `messages` 逐字相等。
  （实现时先把重构前的产物 dump 成 golden，再比对。）

---

## S3 · terminal tool 统一

**改**：`core/capabilities/control_tools.py`、`core/loop/steps/background_observe.py`。

1. 后台 observe 的 `terminal_tool_name` 从 `BACKGROUND_PROCESS_REPORT_NAME` 换成
   `REPORT_TASK_OUTCOME_NAME`。
2. **按 boundary 决定消不消费 `task_status`**（设计文档 §3.3.2）——本步一律**不**消费，
   行为因此不变：

   | boundary | 本步消费 verdict? | S5 之后 |
   |---|---|---|
   | `plain_text` | 否 | **是** |
   | `mechanical` / `finish` / `normal` / `interrupt` / `dispatch` | 否 | 否 |

3. 删 `collect_process_report`（签名是 `report_task_outcome` 的真子集）。

**一个要当心的地方**：`report_task_outcome` 的实现会直接写 `ctx.task` 的
`process_report` / `next_step_hint` / `observer_outcome` / `actor_done`。后台 observe
跑在 `launch_background_observe` 快照出来的 state 上，且常在主 run 的
`finally evict(agent.id)` **之后**才真正执行——**必须确认这些写落在快照的 task 副本上、
不会污染主线程那个 task**。若是同一对象引用，本步就要先把写入隔离掉（例如后台路径
传一个 `write_through=False` 的 ctx），否则 S3 表面行为不变、实际已经在偷偷改 task。

**验证**
- 现有 `test_background_observe_wiring.py` 全绿。
- 新单测：后台 observe 跑完后，主线程 task 的 `observer_outcome` / `next_step_hint`
  未被改写。
- 全局 grep 确认 `collect_process_report` 无残留引用。

---

## S4 · 带外判决入口

**改**：`core/orchestrator/task/manager.py`（新增入口）、`core/hitl/service.py`（resolve）。

新增一条「不属于任何活跃 run 的判决」提交路径：

```
后台 observe 完成 → 构造 RunOutcome(kind=COMPLETED, verdict=...) → TaskManager 处置
```

- `disposition_for` **一行不改**：判 success 走已有的 `COMPLETED + verdict="success"` 分支。
- TaskManager 作为「task 状态的唯一改写者」的地位不变，新增的只是它的一个调用方。
- task 终结时 **resolve 掉那个未决的 `wait_for_user` HITL 请求**，否则悬空。

### S4.1 · 仲裁必须在 TaskManager 层做，不能靠那个 await 兜底

`_run_loop` 入口的 `await_pending_background_observe`（两个轴、`asyncio.shield`）保证的
是**新 run 不与后台 observe 并发跑**，**不**保证「USER_PROMPT 的写入」与「判决落库」
有序。人回复的实际时序是：

```
send_message → _inject_user_turn        ← 写 USER_PROMPT，不等后台 observe
             → requeue_for_message       ← 重排
             → 新 run → _run_loop 入口    ← 到这里才等
```

消息早就进去了。所以带外判决到达时**必须自己检查 task 是否还是 PAUSED**，不是就拒绝。
S4.3 拆掉那几处 await 之后这条更要紧——它是唯一的仲裁。检查与状态转移必须在
TaskManager 的锁内**原子**完成，否则「检查时还是 PAUSED、转移时已被重排」的窗口依然在。

**验证**
- 新单测：直接调这个入口 → task 从 PAUSED 转 FINISHED、DAG 后继入队、wait HITL 被 resolve。
- 新单测：task 已被人先开口重排（不再 PAUSED）→ 判决被拒绝，状态不变。
- 新单测：wait HITL 的 resolve 是幂等的（重复提交不炸）。

### S4.2 · 段界水位线：让迟到的折叠自己落回原位，而不是靠等

**这一步取代了「给 `_inject_user_turn` 补屏障」的做法**（用户裁定 2026-09-22：除非是
「不确定要不要建新 task 框」这类非等不可的地方，不要拖慢回复速度）。

**问题不在时间戳。** `segment_fold` 的锚点逻辑早就不用 `now()`：

```python
if following:
    summary_ts = following[0].timestamp - timedelta(microseconds=1)
else:
    summary_ts = to_archive[-1].timestamp   # 段尾：锚被折段末条位置，不用 now()
```

迟到的摘要本来就会被放回它该在的位置。

**问题在段界是动态查找的**：

```python
boundary_idx = 最后一条 role=user 回合
pool = view[boundary_idx + 1:]
```

新 USER_PROMPT 一写进去，它就成了「最后一条 user 回合」，于是 `pool` 空 → `to_archive`
空 → 落到 `else: summary_ts = now_utc()`（摘要排末尾）→ `memory.fold([], [summary])`
（**上一段 raw 一条都没折**）。锚点逻辑根本没机会生效。这才是
`_write_hitl_reply_turn` 那条屏障真正在挡的东西。

**改**：`launch_background_observe` 快照 state 时（它已经在铸独立 `run_id` /
`sequence_counter`），把**当时的段界**一并钉住——最后一条 user 回合的 record id，或
`(timestamp, seq_no)` 水位线。`segment_fold` 按水位线算 pool，不再动态查找。

于是：要折的仍是正确的那一段；新 USER_PROMPT 落在水位线之后、进 pool，但 `_protected`
把它排除出 `to_archive`；`following[0]` 正是那条新 USER_PROMPT，`summary_ts = 它 - 1μs`，
摘要精确落回原位。**两条注入路径都不用等**，`_write_hitl_reply_turn` 里那道现有屏障
也可以一并去掉。

**这个改造解决不了的一件事**（接受）：`_run_loop` 的 **agent 轴** await 另有理由——新
task 首次装配经 `recall_recent_by_agent`（过滤 `not is_superseded`）会读到上一轮尚未被
supersede 的 raw，拿原文而非胶囊，prompt 白胀一轮。fold 还没发生，水位线救不了。但这是
**性能降级不是正确性问题**，那个 await 留着当性能优化即可。

**验证**
- 新单测：段界水位线钉住后，中途插入一条 USER_PROMPT，`to_archive` 仍是原来那段、
  `summary_ts` 落在新 USER_PROMPT 之前 1μs。
- 新 e2e：park → 后台 observe 仍在飞时人发消息 → 上一段被正确折叠、下一轮装配不误判续跑。
- 回归：`_write_hitl_reply_turn` 去掉屏障后，HITL 应答路径的既有 e2e 仍绿。

### S4.3 · 拆掉三处 `await_pending_background_observe`

段界水位线落地后，这三处等待各自的理由还剩什么：

| await 点 | 原理由 | 水位线之后还剩 | 拆掉的代价 |
|---|---|---|---|
| `_write_hitl_reply_turn`（`runtime.py`） | 迟到的摘要越序、埋掉新输入 | **无**——水位线解决 | 无 |
| `_run_loop` 入口 · task 轴 | 「run 的一切 memory 读写都落在折叠结果之上」 | 首次装配可能读到未折的 raw | prompt 白胀一轮 |
| `_run_loop` 入口 · agent 轴 | 新 task 经 `recall_recent_by_agent` 读到未 supersede 的 raw | 同上 | prompt 白胀一轮 |

**全部拆掉**（用户裁定 2026-09-22：「prompt 白胀一轮可以接受」）。后台 observe 由此
真正成为 fire-and-forget，与主线程彻底解耦——人回复不再为任何后台 LLM 往返买单。

**一个要知道的边缘情况**：一个 run 通常只在 `prepare` 装配一次，中途 fold 落地不影响
它。但若这一轮触发了 context recovery 的 `prepare` 重入，第二次装配会读到折叠后的结果，
上下文在同一个 run 内**前后不一致**（变小了）。结果不是错误，代价是 cache 前缀被打穿。
可接受，但排查上下文异常时要记得这条。

**不拆的**（用途不同，与 run 调度无关，保留）：
- `TurnHandle.wait_for_finish` 等 close 边界的 `TaskRecapDone`——那是 host 的 API 契约
  （拿到完整结果才返回），不是调度屏障。
- `TaskManager._fire_session_done` 的 `gather`——会话空闲判定。

**验证**
- 回归：拆掉后 `test_outage_resume.py`、`test_recovery_backfill_and_cancel_e2e.py`、
  reconcile 重放相关的 e2e 全绿（这几条是 task 轴 await 的主要覆盖面）。
- 新 e2e：park → 后台 observe 仍在飞 → 人立刻回复 → 回复的处理**不等**后台（断言注入到
  重排的耗时不含那次 LLM 往返），且上一段最终被正确折叠。
- 新单测：带外判决与重排并发到达 → 无论谁先，终态都自洽（判决先→FINISHED；重排先→判决被拒）。

---

## S5 · 后台 observe 在 `plain_text` 边界产 verdict ← **第一个行为切换点**

**改**：`background_observe.py`、`composer.py`（cue）。

1. `plain_text` 边界消费 `task_status`，经 S4 的入口提交。
2. cue 换掉：`_background_observe_cue("plain_text")` 改成判定版（`judge=True` 那条），
   并给 `_BACKGROUND_BOUNDARY_DESC` 加一条 `plain_text` 的状态描述（「这段以纯文本收尾，
   人在旁边等着」）。
3. subtasks 清单在 `plain_text` 边界也传进去（observer 判 retry 时要在 `next_step_hint`
   里指名哪个子任务产出不合格）。
4. **`is_short_segment` 降级为「免折不免判」**：从 LLM 调用**之前**的早退，挪到调用
   **之后**只 gate `segment_fold`。短段照样保 raw，verdict 一次不少。
5. **verdict 缺失 ≡ retry**：LLM 失败、没调 terminal tool、崩溃——一律维持 PAUSED，
   绝不静默放行。

**此刻的作用范围**：`act.py:1058` 的 `_is_own_root` gate 还在，所以只有 root task 的
纯文本走这条。**这是有意留的观察期。**

**行为变化**：root task 说完一段纯文本 park 之后，不再无限期 PAUSED——observer 判
success 就终结、判 retry 就继续等人。

**验证**
- 新 e2e：root task 纯文本 → park → 后台判 success → task FINISHED、后继放行。
- 新 e2e：判 retry → 仍 PAUSED，`next_step_hint` 出现在下一轮 prompt 里。
- 新 e2e：人在判定前开口 → 重排同 task，判决作废。
- 新单测：短段仍跑 LLM（拿到 verdict）但不写 `TASK_COMPACT_SUMMARY`。
- 回归：`test_interactive_task.py`、`test_hitl_e2e_v2.py`、`test_round_discarded_*`
  会受影响——它们断言的正是「root park 后一直 PAUSED」。

---

## S6 · act 分流切换

**改**：`core/loop/steps/act.py`。

1. `_finish_plain_text_turn` 的判据从

   ```python
   isinstance(settings, NormalTaskSettings)
     and interaction_mode == "interactive"
     and hitl is not None
   ```

   改成 `not task.unattended`（加 `hitl is not None` 的可用性兜底）。
2. 去掉 `launch_background_observe(boundary="plain_text")` 前的 `_is_own_root` gate。

**行为变化**：子任务的纯文本回合开始 park。S1 的闸门保证同一时刻只有一条交互线。

**验证**
- 新 e2e：子任务纯文本 → park → 判 success → 终结 + 后继放行，全程只有一条交互线。
- 新 e2e：父任务 park 期间，子任务不被派发（S1 闸门生效）。
- 回归：`test_unattended_e2e.py`（unattended 仍不 park）、`test_park_semantics.py`。

---

## S7 · 删 `interaction_mode`

**改**（此时已无人读，纯删）：

| 文件 | 删什么 |
|---|---|
| `models/task.py` | `interaction_mode` 字段、`TaskInteractionMode` 类型 |
| `capabilities/control_tools.py` | `_mode`、`_child_mode`、`delegate_task(interactive=)`、plan spec 里的 `interactive` |
| `runtime.py` | `dispatch_task(interaction_mode=)`、`unattended + interactive` 互斥校验、一处三元式 |
| `orchestrator/lifecycle/session_registry.py` | 一处三元式 |
| `control/types.py` | `TaskProjection.interaction_mode` |
| `control/reducers.py` | 三处（写 352 / 读 426 / 读 1007） |
| `control/converters.py` | 一处 |
| `orchestrator/task/manager.py` | 一处（事件 payload） |
| `loop/steps/act_guidance.py` | 按 mode 分叉的那段提示词 |
| `assembler/composer.py` | 两处 docstring 里「interactive 任务」的举例措辞 |

**迁移**：存量事件流带着这个字段，**反序列化路径必须忽略而不是报错**（`reducers` 的
`t.get("interaction_mode")` 直接删掉即可，dict 里多一个键无害）。老快照照样能恢复。

**测试**：
- 整体删除：`test_delegate_interactive_downgrade.py`（测 `_child_mode`）、
  `test_interaction_mode_persistence.py`（13 处，测投影持久化）。
- 重写：`test_interactive_task.py`（改成测新的 park 语义）。
- 机械修改：约 16 个文件里建 task 时传 `interaction_mode=` 的 fixture 引用。

---

## S8 · 收尾

**a. `next_step_hint` 改成真正的一次性**（`act.py` / `act_guidance.py`）

今天没有清除点，靠「下次 observer 判决覆写」。S6 之后 root 会经常产 hint，若某轮
observe 降级走机械判决，hint 会继续出现在再下一轮、变成过期指令。改成 **act 消费掉
就清**。

**b. host：`_root_agent_id` 改成「找当前可寻址的 agent」**
（`NetliveCoworkPy/src/netlivecowork/api/sessions.py`）

从固定找 `parent_agent_id is None`，改成用 `list_agents(session_id)` 过滤
`status == "waiting_human"`；没有则回落到 root（会话空闲时的正常情形）。

S1 的闸门保证同一时刻至多一个，所以「当前可寻址的 agent」是良定义的。
**host 的回合状态机（`entry.status` / `turn_seq` / `stage_round`）一个字不用动。**

**c.（可选）`next_step_hint` 的事件出口**

让 park 气泡能带一句「复核认为还差：X」。纯体验优化，不影响正确性，可以单独排期。

---

## 范围外 / 存疑

**前台 observe 放宽「root → 机械判决」——本计划不做。**

设计文档 §3.2 提过这条，但推演到最终形态后它不再必需：root task 的 `plain_text` 走
**后台** observe（那里必然跑 LLM），而 `actor_done` 走前台 observe 时机械判决够用
（agent 自己宣布了完成，`actor_done → success` 是合理映射）。

除非实测发现 `actor_done` 路径的 success 判得太松（agent 声称完成但实际没有），否则
不要为它引入一次额外的前台 LLM 调用。**这一条留作 S5/S6 观察期的一个待测问题。**

**LLM 调用次数**：S6 之后唯一真新增的一格是「无 observe ROLE 的子任务 + 短段」
（设计文档 §3.3 成本表第四格，+1）。曾考虑给极短回合一条「直接按 success 放行」的
快路径，已否决——一句话的回合既可能是交付完了，也可能是 LLM 问了个问题。
