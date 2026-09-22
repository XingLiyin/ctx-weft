# 纯文本回合交给 observer 判定 —— 机制全貌

> 状态：设计整理，未实施。
> 起因：`Task.interaction_mode` 是一个「纯文本 turn 怎么处理」的 task 级静态配置，
> 但「这段话是想问人还是交付完了」是逐回合的语义。用一个字段统一回答一个逐回合变化
> 的问题，必然有一半时候是错的——interactive 的 task 里 agent 交付完了也得 park，
> auto 的 task 里 agent 想停下来说句话也停不了。

## 1. 核心规则

纯文本回合（act 循环里 LLM 没有发出任何 tool call 的那一轮）的归宿，由**收尾方式**
和**有没有人在**两件事决定，不再由 task 上的任何配置决定：

| 收尾方式 | observe | park | 判定时机 |
|---|---|---|---|
| 调了 `finish_task`（`actor_done`） | 前台 | 否 | 同步，阻塞 |
| `unattended` task 的纯文本 | 前台 | 否（没人可等） | 同步，阻塞 |
| 其他纯文本 | **后台** | **立即 park** | 异步，与 park 并行 |

第三行是唯一的新行为。前两行今天就是这样跑的。

**park 与 observe 并行，不是串行。** park 立即发生，人马上能打字；observe 在后台跑，
判完再决定流程往哪走。这是整个方案成立的关键——它避免了「等 observer 判完才让人说话」
那种每回合多一次 LLM 往返延迟的体感。

### 1.1 park 之后的三条归宿

- **observe 判 `success`** → 带外提交给 TaskManager → task 终结 + DAG 后继放行。
  agent 仍可被 `send_message` 搭话（会在它身上开一个新 root task）。
- **observe 判 `retry` / `fail`** → 维持 PAUSED，人来决定下一步。人回复则冷 resume
  同一个 task 继续，带着 observer 的 `next_step_hint`。
- **人先开口**（早于 observe 判完） → 冷 resume 同 task，**observe 的判决作废**。
  人还有话说，这个 task 显然没完。`act_recap` / `outputs` 仍该记下。

`retry` 不再触发自动重跑。有人在场时人就是护栏，`max_retries` 是给无人值守的自动重跑
设的失控护栏，不该被一段正常的多轮对话烧光。

### 1.2 这条规则对 root task 和子任务完全一致

不区分 `parent_task_id`。子任务纯文本收尾同样 park，同样保留对话空间。

「放行后继」和「保留对话空间」不冲突，它们回答两个不同的问题：

- 后继能不能跑 —— observer 答（产出够不够用）
- 人还有没有话说 —— 人答

人的插话窗口是 park 到 observe 判完之间那段时间（一次后台 LLM 往返）。人慢于这个窗口
才开口，那本来就该是一次新的修正工作，而不是把已放行的后继倒回去。

**规则一致不等于并发。** 子任务同样 park，但调度层保证同一时刻只有一条交互线：非
unattended 的 task 在一个 session 内至多一个处于活跃或 park 状态（见 §3.7）。人面对的
永远是一条线，不是几条同时等他说话的线。

## 2. Task 上的字段

### 2.1 删除

| 字段 / 符号 | 位置 | 说明 |
|---|---|---|
| `Task.interaction_mode` | `models/task.py` | 整个字段 |
| `TaskInteractionMode` | `models/task.py` | 类型别名 |
| `_mode` / `_child_mode` | `capabilities/control_tools.py` | 两个辅助函数 |
| `delegate_task(interactive=...)` | `capabilities/control_tools.py` | LLM 工具参数 |
| `plan` spec 里的 `interactive` | `capabilities/control_tools.py` | 同上 |
| `dispatch_task(interaction_mode=...)` | `runtime.py` | host 侧参数 |
| `TaskProjection.interaction_mode` | `control/types.py` | 投影槽位 |

连带删除：`session_registry.py` / `runtime.py` 里两处 `"auto" if unattended else
"interactive"` 三元表达式、`runtime.py` 里 `unattended + interactive` 的互斥校验、
`act_guidance.py` 里按 `interaction_mode` 分叉的那段提示词。

### 2.2 保留

**`unattended: bool`** —— 唯一保留的「有没有人在」。删掉 `interaction_mode` 之后，
它成为 park 的唯一前提。它不是 `interaction_mode` 的别名，它答的是一个事实（有没有人），
不是一条策略（纯文本要不要停）。**不给 LLM 旋钮**：它是作业被怎么起起来的事实，只能
从父任务继承，不能由 actor 自行宣布。

与这个机制相关的其它 Task 字段，一个都不动：

| 字段 | 谁写 | 谁读 |
|---|---|---|
| `observer_outcome` | observer 的 `report_task_outcome` | FinalizeStep → RunOutcome |
| `next_step_hint` | 同上 | `act_guidance` 渲染进下一轮 prompt |
| `process_report` / `_at` | 同上（`act_recap`） | 段摘要 / finish 对 / 子任务 bubble |
| `task_summary` | 同上 | 同上 |
| `outputs` | **改由 observer 产**（见 §5.4） | `_build_memory_content` / background_observe / finalize 等 6 处 |
| `actor_done` | `finish_task` 等控制工具 | act 循环退出判据 → `exit_reason="actor_done"` |
| `retry_count` / `max_retries` | TaskManager | disposition 表 |
| `dag_deps` | 建 task 时 | 后继解锁 |

`Task.settings`（`NormalTaskSettings`）与本机制无关：它装的是**装配决策**
（`skill_name` / `use_subagent` / `inherit_memory` / `inherit_from_agent_id` 等），
只在装配那一刻被读一次，不像 `unattended` 那样要被 reducer、HitlService、restore
反复直读。这条分界线保持不变。

## 3. 组件职责

### 3.1 act（`loop/steps/act.py`）

纯文本回合的分流点，在 `_finish_plain_text_turn`。改造后的判据只剩一条：

```
if not task.unattended:
    立即 park（_cold_park → HitlPark）+ 启动后台 observe
else:
    emit ACT_TURN_COMPLETED(reason="stop") → 前台 observe
```

今天那个 `isinstance(settings, NormalTaskSettings) and interaction_mode ==
"interactive" and hitl is not None` 的三重判据整体消失。

`_park_await_user` 里的无人值守守卫（`if task.unattended: return`）在新判据下成为
冗余的第二道防线，可以保留（它读的和 `hitl.open()` 的守卫是同一个真相源，不存在漂移）。

`_park_for_interrupt`（人按暂停键）不受影响，它对无人值守守卫的豁免照旧。

### 3.2 前台 observe（`loop/steps/observe.py`）

职责不变，但**适用场景收窄**到两种：`finish_task` 收尾，以及 unattended task 的
纯文本收尾。

「root task → 机械判决」这条规则需要放宽。今天的理由是「顶层任务无 parent 可上报，
不需要 LLM observer」，但新机制给了它新用途：不是上报给 parent，而是**判定 + 决定
要不要放行后继**。口子已经开过一次——`max_turns` / `context_limit` 时即使 root 也
强制 LLM observe——现在要把条件放宽到「凡是要产 verdict 的场合」。

### 3.3 后台 observe（`loop/steps/background_observe.py`）

今天它的 terminal tool 是 `collect_process_report`，只产段摘要 + 折 raw，
**刻意不写 task 状态**。改造后它要兼产 verdict。

**LLM 调用次数不变**，这是本方案最划算的地方。今天 recap / 折叠有四条路径，
`_is_own_root` 只 gate 其中两条：

| 路径 | 触发条件 | root gate? | LLM 成本 |
|---|---|---|---|
| close 边界后台 observe | `normal`/`actor_done` + 非 retry | 是 | 一次后台 LLM |
| `mechanical` 边界后台 observe | `not used_llm and not launched` | **否** | 一次后台 LLM |
| `plain_text` 边界（`act.py`） | interactive 纯文本让位 | 是 | 一次后台 LLM |
| `_fold_retry_segment` | verdict == retry | **否** | 无（复用 `act_recap`） |

（`_is_own_root` docstring 里那句「非 root 不触发后台 observe」只是在解释 close 边界
那一条的 gate 理由，不是普遍规律。）

逐格对照改造前后：

| 场景 | 今天 | 改造后 | LLM 次数 |
|---|---|---|---|
| root 纯文本 | close 边界后台 observe 产 `_close_report` | 同一次调用换 terminal tool，兼产 verdict | 不变 |
| 子任务纯文本，有 observe ROLE | 前台 LLM observe（同步，无条件跑） | park + 后台 LLM observe（异步） | 不变 |
| 子任务纯文本，无 ROLE，长段 | `mechanical` 后台 observe | park + 后台 observe 带 verdict | 不变 |
| 子任务纯文本，无 ROLE，**短段** | **`is_short_segment` 免折，零调用** | 必须跑（要 verdict） | **+1** |

前三格是同一次 LLM 调用从前台挪到后台、顺带多产一个 verdict。第四格是真的新增，
且不可避免——理由见 §3.3.1。

### 3.3.1 短段免折门要降级为「免折不免判」

`is_short_segment` 今天同时决定两件事（不跑 LLM、不折叠），只因为后台 observe 今天
跑 LLM 的唯一目的就是产摘要。新方案里它有两个产物，收益判断完全不同：

- **verdict** —— 必出。**短段更需要它**：一个纯文本短回合既可能是「一句话就交付完了」，
  也可能是「LLM 问了个问题」，这两者只有判定能区分。用回合长度去绕过判定，等于把本
  方案要解决的问题原样放回去。
- **段摘要** —— 可免。短段的 recap 常比原文还长，折它是净亏。这条判断不受影响。

所以门要降级：从「要不要跑这次后台 observe」改成「跑出来的 `act_recap` 要不要拿去
折叠」。实现上是把它从 LLM 调用**之前**的早退，挪到调用**之后**只 gate `segment_fold`
那一步。短段照样保 raw，verdict 一次不少。

可省的地方：短段那次调用可以用更轻的装配（只要 verdict、不要 `act_recap`），省 token。

「失败吞掉、降级 = 该段保 raw」这条不能照搬到 verdict 上。判定失败时 task 该停在
PAUSED（维持 park）而不是被静默放行——即 **verdict 缺失 ≡ retry**，这与「默认态是
park，只有 success 触发转移」是同一句话。

### 3.3.2 terminal tool 可以统一，`collect_process_report` 可以删

它的签名是 `report_task_outcome` 的**真子集**（只有 `act_recap` + `task_summary`，没有
`task_status` / `task_failure_reason` / `next_step_hint`）。它存在的全部理由是那句
docstring：「Zero state write: never touches task.status / process_report / etc.」。
新方案下后台要写状态，这个约束本身消失。

但后台**不是所有边界都产 verdict**，统一 terminal tool 之后要按 boundary 决定消不消费
`task_status`，否则会把 `actor_done` 路径的前台判决覆盖掉：

| boundary | 产 verdict? | 产摘要? |
|---|---|---|
| `plain_text` | **是**（新） | 是 |
| `mechanical` | 否——判定已由机械判决给出 | 是 |
| `finish` / `normal` | 否 | 是，落 `_close_report` |
| `interrupt` / `dispatch` | 否 | 是 |

### 3.3.3 两条装配路径可以合并

前台 `_build_observer_messages` 与后台 `_build_background_observe_messages` 的**主体
完全一样**：system 都是 `_build_act_system`，骨架都是 `_build_facet_trailing_messages`，
对话主体是同一批 blocks 重建的 act 风格完整会话。差异只有三处参数：

1. **尾部 cue —— 语义正好相反。** `_background_observe_cue` 结尾写死「Just summarize —
   **do not judge success/retry/fail**, and do not call any other tool.」。新方案下
   `plain_text` 边界必须换掉这条，并给 `_BACKGROUND_BOUNDARY_DESC` 加一条 `plain_text`
   的状态描述（「这段以纯文本收尾，人在旁边等着」）。
2. **subtasks 清单 —— 前台有，后台没有。** 前台从 `request.extra["subtasks"]` 渲染
   每个子任务的 `task_id`/`title`/`outcome`，供 observer 在 `next_step_hint` 里**指名**
   哪个子任务产出不合格。后台今天不产 hint 所以不需要；新方案下它要产，必须补上。
3. **outputs 注入条件不同 —— 但这条不用改。** 前台无条件注入 `## Final output`，后台
   只在 close 边界注入。理由在 `_finish_result_section` 的 docstring：`finish_task` 是
   SILENT 工具，产出不写任务层对话，不显式喂进来观察者会虚构完成叙述。而 `plain_text`
   收尾时那段文本本身就是 assistant 回合、在重建的对话里看得见——后台不注入是对的。

三处都能参数化（`judge: bool` / `subtasks` / `pre_cue_sections`），合并之后「前台 vs
后台」在装配层彻底消失。剩下的差异——异步护栏（per-task 锁、幂等、agent 已被 evict 的
处理）、`origin`（host 据以不把后台 LLM 交互渲染进对话流）、落地路径——全在调用方，
那才是真正的分界。

### 3.4 带外判决入口（新增）

park 抛 `HitlPark` 时 run 就结束了。后台 observe 判完要改的是一个**没有活跃 run 的
task**——今天 `_close_report` 槽由 finalize 在同一个 run 内取用，那条路在这里不存在。

需要新增：后台 observe 完成 → 构造 `RunOutcome` → 提交 TaskManager 处置。

- TaskManager 作为「task 状态的唯一改写者」的地位不变，新增的是它的一个调用方。
- `disposition_for` 是纯函数，**一行不用改**：判 success 走已有的 `COMPLETED +
  verdict="success"` 分支。
- task 终结时要 **resolve 掉那个未决的 `wait_for_user` HITL 请求**，否则悬空。
- 「人先开口 vs observe 先判完」的竞态由**带外入口自己仲裁**：判决到达时检查 task
  是否还是 PAUSED，不是就拒绝；检查与状态转移在 TaskManager 锁内原子完成。
  **不能靠 `await_pending_background_observe` 兜底**——那个 await 保的是「新 run 不与
  后台 observe 并发跑」，而 `_inject_user_turn` 写 USER_PROMPT 时根本不等它（见 §6.5）。

### 3.5 HitlService / CapabilityGateway

职责不变。`unattended` 在它们那里的作用点一个不动：

- `HitlService.open()` —— 无人值守守卫（HITL 的唯一登记入口）
- `CapabilityGateway` —— 需要人工授权的能力自动拒绝、`ask_user` 的「没人可问」兜底

### 3.6 assembler / composer

`_current_task_user_index`（取首条，钉 `## Current Task` 框）和
`_latest_task_user_index`（取末条，钉 `## Current Message` 框）**原样保留**。

它们不依赖 `interaction_mode`，只按 `task_id` 匹配。而且在新机制下它们更重要了：
`retry` → park → 人回复 → 冷 resume **同一个 task**，正是同一 task 累积多条
`user_prompt` 的来源。首条是原始请求，末条是「你继续」，重量级注入钉在首条才不会
每轮漂移、打穿 cache 前缀。

只需把这两个函数 docstring 里「interactive 任务」的举例措辞改掉。

### 3.7 TaskManager：单交互线闸门

**判据**：非 unattended 的 task，在一个 session 内同时至多一个处于「在跑 **或**
PAUSED / AWAITING_HUMAN」。unattended 的后台作业不受此限，照 `max_concurrent` 并行。

**位置**：`drain()` 的 `skip` lambda。那里已经有「同 agent 不并发」的谓词，再加一个
同族的即可；`_tasks` 全量在手，查得到 park 中的 task，不需要新状态。

**判据为什么不能是「在跑」**：park 一发生槽位就释放——`_settle` 对 `_PARKED_STATUSES`
（含 `AWAITING_HUMAN`）先 `_release_slot` 再立刻 `drain()`。于是 root task 一 park，
下一个非 unattended 子任务就被派发，它也 park，host 就看到两条线了。必须把 park 中的
算进闸门。

**维度天然对齐**：TaskManager 是 per-session 的（`_task_managers[session_id]`），host
的 `entry` 也是 per-session 的。约束落在 TaskManager 上，粒度正好是 host 单线模型需要
的那个，不需要跨层协调。

**两个代价**（2026-09-22 确认接受）：

1. **有人在场的会话里 DAG 并行变串行。** `unattended` 是继承的，正常会话里 delegate
   出的子任务全部 `unattended=False`，于是串行跑。推进没停（一个接一个），但并行度没了。
   有人盯着的会话里串行反而更可读；真正需要并行的是后台作业，不受影响。
2. **判 `retry` 的 park 会停住整条交互线。** park 的三种结局里只有 `retry`/`fail` 长期
   占槽（`success` 一次 LLM 往返就释放，人先开口则重排同 task）。而 retry 恰恰意味着
   「agent 说它没做完、要人介入」——此时停住其他 task 是对的，让别的线抢跑只会让人
   看不过来。

`unattended` 的角色由此更完整：它答三个问题——纯文本要不要 park、HITL 能不能开、
能不能并行。三者都直接源于「有没有人在」这一个事实，是推论不是策略，所以不会重蹈
`interaction_mode` 的覆辙。

## 4. 场景走查

### 4.1 有人在场的 task，agent 说了段纯文本

```
act: LLM 回合无 tool call
 └→ _finish_plain_text_turn
     ├→ launch_background_observe(boundary="plain_text")   [异步，不等]
     └→ _cold_park → hitl.open(form=WAIT) → raise HitlPark
         └→ run 结束，RunOutcome(kind=AWAITING_HUMAN) → task PAUSED
            人此刻已经可以打字
后台 observe（几秒后）
 ├→ report_task_outcome(success) → 带外提交 → task FINISHED + 后继放行
 ├→ report_task_outcome(retry)   → 维持 PAUSED，next_step_hint 留给下一轮
 └→ 调用失败 / 没调 terminal tool → 同 retry（verdict 缺失 ≡ retry）
```

人如果在后台 observe 判完之前开口：冷 resume 同 task → 注入 `USER_PROMPT` → 重入 act。
到达的 observe 判决被仲裁拒绝。

### 4.2 agent 调了 finish_task

```
act: finish_task → actor_done=True → exit_reason="actor_done"
 └→ 前台 observe（同步阻塞）
     ├→ success → FINISHED + 后继放行
     ├→ retry   → 走 disposition 表的自动重跑（烧 retry_count）
     └→ fail    → FAILED
```

不 park。agent 明确宣布「我做完了」时判定是同步的，流程不带着未决判定往前走。

### 4.3 unattended task 的纯文本

```
act: 纯文本 → 不 park（没人可等）→ emit stop → 前台 observe → 正常处置
```

与今天 `auto` 模式的行为完全一致。

### 4.4 子任务说了段话，人想插话

外部消息一律走 `send_message(agent_id)`。它的路由判据是 **`current_task_id` 是否
终态**，不是人的快慢，也不是 agent 忙不忙：

```python
current = rec.current_task_id
if current and not self._task_is_terminal(rec.session_id, current):
    → _inject_user_turn(current, ...)    # 注入并重排该活 task
else:
    → _start_task_for_agent(...)          # 新建 task
```

于是 park 之后有两种落点：

- **park 中**（task PAUSED，**不是**终态）→ 注入并重排**同一个 task**。这是绝大多数
  情况：后台 observe 还在飞，或它判了 `retry` / `fail`。
- **observe 已判 success**（task FINISHED、后继已放行、agent 转 `idle`）→ 才走
  `_start_task_for_agent` 新建 task。对已终态 agent 它会 `recover_agent(keep_alive=
  True)` 冷恢复，这条路今天就存在。

**park 不是 busy。** `assert_can_receive` 的守卫里 `running` 才抛 `AgentBusyError`，
而 park 之后 run 已经抛 `HitlPark` 结束、agent 是 `waiting_human`，明确在放行名单里
（`idle` / `waiting_human` / `interrupted` 放行）。这正是 park 的意义——让出控制权、
变得可以收消息。

第三条路径不在本机制范围内但要知道：父任务 `_suspended_on_live_children`（等子任务）
时，消息只写进对话，`_try_resume_parent` 在子任务收尾时自然唤醒它。人对父 agent 说话
走这条，对子 agent 说话走上面两条。

### 4.5 子任务需要人的决策才能继续

走 `ask_user`，不走本机制。它是**主动求助**：硬等待（PAUSED_HITL），只阻塞它自己，
兄弟任务照跑。本机制的 park 是**被动的对话窗口**：软待命（PAUSED），允许但不强制回复。

两者不可互相替代，各有各的入口。

## 5. 不变式

1. `unattended ⟹ 从不 park`。由 act 的单一判据保证，`HitlService.open()` 的守卫
   是第二道防线。删掉 `interaction_mode` 之后，今天那四处「`unattended ⟹ auto`」
   的重复保证一并消失。
2. **默认态是 park，只有 `success` 触发状态转移。** `retry` / `fail` / verdict 缺失
   / observe 崩溃，归宿都是「维持 PAUSED」。
3. 一个 task 至多一个在途后台 observe（`_task_locks` 串行，已有）。
4. **最终交付物是 actor 的收尾消息正文**，不是任何工具参数。这条既有契约不变——
   observer 产的是 `task.outputs`（给 parent / 给系统的结构化交付物，取代
   `_compose_final_outputs` 的机械拼接），**不是**用户看到的对话正文。
5. **一个 session 至多一条交互线。** 非 unattended 的 task 同时至多一个处于活跃或
   park 状态（§3.7）。这条是 host 单线回合状态机得以不变的全部依据。

## 6. 待定事项与风险

### 6.1 next_step_hint 需要改成真正的一次性

今天它没有清除点，靠「下次 observer 判决时覆写」（`task.next_step_hint = hint or
None` 是唯一写点）。今天没事，因为 root task 极少跑 LLM observe、几乎不产 hint。

新机制下 root task 会经常产 hint，于是：park → 人回复 → 这一轮若 observe 走了机械
判决降级（不调 `report_task_outcome`），hint 不会被覆写，会继续出现在再下一轮，变成
过期指令。**实现时改成 act 消费掉就清。**

### 6.2 next_step_hint 对人不可见

它只进 prompt（`act_guidance` 渲染），没有对应事件发给 host。

但在 `retry` → park 的场景下，人正要决定「要不要让它继续」，而 observer 刚判断了
「没做完，下一步该做 X」——这正是人需要的信息。park 气泡上如果能带一句「复核认为还
差：X」，人的决策质量完全不同。**需要给 hint 加一条事件出口**，是新增，不是改。

### 6.3 成本

**LLM 调用次数不变**（见 §3.3 的逐格对照）。新增成本全在 LLM 之外：

- **DAG 后继从同步判定变成异步判定，且在有人会话里叠加串行。** 今天子任务纯文本走
  前台 observe，判完直接放行；改造后要先 park、再等后台判完才放行。注意这是**延迟**
  而非额外开销（那次 LLM 调用本来就要发生），但 §3.7 的单交互线闸门会让它在有人在场
  的会话里**累加**：5 个子任务串行，每个纯文本收尾各等一次 park→判定的往返。实际影响
  取决于 actor 多常用纯文本收尾——调了 `finish_task` 的走前台 observe，不 park。
- **每个纯文本回合一次 `hitl.open()` 落库。**
- **多一次 run 生命周期往返。** run 抛 `HitlPark` 结束，之后冷 resume 或被带外判决
  终结。DAG 里五个子任务各说几轮，就是十几次这样的起止。
- **人回复后没有额外延迟**（2026-09-22 裁定）。原本设想靠 `await_pending_background_observe`
  兜住时序，代价是人回复要等一次后台 LLM 往返。改为**段界水位线**（§6.5）之后，迟到的
  折叠自己落回原位，三处 await 全部拆掉，人回复不为任何后台往返买单。代价是首次装配可能
  读到未折叠的 raw——prompt 白胀一轮，已确认接受。
- **短段无 ROLE 的子任务多一次 LLM 调用**（见 §3.3 第四格）。曾考虑给极短回合一条
  「直接按 success 放行、不跑 LLM」的快路径，**已否决**：一句话的回合既可能是交付完了，
  也可能是 LLM 问了个问题，无法用长度区分。门改为「免折不免判」，见 §3.3.1。

### 6.4 host 侧

- 几个子 agent 同时 park，界面上是几条等着人说话的线。人只对其中一条说话，其余的照常
  被 observe 判定放行。这个呈现模型比今天（只有 root 一条线）复杂。
- `retry` → park 发的是 `ACT_TURN_COMPLETED(reason="await_user")`，`success` →
  FINISHED 发的是 `TaskFinished`。人在两种情况下看到的都该是「agent 说完了，我可以
  打字」。这个区别纯属系统内部（上下文延不延续、`next_step_hint` 注不注入），不该
  泄漏到界面上。
- root task 的 `FAILED` 不该渲染成红色错误，它只是「这轮没做成」，跟子任务失败不是
  一回事。
- 要让用户能对子 agent 回话，host 需要把子 agent 的纯文本呈现到用户看得见的地方，
  并在回话时带上 `agent_id`。**core 侧的数据前提已经满足**，缺的只是界面：

  | 入口 | 过滤 | 拿到什么 |
  |---|---|---|
  | `send_message(agent_id, ...)` | —— | 按 agent 显式寻址；`agent_id` 全局唯一，路由只看它，`session_id` 不参与 |
  | `list_agents()` | `session_id` / `parent_agent_id` / `include_terminated` | `AgentSummary`：`status`（park 中 = `waiting_human`）、`current_task_id`、`parent_agent_id`（还原成树） |
  | `list_pending_hitl()` | `session_id` / `agent_id`，可叠加 | `HitlRequestView` |

  两个坑：

  1. **内存现实 ≠ 全部事实。** ALM 是只增不删的缓存，但 `forget_session` 会把一条会话
     逐出，逐出后 `list_agents` 返回空列表。几个子 agent park 着时进程重启，host 必须
     先 `rebuild_session(session_id)` 才看得见那些 park 线（`list_pending_hitl` 同理：
     「恢复是喂进来、不是查回去」）。
  2. **`AgentSummary` 里没有 `session_id`。** 热路径不碍事（能列出来就说明 registry
     里有它），但 host 若把列表持久化、隔天再拿来发消息，registry miss 且给不出
     `session_id` 时会抛 `AgentNotLoaded` —— 那条「没 session 语境就扫全部 active
     session」的 sweep 自 2026-09-21 已删。host 侧要自己把 session_id 存住。

### 6.4.1 NetliveCoworkPy 现状：session 主键，固定打到 root agent

查过 host 实现（2026-09-22）。它的 core 对接是扎实的——调 `list_agents` 前先
`ensure_core_warm` 装填、`send_message` 显式带 `session_id`，上面两个坑都处理了。
**问题在维度**：

```
POST /sessions/{session_id}/messages        ← 路由是 session 主键
  SendMessageRequest: content / initial_task / llm_* / reasoning_effort / user_info
                                            ← 没有 agent_id
  → agent_id = await _root_agent_id(runtime, entry)
      找 parent_agent_id is None 的那个
  → runtime.send_message(agent_id, content, session_id=session_id)
```

`api/sessions.py` 里所有路由都是 `/{session_id}/...`，没有一条 agent 维度的。
**子 agent 今天完全无法寻址。**

host 的 `entry` 是 **session 级的单线回合状态机**——`entry.status`、`entry.turn_seq`、
`entry.user_prompt`、`begin_user_turn` / `stage_round` 的临界区全是单值。若几个子 agent
能并行 park、各自独立对话，这些单值就都要回答「是谁的」。

**但它们不需要回答。** §3.7 的单交互线闸门从调度层保证同一时刻只有一条非 unattended
的线，host 的单线假设因此继续成立，回合状态机一个字不用动。

于是 host 侧的改动缩成一处：

- **`_root_agent_id` 改成「找当前那个可寻址的 agent」**，而不是固定找
  `parent_agent_id is None` 的 root。park 的可能是某个子 agent——按 §3.7，同一时刻
  至多一个，所以这个「当前可寻址的 agent」是良定义的。用 `list_agents(session_id)`
  过滤 `status == "waiting_human"`；没有则回落到 root（会话空闲时的正常情形）。
- `SendMessageRequest` 可以加一个可选 `agent_id` 做显式寻址，但不是必需——单线之下
  host 自己就能解析出唯一的那个。

`AgentBusyError → 409` 的处理照旧：单线之下它仍然罕见（用户对着当前那条线说话时，
那条线要么在 park、要么刚被自己的消息重排）。

**结论**：core 侧没有缺口；host 侧一处改动，回合状态机不动。

### 6.5 段界水位线：让迟到的折叠自己归位

**问题不在时间戳。** `segment_fold` 的锚点早就不用 `now()`：`summary_ts =
following[0].timestamp - 1μs`（被折内容之后第一条记录的时间戳减一微秒），注释还特意标了
「段尾：锚被折段末条位置，不用 now()」。迟到的摘要本来就会被放回该在的位置。

**问题在段界是动态查找的**：`boundary_idx = 最后一条 role=user 回合`，`pool =
view[boundary_idx+1:]`。新 USER_PROMPT 一写进去它就成了「最后一条 user 回合」，于是
`pool` 空 → `to_archive` 空 → 落到 `else: summary_ts = now_utc()`（摘要排末尾）→
`memory.fold([], [summary])`（**上一段 raw 一条都没折**）。锚点逻辑根本没机会生效。

**改**：`launch_background_observe` 快照 state 时把当时的段界一并钉住（最后一条 user
回合的 record id，或 `(timestamp, seq_no)` 水位线），`segment_fold` 按水位线算 pool。
新 USER_PROMPT 落在水位线之后、进 pool，但 `_protected` 把它排除出 `to_archive`，于是
`following[0]` 正是它，摘要精确落回原位。

由此三处 `await_pending_background_observe` 全部可拆（`_write_hitl_reply_turn` 一处、
`_run_loop` 入口两个轴），后台 observe 真正成为 fire-and-forget。

### 6.6 迁移

`interaction_mode` 有投影槽位（`TaskProjection`）和事件负载（reducers 三处、
`manager.py` 一处）。存量事件流里带着这个字段，反序列化路径需要忽略它而不是报错。
老快照照样能恢复。
