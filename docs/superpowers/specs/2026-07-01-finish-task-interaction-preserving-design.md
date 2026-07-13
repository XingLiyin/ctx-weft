# finish_task 反转契约:答复即消息正文(交互保留型收尾)

- 日期: 2026-07-01
- 分支: feat/interaction-preserving-capsule
- 状态: 设计已确认,待实现

## 1. 问题

`finish_task` 工具当前把"给用户的最终答复"塞在参数 `result` 里。由此产生两个互相纠缠的乱象:

1. **Agent 不稳定**: 有时把最终答复写进 `result`,有时又写成普通消息正文、`result` 只剩一句干总结。
2. **前端不确定**: `finish_task` 的输入有时被渲染成一个气泡/卡片,有时不渲染。

### 根因

**"给用户的最终答复"没有唯一落点,且提示词自相矛盾。**

- 存在两个通道:①助手消息正文(prose);②`finish_task(result=...)`。
- 提示词对 `result` 的描述互相打架:
  - 工具描述(`control_capability.py:293`): "Your final reply to the user … put the WHOLE reply here only" —— 框定为**对话式答复**。
  - `SOUL.md:42`: "`result` 必须描述实际完成或产出的内容……最终成品" —— 框定为**产出描述**。
- 我们同时要求模型做两件违背其天性的事:把整段答复塞进工具参数,且**不要**写成消息正文。LLM 被训练成"把答案写进消息正文、再顺手调工具",于是两头摇摆 —— 这就是不稳定的机械来源。

### 前端确认(关键事实)

- 助手消息正文本就会渲染并持久化成普通气泡: `useSessionSSE.ts:350-357`(`text_done` → `ChatMessage`)。
- `finish_task` 的 `result` 被**另外**渲染成一张 `task_summary` 卡: `useSessionSSE.ts:297-303`,判定逻辑在 `taskSummary.ts`(仅根任务、`result` 非空才展示)。
- 于是:模型只写 `result`→只见卡(答复被当卡显示);模型写 prose+`result`→双气泡。这正是"很乱"。

### 传播事实(约束设计)

- `finish_task(result=X)` 设 `ctx.task.outputs = X`(`control_capability.py:304`)。
- 子任务的**正文永不传给父任务**;父任务只经 blackboard 读到 `task.outputs`(= 交付物)。`task.outputs` 是子→父的唯一通道,且 observer 的 "Final output" 也读它(`composer.py:754`)。
- `finish_task` 契约对 root / 子任务**完全一致**,无深度区分;只有前端对 root 特判。
- 过程报告(observer 产的 `task_summary`)本就独立存在、进 tool 槽、**用户永不可见**。

## 2. 方案:反转契约(2b)

**唯一规则(与深度无关):**

> 把最终答复正常写成**消息正文**,然后调用 `finish_task()` 收尾。正文即最终交付物。

- 只写正文、不调 `finish_task` → 仍是对话/进行中(维持现有语义)。
- 正文 + `finish_task()` 同回合 → 该正文即最终交付,任务结束。
- `finish_task` 保留**一个可选参数** `deliverables_summary`(产出小结):只放交付物清单(改了哪些关键文件等),**明确不是答案**,给复核者/父任务看,可空。

**`task.outputs` = 收尾回合正文 + `deliverables_summary` 拼接**,拼接本身即兜底。

这为何根治不稳定: **"答案"永远只有一个家(正文)**,模型不再纠结放 prose 还是放参数 —— 参数已明确不是放答案的地方。小结填不填无所谓,不影响答案送达。

### 数据流(改后)

```
收尾回合: assistant 正文(答复) + finish_task(deliverables_summary?)
  │
  ├─ 前端: 正文 → text_done → 普通助手气泡(确定性,永远在)
  │        finish_task 标记 → 不渲染(删掉 task_summary 卡)
  │
  └─ 后端 act.py 收尾: task.outputs = 正文 + 小结(拼接,见兜底链)
             │
             ├─ observer "Final output" 读 task.outputs   (不变)
             ├─ blackboard publish = outputs + 过程报告    (不变)
             └─ parent 召回 outputs                        (不变)
```

下游三处(observer / blackboard / parent)逻辑**一行不改** —— 它们都读 `task.outputs`,只是 outputs 来源从"工具参数"换成"正文+小结拼接"。

## 3. 捕获与兜底(锁死落点)

`act.py` 收尾后合成 `task.outputs`(它同时握有正文与 finish_task 参数):

```
body    = 收尾回合(transcript[-1])的 assistant_text
          ↓ 若空
          回溯本段 transcript 里最近一段非空 assistant_text
summary = 收尾回合 tool_calls 里 finish_task 的 deliverables_summary(可空)

task.outputs =
    body                     若只有 body
    summary                  若只有 summary
    body + "\n\n" + summary  若两者都有(拼接分隔见实现)
    None                     两者都空 → 交给 observer 护栏(见 §4)
```

只在**收尾路径**合成: `exit_reason in ("normal","actor_done") 且 task.status != "SUSPENDED"`。
这既覆盖 finish_task 收尾(actor_done 且未挂起)、又保留纯文本收尾(normal),且**排除** max_turns / context_limit / delegate-suspend(它们不产最终输出,维持现状)。

## 4. observer 护栏

`report_task_outcome` 已有护栏(`control_capability.py:438`): `task_status=="success" 且 not task.outputs` → 改判 retry。保留;仅把提示文案改写成新契约("把最终答复写成消息正文,再调 `finish_task()` 收尾")。这是"正文+小结全空"这一残余边界的兜底 —— 空收尾不会被判成功。

## 5. 改动清单

### Core(ctx-weft)

1. **`control_capability.py` `finish_task`**
   - 参数 `result` → `deliverables_summary`,可选(默认 `""`)。
   - 重写 Annotated 描述 + docstring: 定位为"给复核者/父任务的交付物小结,不是给用户的答复;答复写在你本回合的消息正文里;无可列举则留空"。
   - 函数体: **删除** `ctx.task.outputs = result`;保留 `ctx.task.actor_done = True`。outputs 由 act.py 合成。
   - 返回文案可保持 "Task result submitted." 或改为中性的 "Task finished."。

2. **`act.py`**
   - 收尾合成块(现 131-136): 从 `exit_reason=="normal"` 扩为 `exit_reason in ("normal","actor_done") 且 status!="SUSPENDED"`,按 §3 兜底链合成 `body + summary`。
   - 新增小工具:从 `transcript[-1].tool_calls` 取 `FINISH_TASK_NAME` 的 `deliverables_summary`;body 空时回溯最近非空 `assistant_text`。
   - 提示词 `_build_act_guidance` 的 `finish_core`(646-651): 重写为新契约(见 §6)。

3. **`finalize.py` `_synthesize_dispatch_pair`(232-267)**
   - finish 对只去掉 tool_call 的 `input:{result:...}` → 改为**无参标记** `finish_task()`,避免喂回模型的历史出现 `finish_task(result=<整段答复>)` 诱导退化。
   - **assistant `content` 仍 = `act_recap`(过程复述),不改为答复正文**。关键:短子任务的 body(含真实答复)会被胶囊**内联**进 parent 视图;若 finish 对 assistant 也放答复,就与内联 body 的答复**重复成两条一样的助手消息**(实测 bug)。答复的落点由内联 body / blackboard `mem_content` 承载,finish 对只承载 act_recap + 无参 finish 标记 + tool 槽过程报告。
   - `_replace_finish_report`(bg-observe 替换)同理:assistant 槽仍写 `act_recap`,只刷新 tool 槽过程报告。
   - `_finish_tool_text` / `_output_text` / `_build_memory_content` 不变(答复经 `_build_memory_content` 进 blackboard,dict-list outputs 提取回归在此层覆盖)。

4. **`SOUL.md`(`resources/` 与 `packaging/default_data/` 两份)**
   - 重写 40-43 与 45-47: 删除"`result` 必须描述实际完成或产出的内容"这类矛盾表述,统一为新契约;讲清 prose-only(对话/进行中) vs prose+finish_task(完成)。

### Host(ipmastercowork)

5. **`api/models/session.py`(331-334)**: 删除 `finish_task` 的 `is_root` 特判(前端不再消费)。`control_tool_call` 事件仍发,但前端忽略。

### Frontend(frontend-desktop)

6. **`hooks/useSessionSSE.ts`**: 删除 `taskSummaryFromEvent` 的使用(297-303、409-413),`finish_task` 不再产 `task_summary` item;从 `ChatItem` 联合类型移除 `ChatTaskSummary`。
7. **`lib/taskSummary.ts` 与 `lib/taskSummary.test.ts`**: 删除。
8. **`components/ChatPanel.tsx`**: 删除 `TaskSummaryRow` 组件、`item.kind === 'task_summary'` 分支(591)、相关类型引用。

> 删掉总结卡后,收尾答复仍由 `text_done → ChatMessage` 渲染成普通气泡,确定性存在。

## 6. 提示词新措辞(草案)

**act.py `finish_core`:**

> When your work is done, write your final reply to the user as your normal message text, then call the `control__finish_task` tool to end the task. Your message text is the reply the user sees and the deliverable handed to whoever delegated this task — write it as your message, not inside the tool. `finish_task` takes an optional `deliverables_summary` for a brief recap of concrete artifacts (e.g. key files changed) for the reviewer — that is NOT your answer; leave it empty if there is nothing to itemize.

interactive 追加: "(Replying in plain text WITHOUT calling finish_task pauses the task and waits for the user, instead of finishing.)"

**SOUL.md(要点):**

- 完成任务时,先把最终答复正常写成消息正文,再调用 `control__finish_task()` 收尾;正文就是给用户、也是交给上级的最终交付。
- `finish_task` 的可选参数 `deliverables_summary` 只放交付物清单/小结(给复核者),不是答案;无则留空。
- 只输出纯文本而不调 `finish_task` = 仍在对话/进行中,不算完成。

## 7. 测试

- **单元(core)**:
  - act.py 合成: finish_task 收尾 → outputs = 正文;有 summary → 正文+小结拼接;正文空+有 summary → summary;两者空 → outputs None(触发护栏)。
  - 纯文本(normal)收尾: outputs = 正文(现状不回归)。
  - max_turns / context_limit / delegate-suspend: 不改 outputs(现状不回归)。
  - finish_task 工具: 不再写 outputs、置 actor_done。
  - observer 护栏: outputs 空 → success 改判 retry(新文案)。
  - finalize 金样(`test_capsule_golden.py` / `test_finalize.py` 等): finish 对 = assistant(答复正文)+finish_task()+tool(过程报告);bg-observe 替换仍work。
- **前端**: 删 `taskSummary.test.ts`;确认收尾回合正文成气泡、无重复卡(可在既有 SSE/Chat 测试补一例)。

## 8. 风险与取舍

- **残余边界**: 模型完全不写正文、只填 `deliverables_summary` → 用户侧无气泡(outputs 仍非空,不触发护栏)。经新提示词后极罕见;本设计按用户决定不为此加前端兜底卡。若日后需要,可在"正文空但有 summary"时把 outputs 作为一条 fallback 助手消息渲染。
- **迁移**: 历史里旧的 `finish_task(result=...)` 记录仍存;前端删卡后不再渲染它们(退化为无),可接受。
- **分支现状**: 本分支已有未提交的胶囊改动,注意与 finalize/composer 现有改动协同;金样测试需相应更新。
