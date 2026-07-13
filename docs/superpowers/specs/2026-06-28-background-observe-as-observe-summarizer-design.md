# background observe 升级为 observe 风格段总结器（状态隔离 + close 路径 Process Report 回填）

- 日期：2026-06-28
- 范围：
  - `ctx_weft/core/loop/steps/observe.py`（`_rule_observe` 留作快速占位 + 抽公共 ReAct 骨架 + root close fire background observe，不 await）
  - `ctx_weft/core/loop/steps/background_observe.py`（走 observe persona + 精简工具 + boundary 分流 + 结果槽）
  - `ctx_weft/core/loop/steps/finalize.py`（close 路径 A1：机会性用 background 产出 / 占位 + 异步替换 finish 对 Process Report）
  - `ctx_weft/core/orchestrator/control_capability.py`（新精简工具 `collect_process_report`）
  - `ctx_weft/core/assembler/composer.py`（新 `_BACKGROUND_OBSERVE_INSTRUCTION` cue）
  - `ctx_weft/core/assembler/sources/identity.py`（facet 回退链 `background_observe → observe → act`）
  - `src/ipmastercowork/providers/templates/resolver.py`（`DEFAULT_MERGE_PURPOSES` 加 `"observe"`）
- 关系 spec：
  - **延续** `docs/superpowers/specs/2026-06-26-interaction-preserving-capsule-design.md`：§3.2 root 后台 observe、§3.3 close 胶囊合成、不变量 4「最终段由 finish 对承载」。
  - **延续** `docs/superpowers/specs/2026-06-27-task-segment-summary-assistant-role-design.md`：段摘要 role=assistant。
  - 底层机制：`docs/spec/06-memory-layers-and-compaction.md`。

---

## 1. 动机

三个概念现状混在一起，且 root 段总结质量差：

- **observe** = 对 **task 处理流程**的总结（ROLE.md persona，产 `task_process_report` + 三态裁决）。
- **compact** = 对**会话整体**的压缩（COMPACT.md persona，产 `### 会话目标/已完成工作`）。
- **background observe** 顾名思义是 observe，但**现状错误地调 `summarize_for_compact`（compact prompt / COMPACT.md）** 产段摘要。

且 root 任务正常退出走 `_rule_observe`，只产薄文本（`"Ran N round(s). Task completed."`）作 verdict.summary → 胶囊 finish 对的 Process Report 是这条薄文本，质量差。

### 1.1 目标

1. background observe 改走 **observe persona（ROLE.md）**，与 observe **共用 prompt persona**、**差异化工具与 cue**。
2. background observe 产 verdict 式的 `process_report`，但**严格状态隔离**——绝不污染当前 task 状态。
3. 段总结按边界分流：**打断/暂停**写 memory 段摘要（不变）；**close/finish** 不写 memory，直接进 finish 对 Process Report。
4. close 路径**乐观快速返回**（不阻塞），background observe 异步完成后**替换**薄占位（方案 A1）。
5. 无 observe ROLE 的模板**回退 default 模板 ROLE**。

---

## 2. 核心不变量

1. **background 工具零状态写**：background observe 的工具**不写** `task.status / observer_outcome / actor_done / process_report / error`。状态污染从根消除（无需深拷贝 task）。
2. **persona 共用**：observe 与 background observe 共用 ROLE.md（整体）；差异仅在 **cue（末尾指令）+ 绑定的工具**。
3. **边界分流**：`interrupt/plain_text` → 写 `TASK_COMPACT_SUMMARY`；`finish/normal` → 进 finish 对 Process Report，**不写 memory 段摘要**（对齐 capsule 不变量 4）。
4. **close 不阻塞**：observe 对 root close 快速返回薄 verdict，finalize 立即 close；background observe 异步替换（A1）。
5. **机械退出不变**：`max_turns`（`_maybe_compact_task`）/ `context_limit`（prepare compact）仍走同步 COMPACT.md 压缩，**不**走 background observe。
6. **非 root 不变**：子任务仍走完整 LLM observe（`report_task_outcome` 上报 parent）。

---

## 3. 设计

### 3.1 新 purpose `background_observe`（装配三要素）

- **facet（persona）**：复用 observe 的 ROLE.md。`IdentitySource`（identity.py:32）回退链扩为
  `template.identity.get("background_observe") or .get("observe") or .get("act")`。模板无需新增文件——
  `background_observe` 缺失即落到 `observe`（ROLE.md）。
- **cue（末尾指令）**：composer 新增 `_BACKGROUND_OBSERVE_INSTRUCTION`，在 `_build_facet_trailing_messages`
  的 purpose 分支里选用。内容 =
  「当前 task 处于 `{boundary 状态描述}`。请基于以上执行过程，总结这一段的处理进展，调用
  `collect_process_report` 一次给出 `task_process_report`。**只需总结进展、给出 process_report，
  无需判断 success/retry/fail，不要调用其他工具。**」—— 抑制 ROLE.md 后半的三态裁决。
- **工具**：仅 `collect_process_report`（§3.2），不绑 `report_task_outcome`。

### 3.2 精简工具 `collect_process_report`（control_capability.py）

```python
@control_tool(purposes=["background_observe"])
def collect_process_report(
    task_process_report: Annotated[str, "<复用 report_task_outcome 的 task_process_report 参数描述原文>"],
) -> ControlResult:
    # 零状态写：不碰 task.status/observer_outcome/actor_done/process_report/error。
    return ControlResult(content=task_process_report)
```

- `purposes=["background_observe"]` 使其只在 background 装配里绑定。
- handler **只回传内容**，background observe 从 `ControlResult.content` 取段总结文本。

### 3.3 boundary 状态标签（触发点 → cue）

`launch_background_observe(state, ctx, *, boundary: str)` 增参。6 个触发点按场景传：

| 触发点 | boundary | cue 状态描述 |
|---|---|---|
| act ② 流中打断 / 工具前 / 工具中 / 检查点（act.py 217/334/350/553） | `interrupt` | 「本段被用户打断」 |
| act 纯文本暂停（act.py 389） | `plain_text` | 「actor 散文回复后让位用户、暂停等待」 |
| observe finish 收尾（observe.py 82，`actor_done`） | `finish` | 「任务已通过 finish_task 收尾」 |
| observe 正常产出（observe.py 82，`normal`） | `normal` | 「任务以最终产出正常结束」 |

`boundary` 存入 `snapshot.extra["observe_boundary"]`，装配 `purpose=background_observe` 时 cue 读它生成状态描述。

### 3.4 background observe 实现改造（background_observe.py）

`_run_background_observe` 不再调 `summarize_for_compact`；改：

1. 跑 observe 风格 ReAct（复用 §3.6 抽出的公共骨架），`purpose="background_observe"`，绑 `collect_process_report`。
2. 取工具回传的 `process_report` 文本。
3. **按 boundary 分流**：
   - `interrupt` / `plain_text` → `apply_compact(layer=TASK, summary=process_report, keep_last=0,
     protect_types=(USER_PROMPT,))` 写一条 `TASK_COMPACT_SUMMARY`（role=assistant）。**与现状同形，仅内容来源换成 observe persona。**
   - `finish` / `normal` → **不写 memory**；把 `process_report` 存入**结果槽** `_close_report[task.id] = process_report`
     （模块级 dict，类似现有 `_task_pending`）。
4. 失败吞掉（best-effort，§3.7 降级）。

并发：同一 task 至多一个在跑（沿用 `_task_locks` 串行）。

### 3.5 observe 快速占位 + root close fire（observe.py）

- **`_rule_observe` 保留**（不删）：对 root close 与两个 LLM 兜底，快速返回薄 verdict
  （outcome 推断 + 薄 summary 占位）。**唯一变化**：root close 路径的薄 summary 仅作**临时占位**，
  后续由 background observe 替换（§3.6）。
- observe.execute 对 root 在 `{normal, actor_done}` 边界 `launch_background_observe(boundary=...)`，
  **不 await**（方案甲，与现状同——现状 execute:82 即 fire-and-forget）。
- 指令 2（§3.8 default ROLE 回退）落地后，`_should_use_llm` 的「无 ROLE」分支几乎失效；
  `_rule_observe` 主要服务 root close 占位 + LLM 兜底。
- 非 root 子任务：完整 LLM observe（`report_task_outcome`）不变。

### 3.6 finalize close 路径 A1（finalize.py，机会性 + 异步替换）

`_synthesize_dispatch_pair` 合成 finish 对时（finalize.py:270-311），Process Report 来源改为：

1. **机会性直接用**：查结果槽 `_close_report.pop(task.id, None)`：
   - 命中（background observe 已完成）→ 用它作 finish 对的 Process Report（`report_only`）。
   - 未命中 → 用 `mem_content` 里的薄占位（`verdict.summary`，来自 `_rule_observe`），并记下 finish-对 tool
     记录的 `tool_call_id` + `origin_task_id` 供回调替换。
2. **异步替换回调**：`launch_background_observe` 的 close 路径完成回调中，若产出时 finish 对已合成
   （结果槽被 finalize 取走说明已合成，或检测 finish 对 tool 记录已存在）→
   在 agent scope 定位 finish 对 tool 记录（`AGENT_CONVERSATION_TURN`，role=tool，`origin_task_id==task.id`，
   content 以 `Process Report:` 开头）→ `supersede` 旧记录 + `ingest` 新记录（同 `tool_call_id` 配对、
   content = `Process Report: {新 process_report}`，保留 fail 前缀）。
3. **协调通道**：`_close_report`（结果槽）+ 一个轻量「finish 对已合成」标记（如 `_close_synth_done[task.id]`），
   使回调与 finalize 二者无论先后都收敛：
   - finalize 先：取不到槽 → 占位 + 标 `_close_synth_done`；background 后到 → 见标记 → 执行替换。
   - background 先：写槽；finalize 后到 → 取槽直接用 → **不触发替换**（pop 后回调见槽已空，no-op）。

> close 路径的 background observe **不** `apply_compact`、**不**写 `TASK_COMPACT_SUMMARY`——末段由 finish 对独家承载（不变量 3/4）。

### 3.7 失败降级

- background observe LLM 失败（自愈耗尽）→ 吞掉：
  - 打断/暂停路径：该段**不写**段摘要，保 raw（与现状 §3.6 同）。
  - close 路径：结果槽空 → finalize 用薄占位（`_rule_observe` summary），**不替换**。胶囊永远能合成，绝不因摘要失败而崩。

### 3.8 default ROLE 回退（resolver.py）

`DEFAULT_MERGE_PURPOSES`（resolver.py:30）从 `("compact", "recognize_intent")` 改为
`("compact", "recognize_intent", "observe")`。`merge_default_facets` 即把无 ROLE 模板缺失的 `observe`
facet 从 default 模板补入。planner 等只有 SOUL.md 的模板从此借 default 的 ROLE.md → 走 LLM observe，
不再走 `_rule_observe`（仅 root close 占位 + 兜底仍用它）。

### 3.9 不动的部分

- COMPACT.md persona、`summarize_for_compact` 本身（仍服务 max_turns / context_limit / prepare 同步压缩）。
- `_maybe_compact_task`（max_turns）、prepare 的 `_should_compact` / `_compact_scope`。
- 非 root 子任务的 LLM observe（`report_task_outcome`）+ bubble。
- ROLE.md / COMPACT.md / METADATA.md 文本（整体共用，不拆）。
- 段摘要 role=assistant（上一 spec）。

---

## 4. 数据流（root 任务）

```
打断/暂停段边界（act）:
  park 前 launch_background_observe(boundary=interrupt|plain_text)  ── fire-and-forget
    → ReAct(purpose=background_observe, ROLE.md + bg cue + collect_process_report)
    → process_report → apply_compact(TASK) 写 TASK_COMPACT_SUMMARY(assistant)  ── 段摘要，下轮续跑召回 / close 进胶囊

close/finish 段边界（observe → finalize）:
  observe: _rule_observe 快速薄 verdict（outcome + 薄 summary 占位）
           launch_background_observe(boundary=finish|normal)  ── fire-and-forget，不 await
  finalize: _synthesize_dispatch_pair
            ├─ 结果槽命中 → finish 对 Process Report = background 产出
            └─ 未命中 → 薄占位 + 标记，待回调替换
  background 完成回调:
            ├─ finalize 已合成 → supersede 旧 finish 对 tool + 写新（替换薄占位）
            └─ finalize 未合成 → 写结果槽，finalize 直接取用（不触发替换）
```

---

## 5. 风险 / 边界

- **R1 · close 后异步改胶囊的并发**：task close 后唯一的后续写是 background observe 回调；用 `task.id`
  定位 finish 对 tool 记录、`supersede + ingest` 配对完整。结果槽 + 合成标记保证回调与 finalize 收敛（§3.6）。
- **R2 · 替换前被召回**：替换发生前若新 root task 已召回旧（薄）Process Report，看到的是薄占位；替换后召回得新。
  可接受（薄占位语义正确、非错误）；多数情况 background 与 finalize 几乎同时，结果槽机会性命中即无占位窗口。
- **R3 · background observe token 成本**：每个 root 段边界一次 observe 风格 LLM（与现状一次 compact LLM 同量级）。
  沿用现有「best-effort 失败降级」（§3.7）。
- **R4 · 状态隔离回归**：`collect_process_report` 零状态写是不变量 1 的承重点；测试须断言调用后 task 六字段不变。

---

## 6. 测试计划（`uv run pytest`）

- **T1 · 精简工具零状态写**：调 `collect_process_report` 后断言 `task.status/observer_outcome/actor_done/
  process_report/error` 全不变，返回 content == 入参。
- **T2 · facet 回退链**：`background_observe` 缺失 → IdentitySource 取 `observe`（ROLE.md）；observe 也缺 → `act`。
- **T3 · cue 按 boundary**：装配 `purpose=background_observe`，四种 boundary 各生成对应状态描述 + 「只给 process_report」。
- **T4 · 打断路径写段摘要**：boundary=interrupt，background observe 产出 → 一条 `TASK_COMPACT_SUMMARY`(role=assistant)，
  内容来自 observe persona（mock LLM 固定返回），USER_PROMPT 保留。
- **T5 · close 路径不写 memory**：boundary=finish，断言**无** `TASK_COMPACT_SUMMARY` 写入；产出落结果槽。
- **T6 · A1 机会性命中**：background 先于 finalize 完成 → finalize 取结果槽，finish 对 Process Report = background 产出，
  **无**替换发生。
- **T7 · A1 异步替换**：finalize 先合成（薄占位）→ background 后完成 → supersede 旧 finish 对 tool + 写新，
  配对（tool_call_id）完整、不悬挂；最终 Process Report = background 产出。
- **T8 · 状态隔离端到端**：root 在打断边界 fire background observe，断言当前 task 状态（status/actor_done）
  在 background observe 运行前后不变（不被误判 FINISHED/PENDING）。
- **T9 · _rule_observe 仍快速占位**：root close，background observe 未完成时 finalize 用 `_rule_observe` 薄 summary 占位。
- **T10 · default ROLE 回退**：无 ROLE 模板经 `merge_default_facets` 得 default 的 observe facet；`_should_use_llm`
  对其非 root 子任务返回 True（走 LLM observe）。
- **T11 · 失败降级**：background observe LLM 抛错 → 打断路径段保 raw / close 路径用薄占位，胶囊仍合成，主流程不崩。
- **回归**：max_turns/context_limit 仍走同步 COMPACT.md 压缩（不 fire background observe）；非 root 子任务 LLM observe 不变。

---

## 7. 受影响文件清单（writing-plans 细化排序）

- `core/orchestrator/control_capability.py` — 新 `collect_process_report`（§3.2）。
- `core/assembler/composer.py` — `_BACKGROUND_OBSERVE_INSTRUCTION` cue + purpose 分支（§3.1）。
- `core/assembler/sources/identity.py` — facet 回退链 `background_observe → observe → act`（§3.1）。
- `core/loop/steps/background_observe.py` — 走 observe ReAct + boundary 分流 + 结果槽 + close 回调替换（§3.4/§3.6）。
- `core/loop/steps/observe.py` — `_rule_observe` 留作占位 + 抽公共 ReAct 骨架 + 传 boundary（§3.5/§3.6）。
- `core/loop/steps/act.py` — 5 个触发点传 boundary（§3.3）。
- `core/loop/steps/finalize.py` — close 路径 A1 机会性用 + 异步替换（§3.6）。
- `src/ipmastercowork/providers/templates/resolver.py` — `DEFAULT_MERGE_PURPOSES` 加 `"observe"`（§3.8）。

> 同步约定（memory：上游 wefta→weft）：动 core 落 `ctx-weft`；host resolver 落本仓；若需回灌上游按既有流程。

## 8. 验收

- background observe 走 observe persona（ROLE.md）+ 精简工具，产 process_report 风格段总结。
- 打断/暂停 → `TASK_COMPACT_SUMMARY`；close/finish → finish 对 Process Report（不写 memory 段摘要）。
- close 不阻塞（A1 机会性 + 异步替换），当前 task 状态零污染。
- 无 ROLE 模板借 default ROLE 走 LLM observe。
- §6 全部测试 + `uv run pytest` 全绿；capsule / 段摘要 role 既有回归不破。
