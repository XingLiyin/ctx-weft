# 交互保留型 root 自经验胶囊（交错时间线 + finish_task 承载 + root 后台 observe）

- 日期：2026-06-26
- 范围（均在 `ctx_weft/` 下）：
  - `core/loop/steps/finalize.py`（`_synthesize_dispatch_pair` 重写为交错时间线 + finish 对）
  - `core/loop/steps/observe.py`（`_should_use_llm` root 分支 + 新增 root 后台 observe；`_maybe_compact_task` 保持同步）
  - `core/loop/steps/compact.py`（`summarize_for_compact` 做成 user-aware；`apply_compact(TASK)` 调用方语义）
  - `providers/memory_blackboard/in_memory.py` + `host/providers/memory/postgres.py`（`apply_compact(TASK)` user-aware 折叠）
  - 新增 `core/loop/steps/background_observe.py`（仿 `recognize_intent.py` 的 fire-and-forget）
  - `core/assembler/sources/agent_recall.py`（finish 对的 `AGENT_CONVERSATION_TURN` 渲染，已基本支持，验证/补全）
- 关系 spec：
  - **修改** `docs/superpowers/specs/2026-06-26-root-experience-summary-fold-design.md` 的 §2.1：
    胶囊形态从 `[user][assistant summary][delegate][result]` 演进为本文的交错时间线 + finish 对；
    且其「四件套共享 `now_utc()`」的时间戳方案改为「保留原始 timestamp」（本文 §3.3 step 4，
    连带 §2.2 的 `anchor_ts` 扩到保留胶囊全部元素）。
  - **复用** 该 spec 的 §2.2（fold_root_experience 折叠对齐）、§2.3（角色/轮次约束）、§2.4（compaction summary 渲染期包装）、§2.5（gateway `ensure_leading_user`）、§2.6（current-message 渲染期框架）——本文不改这些，只在它们之上扩展。
  - 底层机制 spec：`docs/spec/06-memory-layers-and-compaction.md`。

---

## 1. 问题

root 任务 close 时由 `_synthesize_dispatch_pair`（finalize.py）合成「自经验胶囊」镜像进 agent 层。
现状胶囊（含相邻 spec 刚修过的版本）只承载 **一条** 原始用户消息（`task.user_prompt`）+ 最新一条
compaction summary + 一个 `delegate_task` 黑盒对：

```
[user]      task.user_prompt            ← 仅最开始那一条
[assistant] <task_compact_summary>      ← 仅最新一条
[assistant] delegate_task(tool_call)
[tool]      mem_content
```

**任务存活期间发生的一切交互都丢了**：HITL `ask_human` 的人类回复（suspend.py 注入的 `USER_PROMPT`）、
用户打断后追加的指令（runtime.py `_inject_user_reply` / act.py 软打断 park）、编辑式打断的撤回轨迹——
这些 `USER_PROMPT` 在 close 的 `_supersede_own_conversation` 里被全量 supersede，胶囊里一条不剩。
agent 回看自己的经验时，只看到「最初要什么 + 最后交了什么」，中间「问过什么、被纠偏过什么、何时转向」
全部消失。

### 1.1 期望

胶囊保留**交互信息**（所有用户消息逐字、按时间线排布），只把 **LLM 自主处理段** 压成摘要。即：
保留「谁说了什么、何时插入」的轮次结构，只压「两条用户消息之间 LLM 干了啥」。

---

## 2. 核心不变量

1. **用户消息永不折叠**：`USER_PROMPT`（原始诉求 + HITL 回复 + 打断注入 + 编辑式打断的 edit-note）
   是时间线锚点，task 层压缩与 close 胶囊合成都**不 supersede、不并入摘要**。
2. **只压 LLM 处理段**：相邻两条用户消息之间的 `LLM_RESPONSE`/`TOOL_RESULT` 折成一条段摘要。
3. **胶囊 = 交错时间线**：`[user 锚点][段摘要][user 锚点][段摘要]…[finish 对]`。
4. **最终段由 finish 对承载**：止于 `finish_task` 的最后一段不单出段摘要，由
   `finish_task(result=outputs)` + `tool: Process Report` 这对承载（Approach B + finish_task）。
5. **段摘要来源按 token 压力分流**（§2.2）：机械退出（max_turns/context_limit）同步压；
   其余段边界（HITL/打断/finish）由 root **后台异步 observe** 产出（§2.3），close 时强一致（方案乙）。
6. **首条恒 user**：胶囊以 user 锚点打头（复用相邻 spec §2.3 / §2.5 兜底）。

---

## 3. 设计

### 3.1 task 层压缩 user-aware（`apply_compact(TASK)` + 两个触发点）

`apply_compact(scope, summary, keep_last, ctx, layer=TASK)` 现状：召回
`TASK_COMPACT_TYPES = [USER_PROMPT, LLM_RESPONSE, TOOL_RESULT]`，按 seq 留最后 `keep_last` 条、
其余折一条 `TASK_COMPACT_SUMMARY` 并 supersede——**不分类型**。改为 user-aware：

- **USER_PROMPT 永不进 supersede 集合、永不并入摘要**（不变量 1），即使它比 `keep_last` 窗口更老。
- `keep_last` 只对 `LLM_RESPONSE`/`TOOL_RESULT` 计数与保留。
- 折叠后 task 层 = `[user₀][段摘要₀][user₁][段摘要₁]…[保留的最近 LLM/TOOL 窗口]`。

两个触发点按 token 压力分工（机制见 docs/spec/06 §7，现状三入口见下表），**user-aware 程度不同**：

| 触发点 | 代码位置 | 折法 | 段摘要来源 |
|---|---|---|---|
| **max_turns 退出** | `observe._maybe_compact_task`（仅 task 层） | **per-segment user-aware**：按 user 锚点切段，每段一条段摘要 | 优先 `verdict.summary`，否则 `summarize_for_compact`（user-aware） |
| **context_limit / token 比例 / 消息 delta** | `prepare._should_compact → compact._compact_scope`（task+agent） | **保锁点、不做 per-segment**：留全部 USER_PROMPT，LLM 处理折**一条**粗摘要（可跨段） | `summarize_for_compact`（专用压缩 LLM） |

> context_limit 路径「不做 per-segment」是刻意取舍：它是 token 救火，粗摘要可跨多个 user 段，
> 但**绝不 supersede USER_PROMPT**——锚点全留，close 时仍能重建交错时间线（只是该粗摘要那一段
> 粒度粗）。详见决策记录（讨论 2026-06-26）。

段摘要的 **timestamp 落在所概括段的区间内**（最旧被折回合的 timestamp），保证 composer 按
`(timestamp, seq_no)` 归并时段摘要排在对应 user 锚点之后、下一 user 锚点之前。

### 3.2 root 后台异步 observe（方案乙）

**根因**：`observe._should_use_llm`（observe.py:281-295）对 root（`parent_task_id is None`）返回 False
→ 走 `_rule_observe`，只产出 `"Task completed."` 薄文本，**没有可用段摘要**。唯一例外是 max_turns
（line 289 强制 LLM）。所以 root 的正常段 / 交互段在现状下没有摘要源 → 胶囊只能留 raw。

**方案**：新增 `core/loop/steps/background_observe.py`，仿 `recognize_intent.py` 的 fire-and-forget：
快照 loop state → `asyncio.create_task` 后台跑一次 observe 风格 LLM 摘要 →
完成后对该段做 §3.1 的 user-aware 压缩（折 raw、留锚点、写 `TASK_COMPACT_SUMMARY`）→
`tm.track_background(task)` 注册（task_manager.py:78-81，使 session 关闭前 `gather`，
task_manager.py:584）。异常吞掉（best-effort，降级见 §3.6）。

触发分流（不变量 5）：

| 段边界 | 同步 / 后台 | 理由 |
|---|---|---|
| max_turns / context_limit（机械、token 压力） | **同步**（保持现状 `_maybe_compact_task` / `_compact_scope`） | 必须先减 token 才能续跑 |
| 用户交互边界（HITL `ask_human` / 打断 → suspend） | **后台异步** | 用户正在交互，有的是时间 |
| **纯文本暂停（`wait_for_user`，交互式回合边界）** | **后台异步** | 交互式聊天每轮天然边界——agent 散文回复后让位用户；不压则交互式 root 每轮处理段留 raw（2026-06-27 补） |
| `finish_task`（正常收尾） | **后台异步**，但 close 同步 await（§3.3） | 用户已拿到 finish_task.result |

**并发约束**：同一 task 至多一个在跑的后台 observe；段 N+1 边界到来时若段 N 的后台摘要还没好，
**排队**（不并行），避免两个摘要器抢同一 task 层 memory。

**非 root 不变**：`parent_task_id` 非空的子任务仍走原 LLM observe（要给 parent 上报 verdict），
不触发后台 observe。

### 3.3 close 胶囊合成（`_synthesize_dispatch_pair` 重写，方案乙）

close 在 `_close_one` step 2 调 `_synthesize_dispatch_pair`（早于 step 3 全量 supersede）。重写为：

1. **方案乙 await**：若本 task 有挂起的后台 observe（最后一段的摘要 / Process Report），
   先 `await` 它完成（强一致；用户早已通过 finish_task.result 拿到答复，此 await 对用户无感）。
2. **快照幸存 task 对话**：在 task scope 召回未 superseded 的
   `[USER_PROMPT, TASK_COMPACT_SUMMARY, LLM_RESPONSE, TOOL_RESULT]`，按 seq 升序，逐条镜像成
   agent 层 `AGENT_CONVERSATION_TURN`（metadata `origin_task_id = task.id`）：
   - `USER_PROMPT` → role=user（逐字）
   - `TASK_COMPACT_SUMMARY` → role=assistant（段摘要；胶囊内 assistant 自述，§2.4 不套包装）
   - 残留 `LLM_RESPONSE`/`TOOL_RESULT`（理论上方案乙后最终段已被后台 observe 压掉、应为空；
     若降级未压则原样镜像，见 §3.6）
3. **追加 finish 对**（最终段承载，不变量 4）：均为 agent 层 `AGENT_CONVERSATION_TURN`，
   **携 metadata `origin_task_id = task.id`**（与镜像回合一致，使 `fold_root_experience`
   §2.2 能按 `origin_task_id` 把整个胶囊连同 finish 对一并 supersede、不落单）：
   - role=assistant，`tool_calls=[{id, name=control__finish_task, input={result: task.outputs}}]`
   - role=tool，`tool_call_id` 配对，content = `Process Report: {verdict.summary}`
     （fail 时前缀 `[outcome=fail] `，见 §3.5）
4. **时间戳：保留原始 timestamp（修改相邻 spec §2.1）**。相邻 spec §2.1 让胶囊四件套共享
   `now_utc()`；但本设计要把 **user 锚点、段摘要、子任务派发对、嵌套子胶囊**（§3.9）按真实
   发生序交织，故镜像回合 **沿用其来源事件的原始 timestamp**（user 锚点取 USER_PROMPT 的 ts、
   段摘要取段区间 ts），唯独 close 合成的 finish 对取 `now_utc()`（落在全部之后）。composer 先
   timestamp 再 seq_no（composer.py:577）自然得到交错时间线。
   - **fold 锚点调整（§2.2 依赖）**：`fold_root_experience` 的 `anchor_ts = min(保留胶囊全部
     元素 ts) − 1µs`（不再只看 `TASK_DISPATCH_RESULT`），保证 compact summary 排在保留胶囊之前。
     fold 仍按 `origin_task_id` / `child_task_id` 识别胶囊归属，supersede 不受 timestamp 影响。
   - 相邻 spec §2.1 担心的「user 回合甩到过去」在本设计中正是**期望行为**（交互锚点本就该按
     原时间排）；其 `−1µs/−2µs` 错位坑不复存在（不再人为错位）。

子任务在胶囊里的处理见 **§3.9**（同 agent 平铺嵌套 / 跨 agent 黑盒），取代原先「dispatch 对
一律 GC」的设想。

### 3.4 finish_task 承载（为何是「合成」而非「复用」）

`finish_task` 在 `SILENT_TOOLS`（capability_gateway.py:53-57），其结果不入 task 对话，canonical
出口是 `task.outputs`；`_ingest_assistant_turn` 还把它从 `LLM_RESPONSE.metadata.tool_calls` 剔除
（act.py:276-277），故 task 层**没有 finish_task 的 tool_call/result**，无悬挂。因此胶囊里的 finish 对
在 close **合成**（与现状合成 delegate 对同构，只换工具名 + result/report 内容）。

### 3.5 outcome=fail

胶囊对 fail 任务一样合成 finish 对：assistant `finish_task(result=task.outputs)` +
tool `[outcome=fail] Process Report: {summary}`。`task.outputs` 为空（fail 常无终稿）时
result 用 `(无最终产出)` 占位，保证 tool_call 配对、不悬挂。

### 3.6 后台 observe 失败降级

后台 observe 的 LLM 调用失败（自愈耗尽）时：
- 该段 **不写段摘要**，保留 raw `LLM_RESPONSE`/`TOOL_RESULT`（不阻断主流程，胶囊里该段呈现为 raw）。
- close 胶囊合成仍照常进行（§3.3 step 2 会把残留 raw 原样镜像）——**胶囊永远能合成，绝不因摘要
  失败而崩或丢锚点**。与相邻 spec「summary 失败退化为占位/截断」一致。

### 3.7 打断处理

复用现有打断机制（act.py / runtime.py），胶囊侧只是「锚点 + 段摘要」自然落位：

- **② 流中打断**（已吐 token）：半截 assistant（`interrupted=True`）+ cancelled/INTERRUPTED 合成
  `TOOL_RESULT` 都属于「被打断那一段的 LLM 处理」，被段摘要折掉、**不进胶囊**；段摘要文本里带
  「（本段被用户打断）」标注（后台 observe 的 prompt 读 `interrupted` metadata 后写入）。
  打断后的用户新指令是一条 `USER_PROMPT` → 胶囊里的下一个 user 锚点。
- **① 编辑式打断**（未吐 token）：`interrupt_edit_note`（act.py:493）把「上一条 X 取消，改为 Y」
  合成一条 `USER_PROMPT`。它和原始 `USER_PROMPT` 都是锚点、相邻保留，撤回轨迹完整；二者之间无
  处理段（打断在出 token 前）。

### 3.9 子任务在胶囊里的处理（同 agent 嵌套 / 跨 agent 黑盒）

子任务（`delegate_task`/`delegate_plan` 派生）一律是独立 task、自带 task 层对话；parent SUSPEND 等它，
并在 parent agent 层落 `TASK_DISPATCH`（gateway，dispatch 工具）。`use_subagent` 决定 assigned：
`False` → 同 agent（creator==assigned）；`True` → 跨 agent（spawn 子 agent，creator≠assigned）。
**子任务执行细节从不在 parent 的 task 层**（在 child 自己的 task 层），故 parent 胶囊里它只能是派发对，
无法折进 parent 段摘要。本设计按「是否同 agent」分两种承载（**取代原 §4.4 黑盒一刀切**：黑盒只对
跨 agent 成立，同 agent 透明）：

**同 agent（`use_subagent=False`）· 透明平铺嵌套（胶囊套胶囊）**
- delegate 对**正常完成**：`TASK_DISPATCH` + `TASK_DISPATCH_RESULT(content="Sub-task 'X' scheduled.")`
  ——即 `delegate_task` 工具的真实返回（`control_capability.py:204`），timestamp = 派发时刻。
  **不**把 child 的 mem_content 塞进该 result。
- child 的「交互保留 + tool 压掉」胶囊（与 root 同套逻辑：user 锚点 + 段摘要 + 自己的 finish 对）
  **平铺嵌入** delegate 对之后：写成 `AGENT_CONVERSATION_TURN`（`origin_task_id = child.id`），
  保留各自原始 timestamp → 自然落在 delegate 对之后、parent 后续段之前。
  注：同 agent ⟹ creator==assigned ⟹ **parent 与 child 共用同一 agent 层 scope**
  （scope_key 按 `agent_id`，docs/spec/06 §8）。故子胶囊回合本就落在 parent 胶囊所在的同一 scope，
  靠 `origin_task_id` 区分归属，**不是跨 scope 搬运**。
- 合法性：子胶囊回合都在 `TASK_DISPATCH_RESULT`（user 消息）**之后**，不夹在 tool_use/tool_result 之间；
  连续 user 由 API 合并（§2.3）。
- child **无**独立胶囊（经验已嵌进 parent）。child 自身 task 层对话在其 close 时照常 supersede
  （已被压进嵌入的子胶囊）。
- **递归**：同 agent 孙任务再平铺嵌一层，深度不限（每层都是已压缩胶囊，体量有界）。
- 实现：同 agent child close 时，除写 `TASK_DISPATCH_RESULT("…scheduled")` 外，跑一遍**自己的胶囊
  合成**（§3.3，含 §3.2 后台 observe 压段），产物落 parent scope 而非 child 自己 scope。

**跨 agent（`use_subagent=True`）· 黑盒 + 独立子胶囊（不变）**
- delegate 对的 `TASK_DISPATCH_RESULT` = child bubble 的 `mem_content` = **`result(outputs) + Process
  Report`**，timestamp = child close。parent 看不到 child 内部。
- child 对其执行 agent 是 own root（`is_own_root=True`）→ 在**子 agent scope** 合成自己的完整胶囊（§3.3）。
- 一致性：parent 黑盒 result = 子 agent 胶囊末尾 finish 对的产出（outputs + Process Report）= 同一交付物。

**`_gc_subtree` 调整**：
- 跨 agent child：parent 的 `TASK_DISPATCH`/`RESULT` 派发对**保留**（不再随 `_gc_subtree` supersede）；
  仅 GC child 自身 task 层残留（其经验已在子 agent 胶囊）。
- 同 agent child：保留 delegate 对 + 嵌入的子胶囊 `AGENT_CONVERSATION_TURN`；GC child 自身 task 层 raw。
- 这两类「parent 直属 child 的派发对 / 嵌入子胶囊」由 parent close 时的 `fold_root_experience`
  按 `child_task_id`/`origin_task_id` 随 parent 胶囊整体管理（§2.2）。

**边界 · 短同 agent 子任务**：短 child 不 bubble（`do_bubble = cross_agent or (same_agent and not
short)`，finalize.py），其 `TASK_DISPATCH` 未配对 → 按现有「未配对 dispatch 隐去」（agent_recall.py:118）
处理；嵌套子胶囊在短 child 被 `close_finished_short_tasks` 强制 close 时再合成嵌入。

### 3.10 不动的部分

`_close_one` 整体结构、step 3 全量 supersede（task 层照清，幸存对话已镜像进胶囊）、bubble 的跨 agent 路径、
相邻 spec 的 §2.2（除 anchor_ts 扩到全元素，§3.3）/§2.3/§2.4/§2.5/§2.6。

---

### 3.11 agent 层压缩适配新胶囊格式（2026-06-27 补）

**背景**：§3.3 让 root 胶囊变成纯 `AGENT_CONVERSATION_TURN`（不再有合成的
`TASK_DISPATCH_RESULT(parent=None)`）。但 agent 层压缩 `_count_root_residues` /
`fold_root_experience`（compact.py）历史上靠「`TASK_DISPATCH_RESULT` 且 `parent_task_id is None`」
识别一个已结束 root 经验单元 → 新格式下数到 0 → **agent 层压缩永不触发、root 胶囊无限增长**。
故 fold 的「识别折叠单元」一步必须改判据（生成 `AGENT_COMPACT_SUMMARY` 总结那一步不变）。

**判据改为 `parent_task_id`**：`_synthesize_dispatch_pair` 给它写的**每个** `AGENT_CONVERSATION_TURN`
（镜像回合 + finish 对）metadata 加 `parent_task_id = task.parent_task_id`（零额外参数，三种调用
天然正确：root 自身→None；同 agent 内嵌子胶囊→root.id；cross-agent 子胶囊（写在自己 agent scope）
→root.id）。

**fold 识别（每个 agent scope 内独立跑）**：
- 把本 scope 的 `AGENT_CONVERSATION_TURN` 按 `origin_task_id` 分组 = 一个个胶囊；每组记其 `parent_task_id`。
- `S` = 本 scope 所有 origin_task_id。
- **顶层折叠单元** = `parent_task_id is None` **或** `parent_task_id ∉ S`。
  （OR 子句让 cross-agent 子胶囊在**自己** scope 里成为顶层单元——它的 parent=root 不在本 scope。）
- `_count_root_residues` = 顶层单元个数（取代数 `TASK_DISPATCH_RESULT(parent=None)`）。
- 超过 `keep_last` → 按单元最早回合 ts 排序，折最老的若干顶层单元；每个折叠单元经 `parent_task_id`
  链展开其后代组（同 agent 内嵌子胶囊）一并折；**配对的 cross-agent 派发对**（`TASK_DISPATCH_RESULT`
  的 `parent_task_id ∈ 折叠 origin 集合` + 按 `tool_call_id` 配的 `TASK_DISPATCH`）一并 supersede。
- `anchor_ts` = 保留单元全部记录（回合 + 其派发对）最早 ts − 1µs（与 §3.3 step4 同思路）。
- 旧的 `AGENT_COMPACT_SUMMARY` 并入新摘要（不变）。

**取代** 原先「复用相邻 spec §2.2」的设想中「靠 `TASK_DISPATCH_RESULT` 识别 root 残留」部分；
§2.2 的「按 origin_task_id 连带 supersede 不落单」「anchor 排在保留胶囊前」思路保留并推广。

## 4. Prompt 格式样例（验收基准）

基准场景「把 auth 从 session 改成 JWT，并补测试」。图例：`逐字`=USER_PROMPT 原文锚点 /
`段摘要`=TASK_COMPACT_SUMMARY（后台或同步 observe）/ `合成`=close 合成的 finish 对。

**情况 1 · 单段正常 finish**
```
[user]      把 auth 从 session 改成 JWT，并补测试                       ← 逐字
[assistant] tool_calls=[ control__finish_task(result="已切到 JWT：三处改完，8 测试全过") ]  ← 合成
[tool]      Process Report: 成功。读 session.py/middleware.py 确认 3 处使用，逐处改 jwt，补 8 测试。← 合成
```

**情况 2 · 中途一次 HITL**
```
[user]      把 auth 从 session 改成 JWT，并补测试                       ← 逐字
[assistant] 〔段①〕读完 auth 现状，发现 refresh 存储方式没定，遂确认。   ← 段摘要（后台 observe@HITL 边界）
[user]      refresh 用 redis，有效期 7 天                              ← 逐字（HITL 回复锚点）
[assistant] tool_calls=[ control__finish_task(result="…") ]            ← 合成
[tool]      Process Report: 成功。按 redis+7d 实现 refresh，3 处改完。   ← 合成（覆盖末段）
```

**情况 3 · ② 流中打断**
```
[user]      把 auth 从 session 改成 JWT，并补测试                       ← 逐字
[assistant] 〔段①·被用户打断〕正用 PyJWT 写 login() 签发，写到一半被打断。← 段摘要（标注被打断）
[user]      别用 PyJWT，用 authlib                                     ← 逐字（打断后新指令锚点）
[assistant] tool_calls=[ control__finish_task(result="…") ]            ← 合成
[tool]      Process Report: 成功。改用 authlib 重写签发/校验，3 处改完。  ← 合成
```

**情况 4 · ① 编辑式打断**
```
[user]      把 auth 模块从 session 改成 JWT                            ← 逐字（原始）
[user]      (上一条已取消) 改为：把 auth 改成 OAuth2，不要 JWT           ← 逐字（edit-note 锚点）
[assistant] tool_calls=[ control__finish_task(result="…") ]            ← 合成
[tool]      Process Report: 成功。按 OAuth2 实现…                       ← 合成
```

**情况 5 · 综合（max_turns + HITL + 多段，三种摘要来源）**
```
[user]      重构整个 auth 体系：JWT + OAuth2 + 审计日志                 ← 逐字
[assistant] 〔段①〕梳理现有 auth，列 12 个改动点，先动 JWT…             ← 段摘要（同步·max_turns）
[assistant] 〔段②〕JWT 完成，转 OAuth2 时拿不准用哪些 provider。        ← 段摘要（后台 observe@HITL 边界）
[user]      OAuth2 用 Google + GitHub 两家                            ← 逐字（HITL 回复锚点）
[assistant] 〔段③〕接入 Google/GitHub OAuth，写审计日志中间件…          ← 段摘要（后台 observe@段边界）
[assistant] tool_calls=[ control__finish_task(result="…") ]            ← 合成
[tool]      Process Report: 成功。JWT + 双 OAuth + 审计全部落地，28 测试通过。← 合成（覆盖末段）
```

**情况 6 · 胶囊在完整 agent prompt 里的位置（与当前任务活对话交织）**
```
[user]      重构整个 auth 体系：…   〔段①〕… [user] OAuth2 用 …  〔段③〕…   ┐ 过去经验（本胶囊）
[assistant] finish_task(...)  [tool] Process Report                          ┘
[user]      ## Current Task: 给 auth 加限流                                  ┐ 当前任务（task 层活对话）
[assistant] 先看现有 auth 中间件…  tool_calls=[read_file]                     ┘
```

**情况 7 · 同 agent 子任务（平铺嵌套，胶囊套胶囊）**——场景 root「研究 X 并写报告」委派「收集数据」给自己
```
[user]      研究 X 并写报告                                    ← 逐字
[assistant] 〔段①〕查现状、定大纲，决定先收集数据。             ← 段摘要
[assistant] tool_calls=[ control__delegate_task(title="收集数据") ]  ← 派发标记
[tool]      Sub-task '收集数据' scheduled.                     ← 工具真实返回
[user]      收集数据：2020-2024 X 领域                         ┐
[assistant] 〔子段①〕检索 5 源、下载…                          │ 子胶囊平铺嵌入
[user]      优先 2023-2024                                     │（origin_task_id=child；交互可见，tool 夹掉）
[assistant] 〔子段②〕过滤清洗…                                 │
[assistant] tool_calls=[ control__finish_task(result="收集到 5 份数据集") ] │
[tool]      Process Report: 成功，覆盖 5 年。                   ┘
[assistant] 〔段②〕基于数据写报告初稿…                          ← parent 继续
[assistant] tool_calls=[ control__finish_task(result="报告完成：…") ]  ← parent 收尾合成
[tool]      Process Report: 成功。报告含趋势/对比/结论三部分。
```

**情况 8 · 跨 agent 子任务（黑盒 + 独立子胶囊）**
```
(a) parent 胶囊（root agent scope）：派发对 tool 结果只 result+report
[assistant] tool_calls=[ control__delegate_task(title="收集数据", use_subagent=True) ]
[tool]      收集到 5 份数据集，已清洗。
            Process Report: 成功，覆盖 5 年。                   ← 只 result+report，无内部交互

(b) 子 agent 自己的胶囊（子 agent scope，独立，parent 看不到）
[user]      收集数据：2020-2024 X 领域                         ← 逐字（= 子任务 user_prompt）
[assistant] 〔子段①〕检索 5 源、下载并清洗…                     ← 段摘要
[assistant] tool_calls=[ control__finish_task(result="收集到 5 份数据集") ]  ← 合成
[tool]      Process Report: 成功，覆盖 5 年。
```

---

## 5. 测试计划

全部用 `uv run pytest`（pyproject 已配 `pythonpath=["."]`）。黄金用例统一格式：
**构造 memory 事件序列 → 跑 close / 装配 → 断言重建出的 messages 序列（role + 内容要点 + tool_calls/tool_call_id 配对）**。

### A. 胶囊合成 golden（事件序列 → agent 层重建 messages）

- **A1 单段正常 finish**：task 层 = `[USER_PROMPT 原始][若干 LLM/TOOL][finish_task→SILENT]`，
  outcome=success。close 后断言 agent 层胶囊 = `[user 原始][assistant finish_task(result=outputs)]
  [tool Process Report]`，**无独立段摘要回合**，`[0]` 是 user。
- **A2 一次 HITL**：序列含「原始 USER_PROMPT → 段①处理 → （后台 observe 产 TASK_COMPACT_SUMMARY₁）
  → HITL USER_PROMPT → 段②处理 → finish」。断言胶囊 =
  `[user 原始][assistant 段①摘要][user HITL][assistant finish][tool Report]`，顺序正确、两条 user 都在。
- **A3 ② 流中打断**：段①含 `LLM_RESPONSE(interrupted=True)` + cancelled `TOOL_RESULT`；后台 observe
  产段①摘要（含「被打断」字样，由读 `interrupted` metadata 触发）。断言胶囊里
  **无** interrupted 半截 assistant、**无** cancelled tool result；段①摘要回合存在且含打断标注；
  打断后 USER_PROMPT 成为锚点。
- **A4 ① 编辑式打断**：task 层 = `[USER_PROMPT 原始][USER_PROMPT edit-note][段处理][finish]`。
  断言胶囊里两条 user 锚点**相邻保留**（原始在前、edit-note 在后），其间无段摘要。
- **A5 综合**：max_turns 同步压出段①摘要 + HITL 后台压出段②③摘要 + finish。断言胶囊三段摘要
  来源齐全、user 锚点（原始 + HITL）逐字、顺序 = §4 情况 5。
- **A6 fail 收尾**：outcome=fail，`task.outputs` 为空。断言 finish 对 = `[assistant finish_task
  (result="(无最终产出)")][tool "[outcome=fail] Process Report: …"]`，tool_call 配对不悬挂。
- **A7 多次 HITL（连续两轮问答）**：原始 + HITL₁ + HITL₂ 三条 user 锚点 + 三段摘要 + finish。
  断言全部锚点保留、交错顺序正确。
- **A8 root 委派过子任务**：root 存活期 dispatch 过子任务（agent 层 TASK_DISPATCH/RESULT，
  child_task_id ∈ 后代）。close 后断言子任务 dispatch 对被 `_gc_subtree` supersede、**不在胶囊**，
  胶囊只含 user 锚点 + 段摘要 + finish 对。
- **A9 后台 observe 被禁用/失败的降级（§3.6）**：模拟后台 observe LLM 失败，某段无 TASK_COMPACT_SUMMARY、
  残留 raw LLM/TOOL。断言胶囊**仍合成**（不崩）、user 锚点全保留、该段以 raw 回合镜像、finish 对正常。
- **A10 short task 不合成胶囊（负向）**：短叶子 task（`_is_short_leaf` True）finish。断言**不**调
  `_synthesize_dispatch_pair`、task 维持 OPEN、无 `AGENT_CONVERSATION_TURN` 写入。

### B. task 层 user-aware 压缩（provider 单测，in_memory + postgres 各一份）

- **B1 user-aware apply_compact(TASK)**：task 层 = `[user₀][llm][tool][user₁][llm][tool][llm][tool]`，
  `keep_last=2`。断言：USER_PROMPT 全留（user₀/user₁ 都在）、被折的是 LLM/TOOL、产出
  `TASK_COMPACT_SUMMARY`、被折 LLM/TOOL 被 supersede。
- **B2 老 USER_PROMPT 不 supersede**：user₀ 远早于 keep_last 窗口。断言 user₀ 仍未 superseded。
- **B3 max_turns per-segment**：两条 user 锚点之间两段 LLM 处理 → 跑 `_maybe_compact_task`。断言
  **每段一条**段摘要、各自 timestamp 落在对应段区间内、user 锚点保留。
- **B4 context_limit 粗压**：跨两个 user 段的 LLM 处理 → `_compact_scope` 粗压。断言 LLM 折成
  **一条**粗摘要（允许跨段）、**两条 USER_PROMPT 均未 supersede**。
- **B5 段摘要时间戳归位**：装配含段摘要 + user 锚点，断言 composer 按 (timestamp, seq_no) 归并出
  `[user₀][段摘要₀][user₁]…` 而非段摘要漂到锚点前。

### C. 后台 observe（方案乙）

- **C1 触发 + 注册**：root 在 HITL/finish 段边界触发 `launch_background_observe`；断言
  `asyncio.create_task` 被建、`tm.track_background` 被调（mock task_manager 断言调用）。
- **C2 finalize 强一致 await（方案乙）**：让后台 observe 故意慢于 finalize 进入；断言
  `_synthesize_dispatch_pair` **await** 了该后台 task 完成后才快照，胶囊含其产出的末段摘要/Report
  （不出现「胶囊已合成但摘要还没好」）。
- **C3 并发串行化**：同一 task 段 N 后台 observe 未完时段 N+1 边界到来；断言第二个**排队**、
  不与第一个并行写 task 层 memory（断言 memory 写入无交错损坏 / 用串行锁计数）。
- **C4 失败降级**：后台 observe LLM 抛错；断言被吞（不冒泡到主循环）、该段无段摘要、胶囊仍合成（接 A9）。
- **C5 max_turns 仍同步**：max_turns 退出**不**走后台、仍由 `_maybe_compact_task` 同步压；断言无
  `launch_background_observe` 调用、同步产段摘要。
- **C6 非 root 不触发**：`parent_task_id` 非空的子任务 finish；断言走原 LLM observe（给 parent 上报）、
  **无** `launch_background_observe`。
- **C7 交互边界触发**：HITL `ask_human` 与软打断 park 两条路径各触发一次后台 observe；断言都 fire。

### D. 打断细节

- **D1 ② 排除半截 + cancelled**：见 A3，独立断言 `interrupted` 半截 assistant 与 `cancelled`/
  `INTERRUPTED` tool result 既不进段摘要折叠保留区、也不进胶囊。
- **D2 ① edit-note 锚点**：见 A4，独立断言 `interrupt_edit_note` 产出的 USER_PROMPT 内容含
  「上一条…取消…改为」结构、且原始 USER_PROMPT 仍在。

### E. finish_task 对

- **E1 finish_task SILENT 不入对话**：跑一轮 actor 调 finish_task；断言 task 层**无**
  finish_task 的 TOOL_INVOCATION/RESULT、`LLM_RESPONSE.metadata.tool_calls` 不含 finish_task。
- **E2 合成对结构**：断言胶囊 finish 对 = assistant(`tool_calls=[{name:control__finish_task,
  input:{result: outputs}}]`) + tool(`tool_call_id` 配对, content 以 `Process Report:` 开头)。
- **E3 配对完整（无悬挂）**：装配胶囊后过 gateway `drop_orphan_tool_results`，断言 finish 对的
  assistant tool_call 与 tool result 配对存活、不被当孤儿丢。

### F. 与现有 spec 交互（回归）

- **F1 fold_root_experience 折多回合胶囊**：构造 > keep_last 个含「多 user 锚点 + 多段摘要 + finish 对」
  的已 close 胶囊触发 agent 层折叠；断言被折胶囊的**全部** `AGENT_CONVERSATION_TURN`
  （按 `origin_task_id` 命中，不论 user/assistant）+ finish 对被 supersede、不落单；保留胶囊完整；
  `AGENT_COMPACT_SUMMARY` 排在保留胶囊之前（复用相邻 spec §2.2）。
- **F2 首条恒 user**：装配整段 agent prompt，断言 `[0]` 是 user（胶囊以 user 锚点打头）；
  构造畸形（前导非 user）输入过 gateway `ensure_leading_user`，断言被兜底（复用相邻 spec §2.5）。
- **F3 包装一致性**：断言胶囊内 role=assistant 段摘要**不**带 `wrap_compact_summary` 包装前缀；
  而 task 层 `TASK_COMPACT_SUMMARY`（含 context_limit 粗摘要）在 task 对话渲染时**仍**带包装
  （复用相邻 spec §2.4，`record_to_history_block` 现状即按 type 区分）。

### G. 端到端

- **G1 全量胶囊 golden**：用情况 5 的完整事件序列跑「ingest → 后台 observe（mock LLM 返回固定摘要）
  → close → 装配」，断言渲染出的 messages 序列逐条匹配 §4 情况 5 期望（role/内容要点/配对）。
- **G2 跨 task 召回**：胶囊写入后开新 root task，断言新任务装配的 prompt 含上一胶囊（经验可被
  `recall_recent_by_agent` 召回、按 timestamp 排在当前任务活对话之前，= §4 情况 6）。

### H. 子任务（§3.9）

- **H1 同 agent 平铺嵌套 golden**：root 委派同 agent 子任务（`use_subagent=False`）含一次 HITL，
  子任务与 parent 都 close。断言重建 = §4 情况 7：delegate 对的 tool 结果 = `Sub-task 'X' scheduled.`、
  子胶囊回合（user 锚点 + 段摘要 + 子 finish 对）平铺在 delegate 对之后、parent 段②之前、顺序正确，
  子胶囊回合的 scope = parent agent、`origin_task_id=child.id`。
- **H2 同 agent 共用 scope、无独立胶囊单元**：断言同 agent 子任务的胶囊回合落在 parent/child 共用的
  同一 agent scope（按 `agent_id`）、靠 `origin_task_id=child.id` 区分；不产生「另一个 agent scope 的
  独立胶囊」（与 H4 跨 agent 在不同 agent scope 成胶囊对照）。
- **H3 递归嵌套**：同 agent 子任务再委派同 agent 孙任务；断言孙胶囊平铺嵌在子胶囊内对应位置、
  深度正确、各层 `origin_task_id` 归属正确。
- **H4 跨 agent 黑盒 golden**：root 委派跨 agent 子任务（`use_subagent=True`）；断言 = §4 情况 8：
  (a) parent 派发对 tool 结果 = `outputs + Process Report`、**无**子内部交互；(b) 子 agent scope 有独立
  完整胶囊；(c) 隔离——parent 召回**看不到**子 agent 的段摘要 / user 锚点。
- **H5 时间戳交织**：同 agent 嵌套场景，断言 composer 按原始 timestamp 把「delegate 对 → 子胶囊回合 →
  parent 后续段」交织成正确顺序（验证 §3.3 step 4 保留原始 ts）。
- **H6 `_gc_subtree` 保留派发对**：parent close 后断言直属 child 的 `TASK_DISPATCH`/`RESULT`（跨 agent）
  与嵌入子胶囊（同 agent）**未被** `_gc_subtree` supersede，仅 child 自身 task 层 raw 被 GC。
- **H7 fold 含派发对/嵌套的胶囊**：> keep_last 个含子任务的已 close 胶囊触发 `fold_root_experience`；
  断言被折胶囊连同其派发对 + 嵌入子胶囊回合（按 `child_task_id`/`origin_task_id`）一并 supersede、
  不落单；`anchor_ts` = min(保留胶囊全部元素 ts) − 1µs（§3.3）。
- **H8 边界·短同 agent 子任务**：短 child 不 bubble、`TASK_DISPATCH` 未配对；断言按「未配对隐去」
  渲染（agent_recall.py:118），强制 close 后嵌套子胶囊补入。

---

## 6. 受影响文件清单（实现期细化由 writing-plans 排序）

- `core/loop/steps/finalize.py` — `_synthesize_dispatch_pair` 重写（§3.3/§3.5），await 后台（§3.2）；
  `_close_one` 同 agent 子任务嵌套合成（§3.9）；`_gc_subtree` 保留直属派发对/嵌入子胶囊（§3.9）；
  bubble 分流（同 agent = "scheduled"、跨 agent = mem_content）。
- `core/loop/steps/observe.py` — `_should_use_llm` root 分支接后台触发；`_maybe_compact_task` 做成 per-segment（§3.1）。
- `core/loop/steps/background_observe.py`（新） — fire-and-forget + 并发串行化（§3.2）。
- `core/loop/steps/compact.py` — `summarize_for_compact` user-aware；`_compact_scope` 的 task 折保锁点（§3.1）；
  `fold_root_experience` 的 `anchor_ts` 扩到保留胶囊全部元素（§3.3 step 4）。
- `providers/memory_blackboard/in_memory.py` + `host/providers/memory/postgres.py` — `apply_compact(TASK)` user-aware。
- `core/assembler/sources/agent_recall.py` — 验证 finish 对与嵌入子胶囊（`AGENT_CONVERSATION_TURN` 带 tool_calls/tool_call_id）渲染。
- 触发后台 observe 的接线点：HITL（observe ask_human 路径 / suspend.py）、软打断 park（act.py）、finish（finalize/observe）。
- 胶囊回合时间戳从「全锚 now_utc()」改为「保留原始 ts」（§3.3 step 4，修改相邻 spec §2.1 实现）。

> 同步约定（memory：上游 wefta→weft）：本设计动 core，落 `ctx-weft`（=ctx_weft 本仓）；
> 若需回灌上游 LoomeX-00 按既有同步流程处理。

## 7. 风险 / 边界

- **后台 observe 的 token 成本**：每个 root 交互段 + finish 段各一次 LLM。可加配置开关
  （类似 `predispatch_compact_token_ratio`）按需关闭，关闭时降级 = §3.6 的 raw 保留路径。
- **崩溃恢复**：后台 observe 未完成即崩溃 → 重启后该段无段摘要、恢复为 raw（与 §3.6 同），
  不影响正确性（锚点在 task 层持久）。`track_background` 只保正常关闭路径。
- **方案乙 await 的极端慢**：后台 observe LLM 长时间不返回会拖慢 close。受 `summarize_for_compact`
  的 `stream_llm_resilient` 自愈 + 超时约束；超时耗尽走 §3.6 降级（不无限阻塞）。

## 8. 验收

- §4 全部情况的装配 prompt 与样例一致：交互锚点逐字保留、LLM 处理段被摘要、最终段由 finish 对承载。
- §5 全部测试 + `uv run pytest` 全绿。
- 相邻 spec 的复现会话回归不破（首条 user、包装、framing 不变）。
