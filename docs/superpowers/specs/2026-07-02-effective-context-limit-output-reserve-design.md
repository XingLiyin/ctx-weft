# Effective Context Limit：为输出预留余量 + 保最近/保摘要裁剪

- 日期：2026-07-02
- 状态：设计待评审
- 相关：`compact.py` / `assembler` / `budget.py`；前置排查见本会话；根因背景见 memory `v0412-uncompactable-agent-summary`

## 1. 背景与问题

装配流水线（`ContextAssembler.assemble`）用 `BudgetStrategy` 按 token 预算裁剪 block，
`token_limit = request.session.context_limit`（assembler.py:158）。而 `session.context_limit`
= LLM 声明的**模型总窗口**（runtime.py:555 `session.context_limit = llm.context_limit`），
**输入 + 输出共享、未给生成预留任何空间**。

由此 prompt 可以被允许填满整个窗口，模型没有余量生成回复；compact 场景尤甚——compact 装配
（`summarize_for_compact`）复用 act 的完整会话重建（composer 的 compact 分支 → `_build_actor_messages`），
prompt 体量≈触发压缩的 act prompt，只在末尾追加一句摘要指令。

此外当前 budget 的裁剪规则有两个与直觉相悖之处：

1. **同档按体积裁**：所有会话回合都是 `priority=3`，budget 按 `(-priority, -token_estimate)`
   排序丢弃（budget.py:47-60），即同档内**先丢 token 最大的块，而非最老的**——没有"保最近"语义。
2. **摘要与原始 raw 同档**：`AGENT_COMPACT_SUMMARY`（agent_recall.py:113）、`TASK_COMPACT_SUMMARY`、
   坍缩后的 `USER_PROMPT` 都是 `priority=3`，可能先于/连同原始 raw 一起按体积被丢——压缩产物（记忆的
   蒸馏）不该比它压缩的 raw 更早被裁。

## 2. 目标 / 非目标

**目标**
1. 为 LLM 输出预留余量：装配预算按 `effective_limit = context_limit − reserved_output_tokens` 裁剪。
2. reserve 复用 LLM 自身输出能力 `max_output_tokens`（host 可配，见 §4.1），按模型预留。
3. 所有 compact 相关 token 计算（触发阈值、压缩目标、级间累减判据）统一按 `effective_limit`。
4. budget 裁剪引入"保最近 / 保摘要"语义（§4.2）。
5. compact 装配时按 `effective_limit` 强制丢弃可裁槽位数据以塞进窗口。

**非目标（本次明确不做）**
- **不改 token 估算口径**。`estimate_tokens = len//4`（utils.py:37）保持不变。这意味着对中文/JSON
  的系统性低估（2~4×）仍在：本方案用 reserve 缓冲 + 强制裁**缓解**溢出，但**不根治**——CJK 极端场景下
  按估算裁到 `effective_limit`、真实 token 仍可能超窗。已知并接受（见 §7）。
- 不做块内截断兜底（见决策 §3-B）。

## 3. 已定决策

| # | 决策 | 结论 |
|---|---|---|
| 估算 | 是否本次修 `estimate_tokens` | **否**，纯限额算术；估算口径不动（非目标）|
| reserve 归属 | reserve 值来源 | **复用 `LLMClient.max_output_tokens`**，按 LLM 自身能力预留 |
| B 兜底 | priority-0 结构地板仍超 `effective_limit` 时 | **抛 `ContextOverflowError`**，不块内截断（错误显式，绝不静默发超限 prompt）|
| A 作用域 | 新裁剪规则作用域 | **全局**（act/observe/compact 共享 budget）— 已确认 |

## 4. 设计

### 4.1 reserve 的数据模型与 plumbing

reserve 语义 = 该会话所选 LLM 的 `max_output_tokens`。它已存在于 host 侧：
- `LLMClient.max_output_tokens`（协议 llm.py:196）
- host env `IPMC_LLM_MAX_OUTPUT_TOKENS` 默认 `8192`（llm_provider.py:83）
- 可按模型在 API/DB 配（`ModelConfig.max_output_tokens`，llms.py:74 回退 8192）

budget 只拿得到 `request.session`，agent/loop_guard 不持有 LLMClient；因此 reserve 须像
`context_limit` 一样**落到 Session / LoopGuard 上**，沿同一批设置点镜像赋值：

**新增字段**（默认 `8192`，与 env 默认一致；`0` 表示不预留）：
- `Session.reserved_output_tokens: int = 8192`（models.py，紧邻 `context_limit`）
- `LoopGuard.reserved_output_tokens: int = 8192`（models.py，紧邻 `context_limit`）
- `SessionView.reserved_output_tokens: int = 8192`（control/types.py，投影层）

**赋值点**（与现有 `context_limit = llm.context_limit` 一一对称）：
- core：runtime.py:555 旁 `session.reserved_output_tokens = llm.max_output_tokens`；
  runtime.py:556/768/799 构 `LoopGuard(...)` 处一并传入；compact_session 路径 runtime.py:1021-1026 同。
- host：`_resolve_context_limit`（sessions.py:92）旁加 `_resolve_max_output_tokens`（取 `client.max_output_tokens`
  回退 8192），在 sessions.py:233/518 创建会话时一并传入。
- control 层：`reducers.py`（96/165/337）投影、`converters.py:23` SessionView→Session 转换，
  与 `context_limit` 平行补 `reserved_output_tokens`。

**计算助手**（单一真源，避免各处重算）：在 core/utils.py 加
```python
def effective_limit(context_limit: int, reserved_output_tokens: int) -> int:
    """装配/压缩预算的有效上限：为 LLM 输出预留余量后的可用输入窗口。"""
    return max(0, context_limit - max(0, reserved_output_tokens))
```
所有消费点（§4.3）通过它取值，不裸算 `context_limit - reserve`。

### 4.2 budget 重构：保护层级 + 保最近裁剪

替换 `PriorityBudgetStrategy` 现有"同档按体积"规则。用 `effective_limit` 作为 `token_limit`
（由调用方传入，见 §4.3；budget 本身仍接收一个 `token_limit` 参数，不感知 reserve 来源）。

**保护层级（priority 越小越保，`0` 永不丢；丢序 7→1）**——用**分别定义的整数 priority** 表达
（不用 subrank）。「已完成 vs 当前」随请求变，是唯一的**动态**轴，由 budget 提级实现；其余全静态。

| priority | 内容 | 赋值方 | 档内 tiebreak |
|---|---|---|---|
| **0** | identity、task_spec、**当前消息锚**（当前 task 的 user_prompt） | slot_priority(identity/task_spec)=0；**pin 动态** | — 永不丢 |
| **1** | directive、capabilities(tool/skill/agent)、**background(项目背景)** | slot_priority | 按体积 |
| **2** | `AGENT_COMPACT_SUMMARY`（agent 层跨 task 折叠——不可替代的蒸馏经验） | slot_priority | 按新旧 |
| **3** | blackboard(subtask/predecessor/project_log) | slot_priority | 按体积 |
| **4** | **当前 task 内容**（`task_id`/`origin_task_id`==当前，除已 pin 的 user_prompt） | **budget 动态提级** | 按新旧 |
| **5** | 已完成 task 的 **agent 层回合**（`agent_conversation_turn`：finish/dispatch 对） | slot_priority | 按新旧 |
| **6** | 已完成 task 的 **task 层胶囊**（`user_prompt`/`llm_response`/`tool_result`/**`task_compact_summary`**） | slot_priority | 按新旧 |
| **7** | knowledge、long_memory（外部召回） | slot_priority | 按体积 |

**降级链**：外部召回 → 完成 task 明细(6) → 完成 task 的 agent 回合(5) → 当前 task 内容(4) →
blackboard(3) → AGENT 摘要(2) → 能力(1) → floor(0)。语义：先舍最可再生/最冗余的（外部召回、已完成
工作的明细），已完成 task 逐层退到只剩 `AGENT_COMPACT_SUMMARY`(2) 代表，最后才动当前 task。

> **两类摘要区分（关键，勿混）**：`AGENT_COMPACT_SUMMARY` 是 **agent 层**跨 task 折叠的蒸馏经验 →
> priority 2 受保护；`TASK_COMPACT_SUMMARY` 是 **task 层**、是**某个 task 胶囊的组成部分**（finalize
> 后胶囊 = `USER_PROMPT` + `TASK_COMPACT_SUMMARY`）→ 归 priority 6（完成）或随当前 task 提级到 4，
> **跟着它所属 task 的胶囊一起丢**，不单独保护。

> **当前 task 提级（动态，与 pin 同性质）**：budget 对 `metadata["task_id"]==request.task.id` 或
> `metadata["origin_task_id"]==request.task.id` 的 block 提级到 priority 4（比已完成 task 的 5/6 更保）。
> 这是唯一无法静态化的轴——同一条记录本轮是"当前"、下轮就是"已完成"。`slot_priority` 只按 kind/type
> 给静态基线（把 history raw 一律当"已完成"→ 5/6），budget 再据当前 task id 提级。

> **当前消息锚（pin）**：`metadata["task_id"]==request.task.id` 且 `type==user_prompt` 的 block 提到
> priority=0（不可裁）。理由：① 它是 composer 渲染 `## Current Message` 框的落点（in-memory 路径
> `_frame_current_message` 就地装饰这条；被裁则当前消息丢失）；② 当前用户诉求最不该丢。代价：若当前
> user_prompt 本身巨大，它进不可裁地板、可能触发 `ContextOverflowError`（§4.5）——诚实行为。
> 坍缩后的当前 `USER_PROMPT`（`metadata.collapsed`）也是当前 task 的 user_prompt → 同样被 pin 到 0，
> 无需 slot_priority 特判。
>
> **为何必须 pin，不能靠 task_spec 回退（时序不变量，勿优化掉）**：task_spec block（priority 0）虽也
> 携带 `user_prompt` 内容，但它是**元数据载体、不占消息位**；composer 的 fresh-build 回退是
> `messages.append(...)`（composer.py:343），会把当前消息塞到**列表末尾**。而当前 task 的 user_prompt
> 只有在它是**最老**时才会被裁到——此时它比当前 task 自己的后续回合都老，那些回合已存活；把当前消息
> 重建到末尾 → **原始提问排到自己的回复之后，时序倒挂**。故 task_spec 只保**内容在场**，pin 才保
> **时序位置在场**，二者互补而非冗余。in-memory 路径永远就地装饰、绝不重排。

#### 4.2.1 配对原子裁剪（raw 档的关键约束）

按 timestamp 逐块丢最老 raw 会切断 `assistant(tool_calls)` 与其 `tool(tool_call_id)` 的配对，产生
dangling tool_call / orphan tool_result → provider 400。gateway 的
`drop_dangling_tool_calls`/`drop_orphan_tool_results`（llm_gateway.py:59/119）是**发送前 drop-only
兜底**：若 budget 切了半对，gateway 会再把对手也删掉——导致 **budget 的 freed-token 记账失真**、
且压缩器看到残缺 transcript。故配对必须在 budget 层解决：

**丢弃单元（DropUnit）**——裁剪前把 message-target block 聚合成原子单元：
- 一个 assistant block（`metadata["tool_calls"]` 含 id 列表）+ 所有 `metadata["tool_call_id"]`
  命中其 id 的 tool block = 一个单元，同生共死。
- 无 tool_calls 的 assistant、`user_prompt`、summaries、其它 = 单元素单元。
- 单元 `token_estimate = Σ 成员`；单元排序 timestamp = `min(成员 timestamp)`（最老成员定序）；
  单元 priority = `max(成员 eff_priority)`（配对成员同 task 同层 → priority 一致，max 无碍）。
- 配对只在同层同 task 内发生（call 与 result 同 priority），不跨 priority 成组。

实现：budget 加 `_coalesce_tool_pairs(blocks) -> list[DropUnit]` 预处理，裁剪按**单元**丢弃
（丢一个单元 = 丢其全部成员 block id）。gateway 兜底保留为纯 belt-and-suspenders。

**裁剪算法**（budget.apply）：
1. `total = Σ token_estimate`；`total ≤ token_limit` → 原样返回。
2. 每 block 求 `eff_priority`：先 `slot_priority` 静态基线，再 budget 动态覆盖——
   pin（当前 user_prompt）→ 0；当前 task 内容（task_id/origin==当前）→ 4。
3. 聚合 DropUnit；**统一排序键 `(-unit_priority, unit_min_ts, -unit_tokens)`**——`-unit_priority`
   降序保证 priority 大的先丢；`unit_min_ts` 升序 = 最老先丢；`-unit_tokens` 为无 ts 档（能力等，
   ts=""）的次级（大先丢）。**无 subrank**。
4. 依序丢弃单元直到 `total ≤ token_limit`；`unit_priority == 0` 跳过（永不丢）。
5. 全部可裁单元丢完仍 `total > token_limit` → 抛 `ContextOverflowError`（决策 B；`required`=priority-0 之和）。

**priority 集中映射（用户定 2026-07-02）**：静态分层收敛到单一真源 `core/assembler/priority.py`：

```python
def slot_priority(kind: str, mem_type: str | None = None) -> int:
    """槽位 → 裁剪 priority 的静态基线（tier 表的代码化，见 §4.2）。
    「当前 vs 已完成」是动态轴，不在此——由 budget 提级（当前→4）。故此处 raw 一律按"已完成"给 5/6。
    kind: BlockKind；mem_type: history 类 block 的 MemoryEventType 字符串。"""
    if kind in ("identity", "task_spec"):
        return 0
    if kind in ("capabilities", "directive", "background"):
        return 1                                # 项目背景=系统提示内容，与能力同档（原 blackboard.py 即 priority 1）
    if mem_type == "agent_compact_summary":
        return 2                                # agent 层跨 task 折叠（受保护）
    if kind == "blackboard":
        return 3
    # 4 = 当前 task 内容：budget 动态提级，slot_priority 不返回 4
    if mem_type == "agent_conversation_turn":
        return 5                                # 已完成 task 的 agent 层回合
    if kind == "history":
        return 6                                # 已完成 task 的 task 层胶囊（含 task_compact_summary）
    return 7                                    # knowledge(reference) / long_memory(语义召回)
```

各 source 改为**调用 `slot_priority(...)` 而非写死整数**：
- `_history.py:record_to_history_block`：`slot_priority("history", str(record.type))`；**并把
  `origin_task_id` 也带进 block metadata**（现仅带 task_id），供 budget 判 agent 层回合归属哪个 task。
- `agent_recall.py`：`AGENT_COMPACT_SUMMARY` block `slot_priority("history", "agent_compact_summary")`；
  raw/turn 走 `record_to_history_block`（已覆盖）。
- `capability.py`、`identity.py`、`blackboard.py`、`knowledge.py`、`long_memory.py`、`task_spec.py`：
  各自 `kind` 传入 `slot_priority`。
- **pin 与当前 task 提级不进此表**：依赖 `request.task.id`，是 budget 层的运行时覆盖。

> 好处：静态分层一处可改、一眼可核。所有 history 类 block 已携带 `metadata["timestamp"]`
> （_history.py:56、agent_recall.py:115）；本次为 agent 层回合补 `origin_task_id` 到 metadata。

### 4.3 触发/压缩目标统一按 effective_limit

所有原先用 `context_limit` 做限额判定的点，改用 `effective_limit(context_limit, reserved_output_tokens)`：

| 点 | 位置 | 改动 |
|---|---|---|
| budget token_limit | assembler.py:158 | `token_limit = effective_limit(session.context_limit, session.reserved_output_tokens)` |
| PrepareStep 触发 | prepare.py:161-163 | 分母 `context_limit` → `effective_limit(...)` |
| 派发前触发 | compact.py:170-174 | 分母 → `effective_limit(...)` |
| escalating 压缩目标 | compact.py:448-453 | `target_tokens = int(effective_limit(...) * target_ratio)` |
| CompactStep 入参 | compact.py:548 | `token_estimate` 语义不变（真实/估算 token），仅比较基准变 |

> 语义校准：`compact_token_ratio`（默认 0.8）、`compact_target_ratio` 的**分母从模型总窗口变为
> effective_limit**，即触发/压到的绝对 token 数整体下移 `reserved_output_tokens`。这是期望行为
> （压缩要为输出留出的窗口负责）。各 ratio 数值不变。

**act 停止阈值**（act.py:296-300，`usage.prompt_tokens >= int(context_limit * 0.8)`）：此处用的是
**provider 返回的真实 token**，是"是否已逼近窗口该停 act"的独立判定，与装配预算不同源。本次
**改为 `>= int(effective_limit(...) * 0.8)`**，使"停 act"与"装配上限"口径一致（都以 effective_limit 为基准）。
硬编码 `0.8` 保持不变。

### 4.4 compact 装配的"强制裁"行为

compact 装配（`summarize_for_compact` → `assembler.assemble(purpose="compact")`）天然走 §4.2 的
budget，因此**无需 compact 专属逻辑**：`effective_limit` 作 token_limit + 保最近/保摘要裁剪，即实现
"按 effective_limit 强制去掉槽位数据"。落地效果：

- 丢弃顺序（priority 7→1）：外部召回(7) → 完成 task 明细(6) → 完成 task 的 agent 回合(5) →
  当前 task 内容(4) → blackboard(3) → AGENT 摘要(2) → 能力/指令(1)；priority-0 永不丢。各档内最老先丢。
- 因完成 task 明细先丢、`AGENT_COMPACT_SUMMARY` 受保护，compact 输入退化为"最近若干 raw + 蒸馏经验"——
  语义自洽：完成 task 的明细通常已折进 `AGENT_COMPACT_SUMMARY`。
- 若 priority-0（identity+task_spec+当前消息锚）仍超 `effective_limit` → 抛 `ContextOverflowError`。
  注：1~7 各档虽保护度不同，但都排在 priority-0 之前被丢——故真正触发 overflow 的**不可裁地板仅
  priority-0**（soul + 当前 task 的 title/desc/user_prompt）。其余会在报错前被丢尽。

### 4.5 ContextOverflowError 的归宿（PrepareStep → 路由）

现状：`ContextOverflowError`（errors.py:55）定义了但**全链路无人 catch**——会经 driver 的
`except Exception`（driver.py:247）发 `STEP_FAILED` 后原样上抛到 runtime `_run_loop`，落进
**generic `except Exception` → task FAILED 终态**（runtime.py:1382）。

设计决策：**终态失败,不可恢复,但要显式且可操作**。关键判断——它**非瞬时**：`/resume` 会重装配
同一批 block 再次溢出，故绝不能像 `LLMOutageError` 那样走 SUSPENDED/resume（会无限循环）。

落地：
1. `ContextOverflowError` 标 `retriable = False`（属性），使 TaskManager 不重排、`_run_loop`
   generic 分支的 `getattr(exc, "retriable", False)` 走"非重试"日志。
2. **可操作文案集中在异常本身**（关键：文案必须随 `str(exc)` 走到 host）。`ContextOverflowError.__init__`
   在未显式传 message 且带有 `context_limit` 字段时，用四个字段拼出**可操作的用户可见文案**（中文）作为
   默认 message，含实测数字，例如：
   > "上下文超出模型可用窗口：保护槽位（角色设定 + 当前任务/消息）约 {required} tokens，已超过为输出
   > 预留后的可用窗口 effective_limit={eff}（= 模型窗口 {ctx} − 输出预留 {reserve}）。请改用更大
   > 上下文窗口的模型，或缩短当前消息 / 任务描述。"
   抛错点（budget.apply）**只传字段、不传 terse message**，使 `str(exc)` 即为该文案。
   > **为什么集中在异常**：生产失败链路是 `TaskManager.run_task` 的 `except` → `_handle_task_failure(error=str(e))`
   > → `_emit_task_failed` 发 `TASK_FAILED.error_message = str(exc)`；投影 reducer 对 `TASK_FAILED` 只改
   > status、**不回填 `task.error`**。若文案只写在 `_run_loop` 的 `task.error`（内存态 Task 对象），事件/投影
   > 读模型（host 侧）永远读不到，只会看到 terse 原串。故文案必须是 `str(exc)`，才能经既有 error_message 通路抵达 host。
3. 在 runtime `_run_loop` 加 **`except ContextOverflowError`（置于 generic `except Exception` 之前）**：
   - task → FAILED（终态，与 generic 同），`run_error = exc`（经 finally 上抛，走既有 FAILED 通路）。
   - `task.error = str(exc)`（可操作文案，供持有 live Task 引用的调用方；host 侧则经 `str(exc)` → `TASK_FAILED.error_message` 获取）。
   - **不** SUSPENDED、**不** 发 `session_interrupted`（区别于 outage）。
4. `failure_counter`：**走标准 FAILED 终态语义**（用户定，2026-07-02）——与 generic FAILED 一致，
   计入 session `failure_threshold`。`except ContextOverflowError` 分支仅为保证不 SUSPEND + 定制 `task.error`，
   失败计数/终态判定复用现有 FAILED 路径，不特殊处理。

> 数字来源：抛错点（budget.apply）把 `required`（priority-0 之和）、`effective_limit`、`context_limit`、
> `reserved_output_tokens` 塞进 `ContextOverflowError` 字段；文案在异常 `__init__` 内组装，无需重算。

## 5. 受影响文件清单（排查产出）

**core**
- `core/utils.py` — 新增 `effective_limit()`
- `core/state/models.py` — Session/LoopGuard 加 `reserved_output_tokens`
- `core/control/types.py`、`reducers.py`、`converters.py` — SessionView 投影/转换镜像字段
- `core/runtime.py` — 555/556/768/799/1021-1026 赋值 reserve + 传 LoopGuard
- `core/assembler/assembler.py:158` — token_limit 用 effective_limit
- `core/assembler/priority.py`（**新增**）— `slot_priority()` 集中映射（tier 表唯一真源）
- `core/assembler/budget.py` — 裁剪算法（整数 priority + eff_priority 动态覆盖(pin/当前提级) +
  统一排序键最老先丢 + 配对原子 `_coalesce_tool_pairs` + 抛错携带 required/effective_limit 字段）
- `core/assembler/sources/` — `_history.py`（改调 `slot_priority()` + **把 `origin_task_id` 带进 metadata**）
  /`agent_recall.py`/`capability.py`/`blackboard.py`/`knowledge.py`/`long_memory.py`/`identity.py`/`task_spec.py` 改调 `slot_priority()`（不再硬编码整数）
- `core/loop/steps/prepare.py:161-163`、`compact.py:170-174/448-453`、`act.py:296-300` — 限额判定改 effective_limit
- `core/errors.py:55` — `ContextOverflowError` 加 `retriable=False` + `floor/effective_limit/context_limit/reserved` 字段
- `core/runtime.py:1382 前` — 加 `except ContextOverflowError` 分支（FAILED + error_code + 可操作文案，不 SUSPEND）

**host**
- `api/sessions.py` — 加 `_resolve_max_output_tokens`，创建会话时传入（233/518）
- （env/DB 已有 `max_output_tokens`，无需新增配置）

## 6. 测试

- **单测 budget**：
  - reserve 生效：`effective_limit = context_limit - reserve`，超 effective 但未超 context 时仍裁。
  - 保最近：同档 raw 超预算时，丢最老、留最新（按 timestamp）。
  - 保摘要：`AGENT_COMPACT_SUMMARY`（priority 2）在 raw（priority 6/当前 4）全丢后才考虑；raw 超量时摘要不动。
  - 完成 vs 当前：当前 task 内容（提级 4）比已完成 task（5/6）更保，即便更老也留；完成 task 层胶囊(6)先于其 agent 层回合(5)丢。
  - **配对原子**：丢含 tool_call 的 assistant 时，其 tool_result 同批丢（反之亦然）；断言结果无
    dangling/orphan（送 gateway 前即成对）。多 tool_call 的 assistant 与多 result 整体成单元。
  - **current-message pin**：`task_id==request.task.id` 的 user_prompt 即便最老也不丢。
  - 抛错：仅 priority-0（含 pin 的当前消息）超 effective_limit → `ContextOverflowError`，且异常带
    `floor/effective_limit/context_limit/reserved` 字段。
- **单测 effective_limit()**：reserve=0 / reserve>context / 负值防御。
- **单测 overflow 路由**：runtime `_run_loop` 收到 `ContextOverflowError` → task FAILED +
  `error_code="context_overflow"` + 文案含数字；**不** SUSPENDED、**不** 发 `session_interrupted`；
  `retriable=False`。
- **compact 触发/目标**：`test_compact_trigger` / `test_escalating_compact` 补 effective_limit 基准用例。
- **plumbing**：session 创建后 `reserved_output_tokens` 正确落到 Session/LoopGuard（core + host 两路径）。
- **回归**：现有 compaction/budget 用例按新 priority 数值与新裁剪序调整期望值；gateway 兜底用例
  仍绿（成对输入下 drop_dangling/drop_orphan 为 no-op）。

## 7. 已知局限

- **估算不修**：`len//4` 对 CJK/JSON 系统性低估。reserve 只在窗口顶部留固定余量（默认 8192），
  **无法吸收 2~4× 的估算偏差**——当真实 token ≈ 2× 估算时，budget 以为压到 effective_limit（比如
  0.95×window − 8192），真实可能仍 > window，provider 仍会拒。本方案解决"没给输出留位置"，
  不解决"prompt 本身被低估到超窗"。二者正交；估算口径改进另开专项。
- **抛错即崩（已设计归宿，见 §4.5）**：保护地板（priority-0）超限时终态 FAILED + 可操作文案，
  不可 resume（非瞬时）。这是设计取舍，不是遗留问题。

## 8. 决策记录 / 待确认

**已确认（本会话）**
- 决策 A 作用域 = **全局**（act/observe/compact 共享 budget 一起改）。
- 决策 B = 保护地板超限**抛 `ContextOverflowError`**，不块内截断。
- reserve = `LLMClient.max_output_tokens`（host 可配，默认 8192）。
- 估算口径本次不动（非目标）。
- 保护层级用**分别定义的整数 priority**（0~7，不用 subrank），丢序 7→1、0 永不丢。
  能力/指令(1) 比 AGENT 摘要(2) 更保（actor 需工具）；`TASK_COMPACT_SUMMARY` 是 **task 层胶囊内容**
  （priority 6/当前提级 4），只有 `AGENT_COMPACT_SUMMARY` 是受保护的摘要(2)。
- **完成/当前任务的降级子序用整数 priority 表达**：完成 task 明细(6) → 完成 task agent 回合(5) →
  当前 task 内容(4)。「已完成」用"非当前 task"近似（budget 拿不到 task.status）。
- `ContextOverflowError` 归宿 = **标准 FAILED 终态**（计入 failure_threshold）+ 定制 `error_code`/可操作
  文案，不 SUSPEND（§4.5）。
- raw 丢弃须**配对原子** + **当前消息锚 pin** + **当前 task 提级**（§4.2.1，后两者 budget 动态）；
  pin 保证当前消息**时序位置**在场（task_spec 只保内容），互补勿当冗余优化（§4.2 时序不变量）。
- priority 静态分层**集中到 `core/assembler/priority.py:slot_priority()`**（唯一真源），各 source 改调它；
  「当前 vs 已完成」动态轴由 budget 提级（不进 slot_priority）。为 agent 层回合 block 补 `origin_task_id` metadata。

**评审门开放项**：无。全部决策已定，spec 定稿。
