# Task-Resident 胶囊 设计

> 状态:草案(待评审)。取代并合并 `2026-06-28-task-resident-capsule-ordering-design.md`(path-B 排序,已废弃)与 `2026-06-28-task-resident-capsule-lifecycle-design.md`。关联:`2026-06-26-interaction-preserving-capsule-design.md`(被本设计部分回退)。

## 0. 一句话

每个 task 结束后**胶囊就留在它自己的 task 层**(短任务=raw 逐条 / 长任务=user 锚点+段摘要),**不镜像进 agent 层**;agent 层每 task 只留 **finish 对**(`finish_task` 调用 + Process Report);召回时 task 层 body 与 agent 层 finish 对/派发对**按(已锚定的)timestamp 一起归并**,嵌套语序自然还原——**不引入任何新排序键/字段**。fold 三级降级。OPEN/CLOSED 改由 `task.status` 判,不靠 supersede。

---

## 1. 模型

### 1.1 现状 → 目标
| | 现状(交互保留胶囊) | 本设计 |
|---|---|---|
| body(user 锚点+段摘要/raw) | close 时**镜像**成 agent 层 `AGENT_CONVERSATION_TURN` + supersede task 层 | **留 task 层**,不镜像、不 supersede |
| agent 层 / 每 task | 完整胶囊 N 条 | **仅 finish 对** 2 条 |
| 召回 | 主要靠 agent 层 | task 层 body + agent 层 finish 对/派发对一起 |
| 排序 | `(timestamp, seq_no)` | **不变**:`(timestamp, seq_no)`,靠回锚保证语序 |

### 1.2 收益
- agent 层极简(每 task 2 条),写量/fold churn 大降。
- fold 天然分级(删 task body → 自然剩 `[call+result]`,见 §4)。
- 嵌套/隔离按 `agent_id` 跨-task 召回自然涌现(§3)。
- **零新增字段、零新排序键**——比 path-B(order_path/order_key)简单得多。

---

## 2. 排序:锚定时间戳(无新键)

### 2.1 为什么 timestamp 够用
一次召回的全部记录散落在多个 task 层 scope + agent 层。`seq_no` 每 scope 各自计数、**跨 scope 不可比**;但 **`timestamp` 是 wall-clock、天然跨 scope/层可比**。一个 task 内 body 与 finish 对/派发对、跨 task 的父子嵌套,**只要时间戳反映逻辑顺序,`(timestamp, seq_no)` 归并即还原正确(先序)语序**。

嵌套:子任务在父 SUSPEND 期间跑,子记录时间戳天然落在父 delegate 与父 resume 之间 → 归并即内联。

### 2.2 唯一不变量:**回锚纪律**
唯一会破坏时间戳语序的是**异步/迟到写入拿到「写入时刻」而非「逻辑时刻」**(后台 observe 在 close 后 ~18s 才回填段摘要)。故:

> **不变量 A(回锚)**:凡**摘要 / finish / 任何异步回填**的记录,其 `timestamp` 必须锚到**它所代表段的逻辑时间**,绝不用 wall-clock 写入时刻。

现状已基本满足,沿用既有范式:
- `apply_compact` 的段摘要锚到被折段(保留段内最早/锚点 ts)。
- finish 对锚到 close 时间(`base = now_utc()` at close)。
- `fold_root_experience` 锚 `anchor_ts - 1µs`。
- 同 agent「scheduled」`TASK_DISPATCH_RESULT` 锚**派发时刻**(交互保留 spec §3.9,使 delegate 对相邻、子树落其后)。

切换到 task-resident 后需**确认这些锚定不破**,并加嵌套+异步回填语序测试兜底(§6 G1)。

### 2.3 tie-break
`(timestamp, seq_no)` 中 seq_no 为次级。逻辑同位(同 timestamp)且**同 scope**的记录(如 finish 对的 assistant/tool、delegate 对)由该 scope 内 seq_no 决定,正确。跨 scope 同 timestamp 极罕见(事件串行、µs 分辨率);如出现由现状行为决定,不专门处理(YAGNI)。

---

## 3. close 合成(task-resident)

### 3.1 所有 task 结束即合成(取消 short 延迟)
取消「short → 不合成、延迟回收」。**每个 terminal task** close 时:
- **own-root / cross-agent root**:agent 层写 **finish 对**(`finish_task(result=outputs)` + `tool: Process Report`),`origin_task_id=task.id`、`parent_task_id`。**不镜像 body。**
- **同 agent 子任务**:agent 层写**派发对**(`TASK_DISPATCH` + `TASK_DISPATCH_RESULT="…scheduled"`,锚派发时刻)+ 自己的 finish 对。子 body 留子自己 task 层(召回时按 agent_id 跨-task 拉回、时间戳穿插内联)。
- **跨 agent 子任务**:派发对 `TASK_DISPATCH_RESULT = 子 mem_content`(黑盒);子 body 在子 agent scope(父 agent_id 不同 → 召不到)。

### 3.2 body:短留 raw / 长压末段
close **不再 supersede 自身 body**(body 即胶囊):
- **短任务**(轮次 ≤ `short_task_turn_cap` 且 token ≤ `short_task_token_threshold`):task 层 raw **原样留**。
- **长任务**:close 时 **supersede task 层末 raw 段**(末个锚点之后的 `LLM_RESPONSE`/`TOOL_RESULT`/`TOOL_INVOCATION`),**保留 `USER_PROMPT` + `TASK_COMPACT_SUMMARY` 锚点**。中间段在各自边界(HITL/打断)已由后台 observe 折成段摘要;**末段由 finish 对的 Process Report 承载**(不变量 4)→ 直接 supersede、**不另产段摘要**(避免与 finish 对重复)。即 body = `[user 锚点][中间段摘要…]`。
  > 等价于旧模型的 `_fold_final_segment_raw`,但作用在 task 层而非已删的 agent 层镜像。短任务跳过(留全 raw)。

> `short_task_*` 两个 LoopConfig 字段**保留**,语义从「合成-or-不」改为「body 留 raw vs 压末段」。

### 3.3 召回拼装
`AgentRecallSource` 两段召回不变(task 层 by-agent 跨-task + agent 层);composer 按 `(timestamp, seq_no)` 归并。结束 task → `[body][finish 对]`;在跑 task → `[body]`(无 finish 对)。

---

## 4. 跨层 fold:三级降级

| 级 | 内容 | 触发 |
|---|---|---|
| **L0 完整** | task 层 body + agent 层 finish 对 | 最近 `keep_full` 个顶层单元 |
| **L1 黑盒** | **删 task 层 body**,仅剩 agent 层 finish 对 = `[call+result]` | `keep_full` 之外 |
| **L2 文本** | 连 finish 对折成一条 `AGENT_COMPACT_SUMMARY` | `keep_pair` 之外(最老) |

- **折叠单元** = 一个顶层 task 及其派发子树。顶层 = `parent_task_id is None` 或 parent ∉ 本 scope 召回集。子树:沿 `parent_task_id` 链纳入(沿用现有 `_expand`)。
- **跨层 supersede 原子**:一次 `supersede(ids)` 同时软删 task 层 body + (L2 时)agent 层 finish 对。要求 provider `supersede` **按 id 生效、不按 scope 过滤**(§6 验证)。
- **触发**:复用 `_should_compact`(token/条数)。**不做** count-based 增长上限(本版延后,见 §5)。
- `keep_full`(沿用 `compact_keep_last`=6)、`keep_pair`(新 LoopConfig,默认 30)。

---

## 5. OPEN/CLOSED 判据反转

旧:task 层非 superseded = OPEN(CLOSED 被 supersede)。本设计 CLOSED body 留 task 层 → 失效。

**新判据**:CLOSED ⟺ `task.status==FINISHED`(等价:有该 origin 的 finish 对);OPEN ⟺ 在跑/SUSPENDED/暂停,无 finish 对。

逐点改动:
| 位置 | 现状 | 改动 |
|---|---|---|
| `finalize.py:_supersede_own_conversation` | close 软删 body | **移除**;长任务改 apply_compact 末段 |
| `finalize.py:close_finished_short_tasks` | 短 task 滞留回收 | **删除**(并入 fold) |
| `finalize.py:_gc_subtree` | GC 后代 raw 残留 | **删除**(后代 body 即胶囊,由 fold 管) |
| `finalize.py:_is_short_leaf` | 决定合成-or-不 | 改为决定 body raw-vs-压末段(§3.2) |
| `compact.py:fold_root_experience` | 单层 agent fold | **改跨层三级**(§4) |
| `agent_recall.py` 第①段 | 注释「OPEN task 全对话」 | 改为「所有未折叠 task 的 body」 |

---

## 6. 不做 / 延后

- **lifecycle 增长上限(count 触发)**:CLOSED body 留 task 层,小任务永不到 token 压力 → 无界堆积。本版**只靠 token 触发的 fold**,count-based 触发(`compact_closed_body_delta`)**延后**。
- path-B(order_path/order_key/intra_seq):**废弃**,本设计用锚定时间戳替代。

---

## 7. 验收要点

- **G1 嵌套+异步回填语序**:父委派同 agent 子任务,模拟段摘要**晚到但回锚逻辑 ts**后,装配 message 序仍 = `[父 body..delegate 对][子 body..子 finish 对][父续跑..父 finish 对]`;delegate 对/finish 对各自相邻、子树不夹在 tool_use/result 之间。
- **G2 短留 raw**:短 task close 后 raw 仍在 task 层、可召回;agent 层有 finish 对。
- **G3 长压末段**:长 task close 后 body = `[user][段摘要]`,raw superseded,finish 对在 agent 层。
- **G4 跨 agent 黑盒**:跨 agent 子 body 不进父 prompt(仅派发对 result=mem_content)。
- **G5 fold L1/L2**:超 keep_full → task body 删、留 finish 对;超 keep_pair → finish 对折成 AGENT_COMPACT_SUMMARY。provider supersede 跨 scope by-id 生效。
- **G6 OPEN/CLOSED**:在跑 task body 召回为活对话(无 finish 对);判据走 status,不靠 supersede。
- **G7 前向兼容**:旧式(镜像在 agent 层的)胶囊记录仍能被现有召回+`(timestamp, seq_no)` 装配(不依赖新字段——本设计本就无新字段)。
