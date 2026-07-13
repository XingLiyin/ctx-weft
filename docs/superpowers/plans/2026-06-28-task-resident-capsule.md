# Task-Resident 胶囊 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 把任务胶囊从「close 时镜像进 agent 层」改为「留在各自 task 层、自身即胶囊」;agent 层每 task 只留 finish 对;召回靠现有 `(timestamp, seq_no)` 归并(无新键),靠回锚纪律保证嵌套语序;跨层 fold 三级降级。

**Architecture:** 五步:① 模型翻转(close 只写 finish 对、不镜像、不 supersede body、全合成,删 `_gc_subtree`/`close_finished_short_tasks`,所有 task 留 raw body);② 长任务 close 时压末段 body;③ OPEN/CLOSED 判据改 `task.status` + AgentRecall 语义;④ 跨层 fold L0→L1→L2;⑤ 回锚纪律核查 + 嵌套/异步语序 golden。**无新增字段、无新排序键。**

**Tech Stack:** Python 3.11,ctx-weft core,InMemoryMemoryProvider(测试)/PostgresMemoryProvider(SQLite),pytest(`cd ctx-weft && unset VIRTUAL_ENV && uv run pytest ...`)。

## Global Constraints

- Spec:`docs/superpowers/specs/2026-06-28-task-resident-capsule-design.md`。
- **不变量 A(回锚)**:凡摘要/finish/异步回填记录的 `timestamp` 必须锚到所代表段的**逻辑时间**,绝不用写入时刻。
- **无新字段、无 order_key/order_path**:排序沿用现有 `(timestamp, seq_no)`。
- **不做**:count-based 增长上限(`compact_closed_body_delta`)。L2 文本 fold **做**。
- `short_task_*` 两 LoopConfig 字段**保留**,语义改为「body 留 raw vs 压末段」。
- 测试从 `ctx-weft/` 跑;提交信息末尾加 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。

---

## 文件结构(改动地图)

| 文件 | 改动 | 步 |
|---|---|---|
| `core/loop/steps/finalize.py` | `_synthesize_dispatch_pair` 只写 finish 对;`_close_one` 无条件合成、删 body supersede、删 `_gc_subtree`;`finalize_task_memory` 删 short 合成门;`_is_short_leaf` 改判 long;删 `close_finished_short_tasks` | ①②③ |
| `core/loop/steps/compact.py` | 删 `close_finished_short_tasks` 调用;`fold_root_experience` 改跨层三级 + `keep_pair` | ①④ |
| `core/loop/steps/act.py` | 长任务末段压缩用到的 `apply_compact`（已有）| ② |
| `core/assembler/sources/agent_recall.py` | 第①段注释/语义改 | ③ |
| `protocols/template.py` | `LoopConfig` 加 `compact_keep_pair: int = 30` | ④ |
| `tests/unit/test_capsule_golden.py` 等 | golden 改：finish-对-only + body 留 task 层 | ①②⑤ |

---

## Task 1: 模型翻转（task-resident 核心）

把 close 从「镜像 body + supersede」改为「只写 finish 对 + 留 raw body」,并移除会删掉新胶囊的 `_gc_subtree` / `close_finished_short_tasks`。本 task 后:**所有结束 task = task 层 raw body + agent 层 finish 对**(长任务压缩留 Task 2)。

**Files:**
- Modify: `finalize.py`（`_synthesize_dispatch_pair` 230-330、`_close_one` 99-180、`finalize_task_memory` 85-96、删 `_gc_subtree` 与 `close_finished_short_tasks`）、`compact.py:209-218`（删调用）
- Test: `tests/unit/test_capsule_golden.py`（A1/A3/A4/A6/A10/H8 改）、新 `tests/unit/test_task_resident.py`

**Interfaces:**
- Produces:close 后 agent 层每 own-root/cross-root = 2 条 finish 对(`AGENT_CONVERSATION_TURN`,`origin_task_id`/`parent_task_id`,timestamp 锚 close);同 agent 子任务 = 派发对(`TASK_DISPATCH`+`TASK_DISPATCH_RESULT`)+ 子自己 finish 对;**task 层 body 不被 supersede**。

- [ ] **Step 1: 写失败测试**（`test_task_resident.py`）

```python
# 用现有 test_capsule_golden 的 helper 风格（InMemoryMemoryProvider, _ev, _task_scope, _state, _loop_ctx, finalize_task_memory）
async def test_close_writes_finish_pair_keeps_body():
    mem = InMemoryMemoryProvider(); tsc = _task_scope("t1"); asc = _agent_scope()
    await mem.ingest(_ev(T.USER_PROMPT, tsc, "做X", 1, role="user"), _pctx())
    await mem.ingest(_ev(T.LLM_RESPONSE, tsc, "在做", 2, role="assistant"), _pctx())
    await mem.ingest(_ev(T.TOOL_RESULT, tsc, "ok", 3, role="tool"), _pctx())
    task = _make_task(outputs="完成")
    await finalize_task_memory(mem, _state(task, tsc, LoopConfig()), task,
                              "完成\n\nProcess Report: 成功", "success", _loop_ctx(mem))
    # agent 层：恰 2 条 finish 对
    caps = await mem.recall_recent(asc, [T.AGENT_CONVERSATION_TURN], 500, _pctx())
    assert len(caps) == 2
    assert caps[-1].role == "tool"  # Process Report
    asst = [c for c in caps if c.role == "assistant"][0]
    assert asst.metadata["tool_calls"][0]["name"].endswith("finish_task")
    # task 层 body 仍在（未 supersede）
    body = await mem.recall_recent(tsc, [T.USER_PROMPT, T.LLM_RESPONSE, T.TOOL_RESULT], 500, _pctx())
    assert len(body) == 3
```

- [ ] **Step 2: 跑确认失败** — `cd ctx-weft && unset VIRTUAL_ENV && uv run pytest tests/unit/test_task_resident.py -v`（现状会镜像 N 条 + supersede body）。

- [ ] **Step 3: 实现**
  - `_synthesize_dispatch_pair`:删 step2 的 survivors 镜像循环(:231-269),只保留 step3 写 finish 对(:271-330)。finish 对 timestamp 已是 `base=now_utc()`(close 时刻,符合不变量 A)。
  - `_close_one`:line 155 `if is_own_root and mem_content and not short` 改为 `if is_own_root and mem_content`(无条件合成);**删 line 164 `_supersede_own_conversation` 整块**;**删 line 172-178 `_gc_subtree` 调用**;同 agent 子任务分支(119/150)去掉 `not short` 条件(无条件 bubble + 子 finish 对)。
  - `finalize_task_memory`:删 `_is_short_leaf` 用于「合成-or-不」的逻辑(short 不再 gate 合成);`_close_one` 去掉 `short` gate（`short` 参数 Task 2 重新用于 body 决策，本 task 先让它不影响合成/supersede）。
  - 删 `_gc_subtree`(:163-180 函数)、`close_finished_short_tasks`(:183-216);`compact.py:209-218` 删 `close_finished_short_tasks` 调用与 import。
  - `_supersede_own_conversation`(:64-72)本 task 不再被调,可保留待 Task 3 删或本 task 一并删。

- [ ] **Step 4: 改 golden** — `test_capsule_golden.py` 的 A1/A3/A4/A6（期望「镜像回合+finish 对」）改为「仅 finish 对(2 条)、body 留 task 层」;A10/H8（短任务负向「不合成」）改为「合成 finish 对、body 留 task 层」。

- [ ] **Step 5: 跑全量 unit** — `uv run pytest tests/unit -q -p no:warnings`,绿。

- [ ] **Step 6: Commit** — `feat(capsule): task-resident — close 只写 finish 对、留 raw body、删 gc_subtree/short 回收`

---

## Task 2: 长任务 close supersede 末 raw 段

短任务 raw 原样留(Task 1 已是);长任务 close 时 **supersede task 层末 raw 段**(末锚点后的 `LLM_RESPONSE`/`TOOL_RESULT`/`TOOL_INVOCATION`),保留 `USER_PROMPT` + `TASK_COMPACT_SUMMARY` 锚点。**不另产段摘要**——末段已由 finish 对的 Process Report 承载(不变量 4),另产会重复。中间段在边界时已被后台 observe 折成段摘要,故等价「supersede 所有 active 的 raw LLM/TOOL,留锚点」。顺手清掉 Task 1 遗留的 dead `descendants` 参数。

**Files:**
- Modify: `finalize.py`（`finalize_task_memory`/`_close_one`:`_is_short_leaf` 判 long → supersede 末 raw 段;删 dead `descendants` 参数）
- Test: `tests/unit/test_task_resident.py`

**Interfaces:**
- Consumes: `_is_short_leaf`(现有:轮次≤`short_task_turn_cap` 且 token≤`short_task_token_threshold` → short)、`memory.supersede`、`memory.recall_recent`。
- Produces: 长任务 close 后 task 层 = `[USER_PROMPT 锚点…][TASK_COMPACT_SUMMARY…]`(raw LLM/TOOL 已 supersede);短任务 = 全 raw 不变。

- [ ] **Step 1: 写失败测试** — (a) 短 task(1 轮、token 小)close → body 仍含 raw `LLM_RESPONSE`/`TOOL_RESULT`(全留)。(b) 长 task(轮次 > `short_task_turn_cap` 或 token > `short_task_token_threshold`)close → body 的 active `LLM_RESPONSE`/`TOOL_RESULT` 被 supersede(不再被 recall_recent 返回),`USER_PROMPT` 与既有 `TASK_COMPACT_SUMMARY` 锚点保留;agent 层 finish 对仍在(Task 1)。

- [ ] **Step 2: 跑确认失败。**

- [ ] **Step 3: 实现** — `finalize_task_memory`/`_close_one`:若 `not _is_short_leaf(...)`(长),召回 task scope 的 active `LLM_RESPONSE`/`TOOL_RESULT`/`TOOL_INVOCATION`,`memory.supersede` 之(保留 `USER_PROMPT`/`TASK_COMPACT_SUMMARY`)。**不写新 `TASK_COMPACT_SUMMARY`**。短任务跳过。同时删 `_close_one`/`finalize_task_memory` 里现已无用的 `descendants` 参数与 `_descendant_task_ids` 调用(Task 1 review Minor #1)。

- [ ] **Step 4: 跑测试 + 全量 unit 绿。**

- [ ] **Step 5: Commit** — `feat(capsule): 长任务 close supersede 末 raw 段（短留 raw），清 dead descendants`

---

## Task 3: OPEN/CLOSED 判据改 status + AgentRecall 语义

**Files:**
- Modify: `agent_recall.py:1-46`（注释/语义）、`finalize.py`（删残留 `_supersede_own_conversation` 若 Task 1 未删）
- Test: `tests/unit/test_open_closed_recall.py`（新建）

**Interfaces:**
- Produces: 结束 task 召回 = `[body][finish 对]`;在跑/暂停 task = `[body]` 无 finish 对。判据走 `task.status`/finish 对存在,不靠 supersede。

- [ ] **Step 1: 写失败测试** — (a) 已结束 root：召回含其 body + finish 对。(b) 暂停(wait_for_user,status≠FINISHED)task：召回含 body、**无** finish 对(活对话)。(c) 跨 agent 子任务 body 不进父 prompt（不同 agent_id）。

- [ ] **Step 2: 跑确认失败 / 确认现状。**

- [ ] **Step 3: 实现** — `agent_recall.py` docstring/注释把「OPEN task 全对话」改为「所有未折叠 task 的 body(结束的带 finish 对、在跑的不带)」;确认 `_supersede_own_conversation` 已无调用并删除;确认无代码再依赖「task 层非 superseded = OPEN」假设(grep 审 `close_finished_short_tasks`/`_gc_subtree` 已删)。

- [ ] **Step 4: 跑测试 + 全量 unit 绿。**

- [ ] **Step 5: Commit** — `refactor(capsule): OPEN/CLOSED 判据改 task.status，AgentRecall 语义更新`

---

## Task 4: 跨层 fold L0→L1→L2

**Files:**
- Modify: `compact.py`（`fold_root_experience` + `_count_root_residues`）、`protocols/template.py`（`LoopConfig.compact_keep_pair: int = 30`）
- Test: `tests/unit/test_cross_layer_fold.py`（新建）

**Interfaces:**
- Consumes: agent 层 finish 对(`origin_task_id`/`parent_task_id`)、task 层 body(`task_id`/`metadata.task_id`)。
- Produces: 超 `keep_full`(=`compact_keep_last`) → L1(删该单元 task 层 body、留 finish 对);超 `keep_pair` → L2(删 finish 对 + 旧摘要 → 一条 `AGENT_COMPACT_SUMMARY`)。

- [ ] **Step 1: 写失败测试** — keep_full=2:开 4 个结束 root,fold → 最老 2 个的 task 层 body 被 supersede、finish 对仍在(召回 = `[finish 对]`);keep_pair=3:最老 1 个 finish 对也被 supersede + 一条 AGENT_COMPACT_SUMMARY。

- [ ] **Step 2: 跑确认失败。**

- [ ] **Step 3: 实现** — `fold_root_experience`:召回 agent 层 finish 对 + (跨层)task 层 body;按 `origin_task_id` 分顶层单元(parent 链 `_expand` 纳子树);超 keep_full 的单元收集其 task 层 body id → `supersede`(跨层一次提交);超 keep_pair 的再收 finish 对 id + 旧 AGENT_COMPACT_SUMMARY → supersede + 写新 AGENT_COMPACT_SUMMARY(锚 `anchor_ts-1µs`)。`_count_root_residues` 数「有 body 的结束顶层单元」。

- [ ] **Step 4: 跑测试 + 验证 provider supersede 跨 scope by-id** — 读 `src/ipmastercowork/providers/memory/postgres.py` 的 `supersede`,确认按 id `WHERE id IN (...)`、不按 scope 过滤;InMemory 同。全量 unit 绿。

- [ ] **Step 5: Commit** — `feat(fold): 跨层 fold L0→L1→L2（删 body→留 finish 对→折文本）`

---

## Task 5: 回锚纪律核查 + 嵌套/异步语序 golden

**Files:**
- Test: `tests/unit/test_nesting_order.py`（新建）
- Modify(若核查发现破): 相应锚定点

**Interfaces:**
- 验证不变量 A 在 task-resident 模型下成立。

- [ ] **Step 1: 写 G1 golden** — 构造:父(同 agent)委派子,子做两段、父续跑、各自 finish;**子的段摘要用「晚到的写入顺序但锚定早 ts」ingest**(模拟后台 observe 回填)。断言 composer 装配的 message 序 = `[父 body…delegate 对][子 body…子 finish 对][父续跑…父 finish 对]`,delegate 对/finish 对各自相邻,子树不夹在 tool_use/tool_result 之间。

- [ ] **Step 2: 跑** — 若失败,定位是哪条记录的 timestamp 用了写入时刻而非逻辑时刻(核查 `apply_compact`/finish 对/`fold` 的锚定),修该锚定点。

- [ ] **Step 3: 加 G2-G4 断言**（短留 raw、长压末段、跨 agent 黑盒;多数已在 Task 1-3 覆盖,此处补嵌套场景）。

- [ ] **Step 4: 全量 unit 绿。**

- [ ] **Step 5: Commit** — `test(capsule): 嵌套+异步回填语序 golden + 回锚核查`

---

## Self-Review 待办

- **G7 前向兼容**:本设计无新字段,旧式镜像胶囊记录仍走 `(timestamp, seq_no)` 装配。把 Task 1 前的一份 golden 另存 `test_legacy_capsule_recall.py`,断言旧式记录(agent 层镜像回合)仍能装配(不回归)。
- **`apply_compact` user-aware**:Task 2 依赖它保 USER_PROMPT 锚点;确认现状已 user-aware。
- **provider supersede 跨层**:Task 4 Step 4 显式验证。
- **暂停 task 仍 OPEN**:Task 3 Step 1 (b) 覆盖。
