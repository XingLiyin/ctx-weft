# Root 自经验承载 compaction summary + agent 层折叠对齐

- 日期：2026-06-26
- 范围：`finalize.py`、`compact.py`（核心修复）；`assembler/sources/agent_experience.py`（assistant
  summary 渲染 + §2.4 包装）、`assembler/sources/short_memory.py`（task 层 summary 包装）、
  `core/loop/llm_gateway.py`（§2.5 `ensure_leading_user`）；`core/loop/driver.py`、`core/runtime.py`、
  `core/loop/steps/suspend.py`、`assembler/composer.py`（§2.6 current-message 框架）。均在 `ctx_weft/` 下
- 相关 spec：`docs/spec/06-memory-layers-and-compaction.md`

## 1. 问题

会话 `ses_01KW29FMC3Z7AQHPG54NXCAE0W`（root task `DFK8X5`，其中含子任务 `tsk_01KW29YXN1XS2TAFKPR0QRVW25` PPTX转PDF）复现到一个 bug：

> 在某个 task 结束之前，prompt 组织正确；该 task 结束之后，原本第一条「总结性的 message」消失，被「用户打断之后说的一句话」取代。

### 1.1 现象（证据）

root task `DFK8X5` 被 compact 过两次，最近一次（15:49:39）产出 task 层摘要
`task_compact_summary`（`### 会话目标 / ### 已完成工作`，seq96，**只存在于 task 层**），它把
真正的原始诉求（seq38「帮我把这个ppt写成pdf」）折叠 superseded。

`DFK8X5` resume 后于 15:59:33 `finish_task` close。close 之后用户继续发消息（16:00:57、
16:02:28），assembler 从 agent 层 `agent_experience` 装配，prompt `[0]` 变成
「你为什么不使用技能呢」——而 `### 会话目标` 摘要彻底消失。与报告逐字吻合。

### 1.2 根因

两条相互作用：

**根因 A — `_synthesize_dispatch_pair` 用错「原始用户消息」**
close 时（`_close_one` step 2，早于 step 3 的 supersede）该函数把「原始诉求」镜像到 agent 层做
root 自经验，取法是：

```python
prompts = await memory.recall_recent(scope, [USER_PROMPT], ...)  # 只返回未 superseded
original = prompts[-1]  # 最旧一条
```

但真正的原始 prompt 早被 compaction superseded，`recall_recent` 拿不到。剩下未 superseded 的
最旧 USER_PROMPT 是中途的打断语「你为什么不使用技能呢」，于是它被当成「原始诉求」写成
agent 层 `AGENT_CONVERSATION_TURN`（seq7，`origin_task_id` 元数据是该 ingest 的指纹）。

**根因 B — compaction summary 只在 task 层，close 时被无声丢弃**
`task_compact_summary` 仅写 task 层。`_close_one` step 3 `_supersede_own_conversation` 把整个
task 层对话（含 `TASK_COMPACT_SUMMARY`）全量 supersede，但该摘要**从未被镜像进 agent 层**，
于是「会话目标 / 已完成工作」彻底丢失。

### 1.3 连带问题 — agent 层折叠落单

`fold_root_experience`（agent 层 compaction）只 supersede
`[TASK_DISPATCH, TASK_DISPATCH_RESULT, AGENT_COMPACT_SUMMARY]`，**不碰
`AGENT_CONVERSATION_TURN`**（`_AGENT_COMPACT_TYPES` 把它列为可折，但该常量定义后从未被引用，
是悬空的）。因此被折胶囊的 user 回合会落单飘在 compact summary 上方。这是现状即有的 gap；本设计
新增的 assistant summary 会令其翻倍，故一并修。

## 2. 设计

### 2.1 root 自经验胶囊新形态（`_synthesize_dispatch_pair`）

```
[user]      task.user_prompt          ← 稳定原始诉求（修根因 A）
[assistant] <task_compact_summary>    ← 仅当被 compact 过才写（修根因 B）
[assistant] delegate_task(tool_call)  ← 不变
[tool]      mem_content               ← 不变
```

四件套都写成 agent 层记录，**共享同一个时间戳 `now_utc()`**，按 ingest 顺序
（user → assistant-sum → dispatch → result）递增 seq_no。composer 先按 timestamp、再按 seq_no
（`composer.py:577`），故胶囊内部靠 seq_no 保序、整体锚在 close 时刻。

改动点：

1. **user 回合换源（修 A）**：`prompts[-1]` → `task.user_prompt`。仍写
   `AGENT_CONVERSATION_TURN` role=user，metadata `origin_task_id = task.id`（已有）。
   `task.user_prompt` 为空则跳过该回合（防御）。对 cross-agent root 天然正确（取那个 agent 的
   子任务 prompt）。

2. **新增 assistant summary 回合（修 B）**：写 dispatch 对之前，在 task scope 召回未 superseded 的
   `TASK_COMPACT_SUMMARY`，有则取最新一条（按 timestamp/seq_no），内容写成
   `AGENT_CONVERSATION_TURN` role=assistant，metadata `origin_task_id = task.id`；没有
   （没被 compact）则不写。
   时序保证：`_synthesize_dispatch_pair` 在 `_close_one` step 2 执行，早于 step 3 的
   `_supersede_own_conversation`，此刻 summary 还活着。
   渲染：`record_to_history_block` 对 role=assistant 且无 `tool_calls` 给空列表，渲染成干净的
   纯文本 assistant 消息，无需改 `agent_experience`。

3. **统一时间戳**：四件套均 `now_utc()`（不再用 `original.timestamp` 把 user 回合甩到过去，
   也不用 `−2µs/−1µs` 错位——后者会和 2.2 的 `anchor−1µs` 撞 µs）。

### 2.2 agent 层折叠对齐（`fold_root_experience`）

折叠后渲染顺序：

```
[AGENT_COMPACT_SUMMARY]   role=user，折掉的旧 root 胶囊，ts = 最旧保留胶囊 − 1µs
[保留胶囊 1] user → assistant(summary) → assistant(delegate) → tool(result)
[保留胶囊 2] ...
[当前 OPEN task 的 task 层对话]
```

改动点：

1. **recall 列表加上 `AGENT_CONVERSATION_TURN`**。
2. **连带 supersede 被折胶囊的 conversation turn**：被折 `root_results`（`parent_task_id is None`
   且在 `[:-keep_last]` 窗口）的 `child_task_id` 集合 = 被折胶囊 task id；supersede
   `origin_task_id ∈ 该集合` 的 `AGENT_CONVERSATION_TURN`，并入 `ids`。
3. 现有 `anchor_ts = min(kept results 的 timestamp) − 1µs` 不变。因 2.1 让胶囊四件套同享
   `now_utc()`，`anchor−1µs` 严格小于最旧保留胶囊的全部元素 → compact summary 干净排在整个胶囊
   之前，不再楔入。

### 2.3 角色与轮次约束（Anthropic 消息格式）

权威规则（claude-api skill）：**首条消息必须是 `user`**（首条 assistant → 400）；**连续同 role
消息允许、被 API 合并成一轮**（故 `[assistant summary][assistant delegate]` 不会 400）。
composer/anthropic 序列化层均不做 role 归一化，role 原样直达 API。据此：

1. **折叠产出 `AGENT_COMPACT_SUMMARY` 必须 role=user**：它是折叠后最靠前的 history 消息
   （`anchor−1µs`），首条必须 user，故 role=user 是承重的、非随意。现状即如此（agent_experience.py
   渲染成 role=user）。**消歧义：渲染期套显式包装**——见 §2.4。
2. **胶囊内 assistant summary 保持 role=assistant**：它非首条（前置 user 回合垫着），语义为
   agent 自述，无歧义。
3. **硬约束——胶囊恒以 user 回合打头**：`task.user_prompt` 为空时回退 `session.user_prompt`；
   若仍为空，则把该胶囊的 summary 降级 role=user（或省略 summary），保证全局首条恒为 user。
   该不变量同时由 §2.5 的 gateway 归一化兜底。

### 2.4 compaction summary 渲染期包装（消歧义）

凡 `*_COMPACT_SUMMARY` 以 **role=user** 呈现的地方，**在渲染成 ContextBlock 时**给内容套显式
包装前缀，使其被读成「先前经验的回顾」而非用户新指令：

```
[以下是先前对话/经验的压缩摘要，供你延续工作参考；并非用户的新指令]
<原 summary 文本>
```

- 套在**渲染期**（`agent_experience.py` 的 `AGENT_COMPACT_SUMMARY` 块；以及 task 层
  `TASK_COMPACT_SUMMARY` 渲染处，保持一致），**不写进存储内容**——否则包装文本会被后续
  compaction 再折叠进新 summary、层层累积。
- 胶囊内的 assistant summary（§2.1）**不套**——它语义已清晰（assistant 自述），且非 user 回合。

> 统一原则：**呈现态（包装标签、`## Current Message` 框架、Reply 提示等）一律渲染期生成、不落库**。
> §2.4 与 §2.6 是同一原则的两个落点。

### 2.5 gateway 发送前归一化（防御纵深）

`llm_gateway.stream_llm` 是唯一发送关口，现有两条合法化（`drop_orphan_tool_results` +
`merge_consecutive_messages`）。**新增第三条 `ensure_leading_user`**：丢弃开头 role≠"user" 的
消息直到首条为 user（Anthropic 首条必须 user，否则 400）。

- 顺序：`merge_consecutive_messages(drop_orphan_tool_results(ensure_leading_user(messages)))`
  ——先丢前导非 user（可能露出新孤儿），再 drop_orphan 清孤儿，最后 merge。
- 与 docstring 中**刻意不下沉的「以 user 收尾」**不冲突：后者每轮强制会在 tool result 后误插
  user、污染工具循环；而「首条 user」只动头部、不注入文案，对工具流安全。
- 定位：这是**纯 400 防御兜底**。正确性仍由 §2.3.3 的 source 守卫保证（胶囊恒以 user 打头，
  不丢数据）；gateway 仅在收到畸形输入时防止 400。同步更新模块 docstring 的不变式清单。

### 2.6 当前消息渲染期框架（current-message framing 不落库）

现状：USER_PROMPT 三处 ingest 不一致——`driver.py`（task 启动）把
`## Current Task` + `## Current Message` + `（Reply in the same language…）` **烤进存储**；
`suspend.py` / `runtime.py`（HITL 续传）存 raw。结果只有最老一条挂着「## Current Message」，
后续轮次裸——呈现态错位。

按 §2.4 的统一原则改：

1. **所有 ingest 存纯裸用户文本**：`driver.py:218-239` 改为 `content = task.user_prompt`，
   不再拼 `## Current Task`/`## Current Message`/Reply（与 suspend/runtime 对齐）。
2. **渲染期只装饰「真正的当前消息」**：框架
   `## Current Task\n{title}\n{desc}` + `## Current Message\n{raw}` + `\n\n（Reply…）`
   只贴到**最近一条 task_conversation 的 USER_PROMPT** block，历史 user 回合保持裸。
   - daemon 路径（`not user_prompt_in_memory`，composer Path 1）本就渲染期实时构建，不动。
   - in-memory 路径（Path 2）改为**就地装饰最近一条当前 task 的 user block**（复用已有的
     `current_task_user_idx` / source 机制），不产生重复、不需 live-append。
3. 顺带修掉「只有首条带框架」的现状不一致。

注：`## Current Progress`（retry 反馈）已是独立的带时间戳 history block（composer
`_progress_history_block`），不在本节范围，保持现状。

### 2.7 不动的部分

`_close_one` 结构、step 3 的全量 supersede（task 层照清，summary 坍缩进胶囊后原件删掉是对的）、
`_gc_subtree`、bubble、`apply_compact`（task 层）。悬空常量 `_AGENT_COMPACT_TYPES` 可顺手删除
或接线；本设计选择删除（其语义已由 `fold_root_experience` 的显式 recall 列表承载）。

## 3. 测试计划

新增/恢复 agent 层折叠相关单测（对应被删的 `test_root_self_experience.py`、
`test_root_subtree_fold.py`、`test_dispatch_fold_golden.py` 区域）：

1. **复现根因 A+B**：root task 先 compact（产出 `task_compact_summary` + supersede 原始 prompt），
   再 close；断言 agent 层胶囊 = `[user=task.user_prompt][assistant=summary][delegate][result]`，
   且 `[0]` 不是中途打断语。
2. **没被 compact 的 root task close**：断言胶囊 = `[user=task.user_prompt][delegate][result]`，
   无 assistant summary 回合。
3. **agent 层折叠落单**：构造 > keep_last 个已 close root 胶囊触发 `fold_root_experience`；
   断言被折胶囊的 user/assistant-summary `AGENT_CONVERSATION_TURN` 被 supersede（不落单），
   保留胶囊完整，compact summary 排在保留胶囊之前。
4. **排序 golden**：装配一遍，断言渲染顺序 = §2.2 所述。
5. **渲染期包装**：断言 `AGENT_COMPACT_SUMMARY`（及 task 层 summary）渲染出的 user 块带包装
   前缀，而存储的 memory event content 不含包装文本（不污染后续折叠）。
6. **gateway `ensure_leading_user`**：单测前导 assistant/tool 被丢、首条恒为 user；前导 assistant
   带配对 tool 被丢后其 tool 不残留为孤儿；正常工具循环（中段 tool result 后无 user）不被误改。
7. **current-message 框架**：断言 USER_PROMPT 存储内容为纯裸文本（无 `## Current`/Reply）；渲染后
   仅最近一条 user 带 `## Current Task`/`## Current Message`/Reply、历史 user 裸；多轮（含 HITL 续传）
   与 daemon 两路径均符合。

## 4. 验收

- 复现会话的后续 prompt `[0]` 恢复为总结性 message（或 task.user_prompt + summary），不再是打断语。
- `uv run pytest` 全绿。
