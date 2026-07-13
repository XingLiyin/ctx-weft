# observe 收敛 retry 段折叠 + 预算驱动三级升级 compact

- 日期：2026-07-01
- 范围：
  - `ctx_weft/core/loop/steps/observe.py`（retry 三来源前台同步段折叠；删预算 compact；root 前台强制 LLM）
  - `ctx_weft/core/loop/steps/compact.py`（`_compact_scope` → 预算驱动升级式 L1→L2→L3；新增 rich→lean 降级；分析式粗估）
  - `ctx_weft/core/loop/steps/prepare.py`（`_should_compact` 纯预算；升级循环 + 末次重装配）
  - `ctx_weft/core/loop/steps/act.py`（`_maybe_predispatch_compact` 复用同一套升级 compact）
  - `ctx_weft/core/loop/steps/finalize.py`（retry 不再写 `process_report`/Progress So Far 字段）
  - `ctx_weft/core/assembler/composer.py`（去掉 process_report vs 段摘要去重逻辑）
  - loop_config / models（新增 `compact_target_ratio`；废弃 `compact_message_delta`；`*_keep_last` 语义改注为保留底线）
- 关系 spec：
  - **延续** `docs/superpowers/specs/2026-06-28-background-observe-as-observe-summarizer-design.md`：段摘要 = `TASK_COMPACT_SUMMARY`（role=assistant）、background observe fire-and-forget + `await_pending_background_observe`。**本 spec 覆盖其不变量 5**（机械退出不再走「同步 COMPACT.md 压缩 / 前台 `_maybe_compact_task` 直呼 `apply_compact`」，改为「前台段折叠 + 预算驱动升级 compact」）。
  - **延续** `docs/superpowers/specs/2026-06-26-interaction-preserving-capsule-design.md`：胶囊、`collapse_task_layer`、保锁点。
  - **延续** `docs/superpowers/specs/2026-07-01-compact-task-collapse-two-cue.md`：`collapse_task_layer` / 二段 cue / `_compact_scope` 双阈值（本 spec 把双阈值改写为升级式）。
  - 底层机制：`docs/spec/06-memory-layers-and-compaction.md`。

---

## 1. 动机

现状两个问题：

1. **压缩经路分裂、且与新坍缩机制不一致。** compact 逻辑散在三处：
   - `observe._maybe_compact_task`（仅 max_turns，前台直呼**旧的** `apply_compact`）；
   - `prepare._should_compact → CompactStep → _compact_scope`（token 比率 / **消息条数** 双门控，并行折 task+agent）；
   - `act._maybe_predispatch_compact`（派发前）。

   其中 observe 的 max_turns 压缩仍用旧 `apply_compact`，没走 2026-07-01 引入的 `collapse_task_layer` / 升级坍缩，形成不一致。

2. **retry 反馈靠 `process_report` 字段累积，旧轮 raw 不删。** 机械退出 retry 时 `finalize` 把 `act_recap` 写进 `task.process_report`，composer 渲染成独立 `## Progress So Far`，但**上一轮 attempt 的 raw 工具调用留在 task 层**，导致重试轮上下文越滚越大（正是触发 context_limit 的元凶之一）。且 max_turns 那轮同时写 `process_report` + `TASK_COMPACT_SUMMARY`，composer 要靠 `_progress_already_in_history` 去重，脆弱。

### 1.1 目标

1. **retry 三来源**（max_turns / context_limit / observer-retry）统一：进 observe → 产 `act_recap` → **前台同步把本轮 attempt 折成一条段摘要（`TASK_COMPACT_SUMMARY`）并删本轮 raw**。不再写 `process_report`/Progress So Far 字段。
2. **compact 只有一套实现**（预算驱动、升级式 L1→L2→L3），两个调用点：`prepare`（预算触发）、`pre_dispatch`（派发前触发）。**observe 永不调用预算 compact**。
3. **触发一律看预算**（token 比率），废弃消息条数门控；`*_keep_last` 降级为纯保留底线。
4. 双比率滞后防抖动；升级间用分析式粗估，进 act 前完整重装配一次校正。

---

## 2. 核心不变量

1. **observe 不做预算 compact**：retry 三来源进 observe 只产 `act_recap` + 前台段折叠（`TASK_COMPACT_SUMMARY` + 删本轮 raw），不碰 agent 层、不做多级升级、不写 `process_report`。
2. **compact 单一实现**：升级式 L1→L2→L3 一套代码，`prepare` / `pre_dispatch` 两个触发点各用各的触发比率；observe 不在内。
3. **段折叠双路径、同产物**：retry（马上重跑）→ **前台 observe 同步折**；interactive 暂停（interrupt/plain_text，等用户）→ **background observe 异步折**（不变），下一轮 `await_pending_background_observe` 等其结束。两路都产 `TASK_COMPACT_SUMMARY`（role=assistant）+ 删本轮 raw、保 `USER_PROMPT` 锚。
4. **胶囊定型仍在终态 close**（`finalize._close_one`）：success/fail 时合 finish 对 + supersede 末 raw。retry 只往 task 层累积段摘要，不合 finish 对。
5. **触发看预算**：`token_estimate / context_limit ≥ compact_token_ratio` 才触发；`*_keep_last` 只作保留底线，不作触发门。

---

## 3. 设计

### 3.1 retry 三来源前台同步段折叠（observe.py）

`context_limit` / `max_turns`（act 机械退出）与 `observer-retry`（非 root 子任务 LLM 判定 retry）汇合到「attempt 结束、outcome=retry」。observe：

1. 产 `act_recap`：
   - **非 root 子任务**：前台 LLM observe（`report_task_outcome`）本就产 `act_recap`。
   - **root + 机械退出**：前台**强制 LLM observe**（扩 `_should_use_llm`：max_turns **与** context_limit 都强制），产有质量的 `act_recap`（否则段摘要是空壳）。
2. **前台同步段折叠**（重塑 `_maybe_compact_task` → 如 `_fold_retry_segment`）：
   - `apply_compact(layer=TASK, summary=act_recap, keep_last=0, protect_types=(USER_PROMPT, TASK_COMPACT_SUMMARY))` → 写一条 `TASK_COMPACT_SUMMARY`（role=assistant）+ supersede 本轮 attempt 全部 raw（`LLM_RESPONSE`/`TOOL_INVOCATION`/`TOOL_RESULT`）。
   - **protect `TASK_COMPACT_SUMMARY`**：只折本轮 raw、保留既往段摘要 → 多轮 retry 段摘要**累积**（非替换），由 L3（保 `collapse_keep_last` 条）控界。这是「多条段摘要 → L3 坍缩」（§3.3 L3 / R4）成立的前提；若只 protect `USER_PROMPT`，旧段摘要每轮被 supersede，L3 永无可坍缩物、增量进度亦丢失。
   - **无条件**（每次 retry 都折）、**无预算门**、**无 keep_last 门控**（`keep_last=0` 折整轮 raw）。
   - **无额外 LLM**：复用 observe 已产的 `act_recap`。
3. observe → finalize：retry 非终态，finalize **不再写 `task.process_report`/`process_report_at`**（该字段的 retry 角色由 `TASK_COMPACT_SUMMARY` 段摘要取代）。

> retry 是「马上要执行」→ 段折叠必须在重排前同步完成，下个 run 一进 prepare 就看到折好的段摘要。故不走 background observe。

### 3.2 interactive 暂停仍走 background observe（不变）

interrupt / plain_text 暂停（等用户、可能很久）→ `launch_background_observe`（fire-and-forget）产段摘要 + `apply_compact` 折 raw（现状 else 分支，2026-06-28 §3.4）。下一轮（用户 resume）由 `await_pending_background_observe(task_id)` 保证段折叠已完成再装配。**本 spec 不改这条路**，仅澄清它与 retry 前台折的分野（不变量 3）。

### 3.3 预算驱动升级式 compact（compact.py，替 `_compact_scope`）

新函数按序升级，每级后**分析式粗估**（§3.5）判断是否继续：

- **L1 · compact agent** = `fold_root_experience`：保最近 `compact_keep_last` 个顶层胶囊，更老的（连同 task 层胶囊 body + agent 对话 + 旧 `AGENT_COMPACT_SUMMARY`）折成一条 `AGENT_COMPACT_SUMMARY`。**1 次 LLM**（`summarize_for_compact(scope="agent")`）。
- **L2 · rich→lean 降级**（新增）：把 L1 保留的**同 agent 任务胶囊**（rich = task 层 body `USER_PROMPT`+`TASK_COMPACT_SUMMARY` + agent 层 finish 对 assistant`{act_recap}`/tool`{task_summary}`）**塌成 sub-agent lean 表示**（= 一条 dispatch-result 式 agent 层回填：delegate 框 + tool 回填 `outputs`+Process Report），**丢掉 task 层 body**。保留每个 task 的身份，只丢交互细节。**无 LLM**（`act_recap`/`task_summary`/`outputs` 都是现成字段）。
- **L3 · 坍缩当前 task** = `collapse_task_layer`：per-retry 折后 task 层已无 raw、全是段摘要；L3 把多条段摘要坍成更少——保最近 `collapse_keep_last` 条，更早的（连同原始 `USER_PROMPT`）坍成一条新 `USER_PROMPT`（原始节 + 执行摘要）。摘要走 `summarize_for_compact(scope="task")` 整段综合。**1 次 LLM**。

升级前提：粗估仍 ≥ `compact_target_ratio`。某级折不动（对应单元数 ≤ 其 `*_keep_last`）→ 直接跳下一级。三级压完仍 ≥ 目标 → 没得压，进 act。

### 3.4 双比率滞后（loop_config）

- **触发**：`token_estimate / context_limit ≥ compact_token_ratio`（如 0.8）。
- **目标**：升级直到粗估 `< compact_target_ratio`（如 0.6）。**新增配置项** `compact_target_ratio`（默认 < `compact_token_ratio`；缺省时可退化为等于触发比率 = 无滞后）。
- 滞后区避免「压到刚好略低于触发比率 → 下一轮又立刻触发」的抖动。

### 3.5 分析式粗估 + 末次重装配（prepare.py，Q4=c）

- L1/L2/L3 **之间**不完整重装配，用**分析式粗估**：累减本级 supersede 掉的记录 token（`estimate_tokens` 各记录 content 之和，减去新写摘要 token），从当前 `token_estimate` 递减。
- **进 act 前完整重装配一次**校正（`prepare._assemble()`）。粗估只用于「要不要升级」决策，不回滚已折 → **粗估宁可偏保守（倾向多压）**。

### 3.6 触发点与配置

- `prepare._should_compact` → **纯预算**（`compact_token_ratio`）。命中 → 跑升级 compact（§3.3）→ 重装配 → act。删除消息条数分支（§3.7）。
- `act._maybe_predispatch_compact`（保留）→ **复用同一套升级 compact**，触发比率换 `predispatch_compact_token_ratio`（6-A）。派发前 L3 坍缩「即将挂起等子」的当前 task 无碍（resume 召回胶囊摘要）。子 spawn-inherit 到压缩后记忆。
- 配置：
  - **新增** `compact_target_ratio`。
  - **废弃** `compact_message_delta` 及「计数 ≥ 阈值才触发」门控。
  - `compact_keep_last`（L1 保留底线：保 N 个胶囊）/ `collapse_keep_last`（L3 保留底线：保 N 条段摘要）**保留，语义改注为「保留底线，非触发门」**。

### 3.7 composer 去重逻辑消解（composer.py）

retry 不再同时写 `process_report` + 段摘要 → 不存在同一份报告渲染两遍。**删除** `_progress_already_in_history`（composer.py:631）及 `_build_current_task_block` 里据 `process_report` 渲染 `## Progress So Far` 的独立分支（composer.py:342-343 / `_progress_history_block`）。retry 反馈改由 task 层 `TASK_COMPACT_SUMMARY` 记录承载——`_history.py:42-49` 已把 `TASK_COMPACT_SUMMARY` 冠以 `## Progress So Far` 标题渲染，语义等价、来源单一。

> `process_report` 字段本身**不删**——它仍服务终态 finish 对 / 子任务 bubble（`report_task_outcome` / dispatch 回填）。仅**去掉它的 retry Progress-So-Far 渲染角色**。

---

## 4. 数据流（一个「重试两次后 finish」的 root 任务）

```
Run 1: prepare（首轮通常不超预算 → 直接 act）
       act → max_turns/context_limit → observe
         observe: 前台 [强制] LLM → act_recap；同步段折
                  → TASK_COMPACT_SUMMARY① + 删本轮 raw（保 USER_PROMPT）
                  → finalize: 置 PENDING 重排（不写 process_report、不合 finish 对）
       task 层 = USER_PROMPT + [段摘要①]

Run 2: prepare（假设超预算 ≥ 0.8）→ 升级 compact：
         L1 agent 折（保 compact_keep_last）→ 粗估仍 ≥ 0.6？
         L2 rich→lean 降级 → 粗估仍 ≥ 0.6？
         L3 collapse_task_layer（保 collapse_keep_last 条段摘要）→ 粗估 < 0.6 停
         进 act 前完整重装配一次
       act → 机械退出 → observe 同步段折 → TASK_COMPACT_SUMMARY② + 删本轮 raw
       task 层 = USER_PROMPT + [段摘要①②]（若 L3 触发则 = USER_PROMPT(原始+坍缩摘要) + [段摘要②]）

Run 3: prepare → act → finish_task → actor_done → observe（outcome=success，不强制 retry）
       finalize._close_one（胶囊定型）：
         agent 层合 finish 对（assistant{act_recap}/tool{task_summary}）
         supersede task 层末 raw 段，保 USER_PROMPT + TASK_COMPACT_SUMMARY 锚
       → 完整胶囊 = task body（USER_PROMPT + 段摘要们）+ agent 层 finish 对
         此后作为「同 agent 任务胶囊(rich)」，将来别的 task 超预算时被 L1 折 / L2 降级
```

三处 compact/折叠定位：

| 位置 | 触发 | 做什么 | LLM |
|---|---|---|---|
| observe per-retry 段折（前台同步） | 每次 retry（无条件） | 本轮 raw → 一条段摘要，删本轮 raw | 复用 act_recap（root 机械退出前台强制 1 次 observe LLM） |
| background observe 段折（异步） | interactive 暂停 | 同上，异步；下一轮 await 等结束 | 后台 observe LLM |
| prepare / pre_dispatch 升级 compact | 预算 ≥ 触发比率 | L1 agent 折 → L2 rich→lean → L3 坍当前 task，压到 < 目标比率 | L1/L3 各 1 次，L2 无 |

---

## 5. 风险 / 边界

- **R1 · 分析式粗估偏差**：粗估可能低估（压不够）或高估（过压）。末次完整装配只校正数值、不回滚已折 → 粗估**偏保守（倾向多压一点）**；过压的代价仅是上下文略瘦，可接受。
- **R2 · root 机械退出前台强制 LLM 成本**：现状 root context_limit 走规则（省 LLM），改后 max_turns/context_limit 都前台 1 次 observe LLM → 换来段摘要质量（否则胶囊段是空壳）。可接受。
- **R3 · L2 降级的语义损失**：同 agent rich 胶囊降级成 lean 后丢交互细节，只留 outcome 摘要。仅在 L1 不够时触发（较少），且降级对象是**较老的保留胶囊**，可接受。
- **R4 · L3 与段摘要的关系**：per-retry 折后 task 层无 raw，L3 坍的是段摘要（`TASK_COMPACT_SUMMARY`）而非 raw；`collapse_task_layer` 的 `_TASK_LAYER_TYPES` 已含 `TASK_COMPACT_SUMMARY`，天然支持。**escalating_compact 的 L3 可折性门也须用 `_TASK_LAYER_TYPES` 计数**（与 `collapse_task_layer` 实际所折一致）——若用只含 raw 的 `TASK_COMPACT_TYPES`，累积段摘要计不进、L3 永不触发。
- **R5 · 升级式的空转**：某级单元数 ≤ `*_keep_last` 折不动即跳级；三级全跳（无可折）而仍超预算 → 直接进 act（硬跑），不死循环。

---

## 6. 测试计划（`uv run pytest`）

- **T1 · retry 前台同步段折**：max_turns 退出（root，mock observe LLM 固定 act_recap）→ 断言写一条 `TASK_COMPACT_SUMMARY`(role=assistant, content=act_recap) + 本轮 raw 被 supersede + `USER_PROMPT` 保留 + `task.process_report` **未**被设置。
- **T2 · context_limit 同 T1**：context_limit 退出走同一前台段折（root 前台强制 LLM）。
- **T3 · observer-retry 段折**：非 root 子任务 observe 判 retry → 前台 `report_task_outcome` act_recap → 同步段折。
- **T4 · observe 不碰 agent 层 / 不做升级**：retry 段折后断言 agent 层记录、当前 task 之外单元不变。
- **T5 · 升级触发纯预算**：`token_estimate/context_limit ≥ compact_token_ratio` 才进 compact；低于则跳过（构造消息很多但 token 低的场景，断言**不**压——验证条数门控已废）。
- **T6 · L1→L2→L3 升级顺序 + 目标比率停**：mock 粗估，令 L1 后仍 ≥ target → 进 L2；L2 后 < target → 不进 L3。断言各级调用与停点。
- **T7 · L2 rich→lean 降级**：构造一个保留的同 agent rich 胶囊 → L2 后 task 层 body 被 supersede、agent 层剩一条 lean 回填、**无 LLM 调用**。
- **T8 · L3 坍段摘要**：task 层多条 `TASK_COMPACT_SUMMARY` → L3 保 `collapse_keep_last` 条、更早坍成一条 `USER_PROMPT`（`summarize_for_compact(scope=task)` 1 次 LLM）。
- **T9 · 分析式粗估 + 末次重装配**：断言升级间不完整重装配（`_assemble` 调用计数）、进 act 前恰好重装配一次。
- **T10 · pre_dispatch 复用升级 compact**：派发前越 `predispatch_compact_token_ratio` → 跑同一套 L1→L2→L3，子继承压缩后快照。
- **T11 · composer 单一渲染**：retry 后仅 `TASK_COMPACT_SUMMARY` 渲染成 `## Progress So Far`，无重复；删 `_progress_already_in_history` 后无回归。
- **回归**：interactive interrupt/plain_text 仍走 background observe 异步段折 + `await_pending_background_observe`；终态 close 合 finish 对不变；非 root bubble 不变。

---

## 7. 受影响文件清单（writing-plans 细化排序）

- `core/loop/steps/observe.py` — `_maybe_compact_task` → 前台无条件段折（keep_last=0、复用 act_recap）；`_should_use_llm` 扩 context_limit；删预算 compact 调用。
- `core/loop/steps/compact.py` — `_compact_scope` → 升级式 L1→L2→L3；新增 rich→lean 降级函数；`fold_root_experience`/`collapse_task_layer` 去计数门控、留保留底线；分析式粗估工具。
- `core/loop/steps/prepare.py` — `_should_compact` 纯预算；升级循环 + 双比率 + 末次重装配。
- `core/loop/steps/act.py` — `_maybe_predispatch_compact` 走新升级 compact（`predispatch_compact_token_ratio`）。
- `core/loop/steps/finalize.py` — retry 分支不再写 `task.process_report`/`process_report_at`。
- `core/assembler/composer.py` — 删 `_progress_already_in_history` + `process_report` 的 Progress-So-Far 独立渲染分支（保 `TASK_COMPACT_SUMMARY` 渲染）。
- loop_config / `core/state/models.py` — 新增 `compact_target_ratio`；废弃 `compact_message_delta`；`*_keep_last` 注释改「保留底线」。

> 同步约定（memory：上游 wefta→weft）：动 core 落 `ctx-weft`；host 镜像配置（若有 `IPMC_*`）落本仓；按既有流程回灌上游。

## 8. 验收

- retry 三来源 → 前台 observe 同步段折（`TASK_COMPACT_SUMMARY` + 删本轮 raw），不写 `process_report`。
- compact 单一升级实现，`prepare`/`pre_dispatch` 两触发点；observe 不调预算 compact。
- 触发纯预算 + 双比率滞后；升级间粗估、进 act 前重装配一次。
- L1 agent 折 → L2 rich→lean → L3 坍当前 task 三级升级正确。
- interactive/close/非 root bubble 既有行为回归不破；§6 全部测试 + `uv run pytest` 全绿。
