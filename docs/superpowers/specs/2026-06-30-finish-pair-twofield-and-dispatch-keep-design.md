# finish 对两段化（act_recap / task_summary）+ 同 agent 派发对保留

- 日期：2026-06-30
- 范围：
  - **ctx-weft core**（`src/ctx_weft/`）：
    - `core/orchestrator/control_capability.py` — `report_task_outcome` / `collect_process_report` 字段重设计
    - `core/loop/steps/observe.py` — `Verdict` 增 `task_summary`；`summary`→ 承载 `act_recap`
    - `core/loop/steps/finalize.py` — same_agent 在 finalize 铸 start_task 框 + 配对静态 result（框与 result 同锚 task.started_at，见 §2.5 修订 2026-07-03；delegate_task 不再由 gateway eager 写框）；
      `_synthesize_dispatch_pair` 两段化；汇报给 parent 的 `mem_content` report 部分改 `task_summary`
    - `core/loop/steps/background_observe.py` — close 路径携两字段；`_replace_finish_report` 同时换 assistant+tool
    - `core/assembler/composer.py` — observe / background_observe 的 cue 改为要两字段（含「综合子任务结果」）
    - `core/runtime.py` — tracking/predecessor 汇报（`_fetch_tracking`）的 process report 部分改用 `task_summary`
  - **host persona prompts**（§2.7，两份 git-tracked 副本均改）：
    - `resources/agents/default/ROLE.md` + `packaging/default_data/agents/default/ROLE.md`（observer：`task_process_report` → `act_recap` + `task_summary` 两段）
    - `resources/agents/default/SOUL.md` + `packaging/default_data/agents/default/SOUL.md`（actor：澄清 `finish_task` 的 `result` = 给 user 的最终输出，执行历程由观察者另记）
- 相关 spec（本设计**修订/续作**）：
  - **修订** `2026-06-28-dispatch-pair-fold-alignment-design.md` §2.1：同 agent 子任务**不再 supersede** 那条
    delegate 回合，改为**保留 delegate + 写一条配对的静态 tool result**（时间戳锚到 task.started_at，见 §2.5 修订 2026-07-03）。
  - **续作** `2026-06-28-dispatch-pair-fold-alignment-design.md` §3.1(a) / §3.3：把「综合已完成子任务结果」
    从未做的软引导，落成 observe 工具的结构化字段 `task_summary`（不再只依赖 LLM 自觉）。
  - **沿用** `2026-06-27-task-segment-summary-assistant-role-design.md`：finish 对 assistant 回合本就该是
    agent 自述；本设计给它填上诚实内容（`act_recap`），与「段摘要=assistant」一脉。
  - 底层机制：`docs/spec/06-memory-layers-and-compaction.md`、`2026-06-28-task-resident-capsule-design.md`。

---

## 1. 问题

### 1.1 同 agent 派发对被 supersede，且历史上派发结果时间戳错位 → 400
`2026-06-28` §2.1 让 same_agent close 时 **supersede 掉 gateway 写的 delegate 回合**，子任务由嵌套 finish 对
全权承载。两个后果：

1. 胶囊里丢了「派发成功」这个锚——子任务 body 直接以本 agent 自有回合内联出现，读起来像凭空冒出一段子任务。
2. 更早的实现把同 agent 的派发 tool result 时间戳打在 **close 时刻**（T2），而子任务 body 的时间戳落在
   `(T1, T2)`。composer 按 `(timestamp, seq_no)` 平铺归并（主键是 timestamp），result @T2 排到了整段 body
   **之后** → delegate(@T1) 与它的 result(@T2) 被 body 劈开 → OpenAI 兼容端「tool 必须紧跟 assistant
   tool_calls」校验 400（实测会话 agt_01KW9AT…：delegate@01:49:50、result@01:58:10，间隔 8 分钟）。

### 1.2 finish 对两段内容缺分工
`_synthesize_dispatch_pair` 现在写 `assistant{content="" + finish_task 调用}` / `tool{"Process Report: …"}`：
assistant 自述是**空的**（agent「在说话」却什么都没说），综合总结挤在 tool 的 Process Report 里。缺少
「assistant 诚实复述这一段 act 干了啥 / tool 承载整段综合总结」的清晰分工。

### 1.3 非 root 子任务的 finish report 只能来自 inline 工具
`background_observe`（产高质量 close report）**只对 own-root 触发**（`observe.py:248` `_is_own_root`）。
非 root 子任务——**包括同 agent 子任务、以及子任务的子任务**——拿不到后台 close observe，其 finish 对内容
只能来自 **inline `report_task_outcome`** 的 verdict。故字段重设计必须**两个 observe 工具一并改**，否则
嵌套子任务的 finish 对仍是旧形态。

---

## 2. 设计

### 2.1 observe 工具字段重设计（两个工具一并改）

把现有的单字段 `task_process_report` **折成 `act_recap`**，并新增 `task_summary`：

| 字段 | 何时产 | 语义 | 去向 |
|---|---|---|---|
| `task_status` | inline 每轮 | success/retry/fail | task 状态机（不变） |
| `act_recap` | **始终** | **诚实复述上一轮 act 阶段做了什么**（第一人称、忠于 transcript，只管最后这一段、不求全） | finish 对 **assistant** content；retry 时作 `task.process_report`→ Current Progress；非 close 段边界作 `TASK_COMPACT_SUMMARY` 段摘要 |
| `task_summary` | **仅终态**（success/fail / close 边界） | **整个 task 执行历程的简洁总结**：点出重要步骤与经验/教训，不琐碎；**不是**最终输出（最终输出 = `finish_task` 的 `result` 入参 / `task.outputs`）。**承载 process report 作用** | finish 对 **tool** result；汇报给 parent 时与 `outputs` 拼接 |
| `task_failure_reason` / `task_reviews` / `next_step_hint` | inline | 不变 | 不变 |

- **`report_task_outcome`**（inline，`purposes=["observe"]`）：`task_process_report` 改名 `act_recap`；新增
  `task_summary`（success/fail 必填、retry 留空）。`task.process_report = act_recap`（retry Current Progress
  语义不变）。verdict 同时带出 `act_recap` + `task_summary`。
- **`collect_process_report`**（background，`purposes=["background_observe"]`）：同样产 `act_recap` +
  `task_summary`。close 边界两者都用；非 close 段边界（interrupt/plain_text）只用 `act_recap` 作段摘要
  （`apply_compact(summary=act_recap)`），`task_summary` 留空。
- **折叠理由**：retry 反馈本就是「上轮做了啥 + 为何重试」≈ `act_recap`；避免终态时让 observer 同时产
  3 个总结字段（process_report + act_recap + summary）的冗余。`next_step_hint` 仍可拼进 `act_recap`
  （沿用现 `task_process_report` 的拼接）。

### 2.2 `Verdict` 与装配 cue

- `Verdict`（observe.py）**字段直接改名** `summary` → `act_recap`，并增 `task_summary: str = ""`。
  全仓 `verdict.summary` 引用点同步改名（`observe.py` 装配/回退、`finalize.py` 读取、`_rule_observe`
  机械产出、相关测试）。
- composer 的 `_OBSERVE_JUDGMENT_CUE` / `_background_observe_cue`：改为要求两段产出，并显式引导
  `task_summary` **综合已完成子任务的结果**（落实 `2026-06-28` §3.3 软引导）。

### 2.3 finish 对两段化（`_synthesize_dispatch_pair`）

占位合成改为两段都带内容（不再 `content=""` / `"Process Report:"`）：
```
assistant: {act_recap}  [tool_call finish_task id=Y, input.result={outputs}]   @T2
tool (id=Y): {task_summary}                                                     @T2
```
- **outputs 与 task_summary 结构上分置**：`finish_task` 的 `input.result = task.outputs`（给 user 看的
  最终输出）；tool 槽 = `task_summary`（process report，简洁、点重要步骤+经验）。agent 自看时两者都在。
- 内容来源：finalize 从 `state.verdict` 取 `act_recap` / `task_summary`（不再靠 `mem_content` 字符串
  `rsplit("Process Report: ")`）。
- `mem_content`（= `outputs` + `task_summary`，§2.6）**仅留给 `BLACKBOARD_PUBLISH` 与 cross_agent bubble**，
  与 finish 对解耦——这两条是「汇报给 parent」，须把两部分拼在一起。
- own-root：占位先由 inline verdict 写；background close observe 产更优两段后经 §2.4 替换。
- 非 root：inline verdict 的两段即最终（无后台替换）。

### 2.4 `_replace_finish_report` 同时替换 assistant + tool

现仅 supersede + 重写 tool（匹配 `"Process Report:"`）。改为：按 `tool_call_id` + `origin_task_id` 定位
finish 对的**两条**记录，supersede 后重写：assistant← 新 `act_recap`、tool← 新 `task_summary`。
close 路径结果槽（`_close_report` / `register_close_synth` 的 A1 时序机制）扩成携 `(act_recap, task_summary)`
二元组，其余 fire-and-forget / 强一致 await 逻辑不变。

### 2.5 同 agent 派发：保留 delegate + 配对静态 result（修订 `2026-06-28` §2.1）

`finalize._close_one` 的 same_agent 分支：
1. **不再 supersede** 那条孤立 delegate 回合——保留它。
2. 改为**写一条配对的 tool result**：`tool_call_id = task.origin_tool_call_id`、`origin_task_id = parent`、
   content = **静态文案**「任务派发成功，以下是执行记录：」（常量，**不含**任何子任务结果），
   **timestamp = task.started_at**（子任务真正开始执行的时刻；见下方修订）。
   **之后永不回填**（对应用户「不把 process 填进去，就保留这个」）。
3. 嵌套 finish 对（§2.3 新形态，origin=child）仍在 close 时写、@T2 收尾。

> **修订 2026-07-03（框与 result 同锚 task.started_at；delegate_task 改由 finalize 铸框）**：
> 演进两步——
> (a) 先把 ack 从「delegate 派发时刻」改锚 `task.started_at`（用户视角这条 `Task '…' started.` 表达
>     「任务**已开始执行**」，应带执行开始时刻）；
> (b) 但**框仍是 gateway 在派发时刻 eager 写的**，与锚 started_at 的 result 分处两个时间戳 → 并发多派发时
>     召回会 `F,F,F,F,R,R,R,R` 堆叠错序（只靠发送前 `reorder_tool_results_after_calls` 兜底）。
>     故进一步：**gateway 不再为 `delegate_task` eager 写框**（`_PLAN_DISPATCH_TOOLS`/delegate_plan 的 envelope
>     框仍 eager 写），改由 `_ensure_dispatch_frame` 在 child finalize **铸**框，
>     **框与 result 同 `timestamp = task.started_at`**（框先写 seq 小、result 后写 seq 大 → 按 (timestamp, seq_no)
>     严格相邻）。与 plan 子的补铸框路径**完全统一**（都是 finalize 铸、同锚 started_at）。
>
> (c) **框名保真**（`Task.origin_tool_name`）：统一走铸框后，若一律用合成名 `start_task` 会丢掉 delegate_task 的
>     真名——而 actor **确实调过** `delegate_task`，用真名才诚实。故新增瞬态字段 `Task.origin_tool_name`
>     （与 `origin_tool_call_id` 同：不入 payload/表，纯内存单会话；重放后 None → 回退 `start_task`）：
>     `delegate_task` 子赋 `DELEGATE_TASK_NAME`，`delegate_plan` 子留 None。铸框时
>     `name = task.origin_tool_name or START_TASK_NAME` → delegate_task 框带真名、plan 子带 `start_task`
>     （plan 子无 per-child 真实调用，合成叙事名才正确）。**核心无任何逻辑读此 name（仅 finalize/测试用）**，
>     故切换安全。
>
> 排序成立：框/ack（T_start=started_at，同戳相邻）< 子 body（driver 启动后才 ingest USER_PROMPT，故 > T_start）
> < finish 对（close T2）。并发多 `delegate_task` 各自成对聚在各自 started_at（串行执行下 started 递增），
> 召回原始序即 `F0,R0,F1,R1,…`，不再依赖发送前兜底。框 input 携 `{title, description}` 保真。
> 崩溃可恢复性不受影响：actor 的 LLM_RESPONSE 本就剔除 dispatch tool_call（`act._ingest_assistant_turn`），
> reconcile 只看 task 层不看此框；子未 close 则框/result 都不存在（一致，无悬挂）。

**为什么在 finalize 而非 gateway eager 写**：① gateway 写 delegate 时无法判定 same/cross（child 未创建、
`assigned_agent_id` 未定，见 `2026-06-28` §2.1 复核），finalize 处 `creator==assigned` 已知；② eager 框只能带
派发时刻，无法落在 started_at 时间线上（见上方修订）。

渲染序列（同 agent，单派发；框名 delegate_task = 真名，plan 子则为 start_task）：
```
assistant: delegate_task [id=X]              @T_start（= task.started_at；finalize 铸，名取 origin_tool_name）
tool (id=X): Task '…' started.               @T_start（同戳相邻）
[子任务胶囊 body                              @T_start+ε..T2]   ← 同 agent_id 内联
assistant: {act_recap} [finish_task id=Y]    @T2
tool (id=Y): {task_summary}                   @T2
[父续跑                                       @>T2]
```

### 2.6 跨 agent 派发：result = `outputs` + `task_summary` 拼接

父看不到子胶囊，cross_agent 的 dispatch result 是父唯一可见的子结果，**须把两部分拼在一起**：
`mem_content` = `task.outputs`（最终输出）+ `task_summary`（process report 作用）。即 `mem_content` 的
report 部分由旧的 `act_recap`/process_report **改用 `task_summary`**（`_build_memory_content(task.outputs,
task_summary)`），`task_summary` 空时回退 `act_recap`。

- `_close_one` cross_agent 分支**写法不变**——仍写 `content = f"{report_prefix}{mem_content}"`（fail 带
  `[outcome=fail]` 前缀），只是 `mem_content` 现在承载 outputs + task_summary。
- `BLACKBOARD_PUBLISH` 同样用这个 `mem_content`（也是「汇报给 parent」的通道）。
- timestamp 维持 close 时刻（@T2）——父在 `(T1,T2)` 挂起、无插入，与 delegate 天然相邻，不需 back-date。
- 其余（origin_task_id=parent、随单元 L2 折）不变。

### 2.7 相关 prompt：observer persona（ROLE.md）+ tracking 汇报

工具字段重设计后，告诉模型「往哪个槽写什么」的 prompt 必须同步改，否则 observer 仍按旧 `task_process_report` 心智填，新字段拿不到正确内容。

- **`ROLE.md`（observer persona，`purposes` act 之外的 observe/background facet 源）**：把「## task_process_report：执行过程记录」一节拆成两节——
  - **`act_recap`（始终）**：诚实复述上一段 act 做了什么（工具/产出/失败），第一人称、只管最后这一段。
  - **`task_summary`（仅收尾 / 裁决 success|fail）**：整段简洁 process report，点出重要步骤与经验，**综合已完成子任务的关键结果**；**显式声明不是最终输出**（最终输出 = actor 在 `finish_task` 的 `result`）。
  - 路径 A（`report_task_outcome`）字段清单：`task_process_report` → `act_recap` + `task_summary`（retry 留空 task_summary）。
  - 「仅生成执行总结」段（`collect_process_report`）：改为给 `act_recap`（+ 收尾段给 `task_summary`）。
- **`SOUL.md`（actor persona）**：`finish_task(result=...)` 处补一句——`result` 是给 user 看的最终成品，执行历程/过程总结由系统的观察者另记，不要把过程塞进 `result`。
- **`core/runtime.py` tracking 汇报**：依赖任务（tracking/predecessor）拿到的前序结果 `f"...result:{...}\nprocess report:{report}"`，其中 `report = tracked.process_report`（现在 = act_recap）应改用 `tracked.task_summary or tracked.process_report`——这也是「汇报给依赖方」，要给综合 process report 而非单段 recap。
- **两份 ROLE.md/SOUL.md 副本同步改**（dev `resources/` + 打包 `packaging/default_data/`），与现有 persona 文件维护约定一致。

---

## 3. 不动的部分

- dispatch 对 / finish 对的 **fold 命运**（L1 黑盒保留、L2 同 `origin_task_id` 一并折，`2026-06-28` §2.2）。
- task-resident body 召回（`recall_recent_by_agent`，按 agent_id 跨 task）、`_supersede_final_raw_segment`
  长任务折末 raw 段（`2026-06-28-task-resident-capsule`）。
- `report_task_outcome` 的 success 护栏（无 outputs 不许判 success → retry）、`task_reviews` reopen 流程。
- `legacy_dispatch.normalize_legacy_dispatch` 适配层、enum 不物理删（`2026-06-28` §5）。
- 段摘要 role=assistant / 不套包装（`2026-06-27`）；`AGENT_COMPACT_SUMMARY`（B 类）全链路。

---

## 4. 风险 / 边界

- **R1 · `act_recap` 的 retry 兼容**：`task.process_report` 现由 `task_process_report` 填、驱动 Current
  Progress 与 `_progress_already_in_compact` 去重。改填 `act_recap` 后语义等价（都是「上轮做了啥」），但须
  测试 max_turns 复用 process_report 作 `TASK_COMPACT_SUMMARY` 的去重路径不破。
- **R2 · `task_summary` 依赖 LLM 产出**：success/fail 时若 observer 漏填 `task_summary`，finish 对 tool 槽
  与 `mem_content` 的 report 部分都会缺。兜底（`_finish_tool_text`）：`task_summary` 空 → 回退 `act_recap` →
  再空给占位（`(本段无更多总结)`/`(无最终产出)`）。**tool 槽不掺 outputs**（outputs 在 finish_task call 入参，
  不重复）。绝不产空 tool result（避免 400 / 空回合）。`mem_content` 同样 `task_summary or act_recap`。
- **R3 · 同 agent delegate 回合找不到（边界）**：§2.5 step2 依赖在 parent_scope 召回到那条 delegate 回合取
  timestamp。若缺失（异常/存量），回退 `now_utc()` 并 log（最佳努力，至多退化成旧 split 行为，不崩）。
- **R4 · 存量数据**：旧的「supersede delegate + close-time result」会话不迁移；新代码只改新写入路径。旧
  会话仍按旧形态渲染（可能仍有 1.1 的 split），但 `llm_gateway` 的 `reorder_tool_results_after_calls` /
  `drop_dangling_tool_calls` 兜底使其不报 400（降级而非崩）。
- **R5 · `_replace_finish_report` 匹配**：从「匹配 `Process Report:` 文本」改为「按 `tool_call_id` +
  `origin_task_id` 定位两条」，更稳；找不到时维持 best-effort log + no-op（`2026-06-28` §3.6 不变）。

---

## 5. 测试计划

全部 `uv run pytest`（pyproject 已配 `pythonpath=["."]`；provider 测试缺 `psutil`/`pyyaml` 时
`--with psutil --with pyyaml`）。

- **T1 · 工具字段**：`report_task_outcome` 产 `act_recap` + `task_summary`（success/fail），retry 时
  `task_summary` 可空；`task.process_report == act_recap`。`collect_process_report` 同字段。
- **T2 · finish 对两段**：`_synthesize_dispatch_pair` 写 `assistant{act_recap}+finish_task` /
  `tool{task_summary}`，二者同 `(timestamp, seq相邻)`、同 `tool_call_id`。
- **T3 · 同 agent 派发保留 + 相邻**：same_agent close 后 delegate 回合**仍在**（未 superseded）、有一条配对
  static result，其 `timestamp == delegate 回合 timestamp`；经 composer 归并后 `delegate → result` 紧邻、
  子 body 排在 result 之后；端到端无 400（断言 messages 中每个 tool 紧跟其 assistant tool_calls）。
- **T4 · 子任务的子任务**：两层 same_agent 嵌套，最内层（非 root、无 background observe）的 finish 对来自
  inline verdict 的两段，形态正确。
- **T5 · own-root 替换**：background close observe 产新两段，`_replace_finish_report` 同时换 assistant+tool；
  A1 时序两个方向（finalize 先到 / bg 先到）都正确。
- **T6 · 跨 agent result = task_summary**：cross_agent dispatch result content == `task_summary`、
  fail 带 `[outcome=fail]`、@T2 与 delegate 相邻。
- **T7 · 非 close 段边界**：interrupt/plain_text 边界用 `act_recap` 作 `TASK_COMPACT_SUMMARY`，`task_summary`
  不参与；段摘要 role=assistant（`2026-06-27` 回归不破）。
- **T8 · R1/R2/R3 兜底**：max_turns 去重路径绿；`task_summary` 空时 tool 非空回退；delegate 回合缺失时
  回退 now_utc + log、不崩。
- **回归**：`test_capsule_golden` / `test_subtask_nesting` / `test_close_task` / `test_open_closed_recall` /
  `test_root_subtree_fold` 按新形态更新；`test_gateway_tool_adjacency` 仍绿。

---

## 6. 受影响文件清单（实现期由 writing-plans 排序）

- `core/orchestrator/control_capability.py` — 两个 observe 工具字段重设计（§2.1）。
- `core/loop/steps/observe.py` — `Verdict` 增 `task_summary`、verdict 装配（§2.2）。
- `core/loop/steps/finalize.py` — same_agent 保留+配对静态 result（§2.5）、`_synthesize_dispatch_pair`
  两段化（§2.3）、mem_content report 部分改 task_summary（§2.6）。
- `core/loop/steps/background_observe.py` — close 携两字段、`_replace_finish_report` 换两条、A1 槽二元组（§2.4）。
- `core/assembler/composer.py` — observe / background_observe cue 要两字段 + 综合子任务（§2.2）。
- `core/runtime.py` — tracking/predecessor 汇报 report 部分用 `task_summary`（§2.7）。
- `resources/agents/default/ROLE.md` + `packaging/default_data/agents/default/ROLE.md` — observer persona 两段（§2.7）。
- `resources/agents/default/SOUL.md` + `packaging/default_data/agents/default/SOUL.md` — actor finish_task result 澄清（§2.7）。
- 测试：control 工具单测、observe verdict、finalize（同/跨 agent + 嵌套）、background A1、composer cue、
  capsule golden 与子任务嵌套回归。

> 同步约定（memory：上游 wefta→weft）：核心改动在 core（落 `ctx-weft`），若需回灌上游 LoomeX-00 按既有
> 同步流程处理；ROLE.md/SOUL.md persona 是 host 侧 default_data，**不随 core 同步**（上游有自己的 persona）。
> host postgres provider 不涉及（本设计不改 memory 写入 role/layer）。

## 7. 验收

- 同 agent 派发：delegate 回合保留、配对 static result「任务派发成功，以下是执行记录：」紧随其后（同
  timestamp）、子 body 内联其后；finish 对 `assistant{act_recap}` + `tool{task_summary}` 收尾；端到端无 400。
- finish 对两段分工落地：assistant 诚实复述上一段 act、tool 承载整段综合总结（含子任务结果）。
- 非 root 子任务（含子任务的子任务）的 finish 对经 inline `report_task_outcome` 两字段正确成形。
- 跨 agent dispatch result（及 blackboard / tracking 汇报）承载 `outputs` + `task_summary`。
- observer ROLE.md 两份副本均含 `act_recap` / `task_summary` 两节、无 `task_process_report` 残留；SOUL.md
  澄清 `result` = 给 user 的最终输出；runtime tracking 汇报用 `task_summary`。
- `uv run pytest` 全绿；相关 capsule spec 复现会话回归不破。
