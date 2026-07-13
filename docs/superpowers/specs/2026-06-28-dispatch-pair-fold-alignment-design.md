# Dispatch 对降级为 delegating task 对话中的一组普通 message（并入 AGENT_CONVERSATION_TURN）

- 日期：2026-06-28
- 范围：`core/loop/capability_gateway.py`（delegate 调用写成 agent 层 delegate conversation turn）、
  `core/loop/steps/finalize.py`（cross_agent result 写成配对 tool 回合；same_agent supersede 孤立 delegate 回合）、
  `core/loop/steps/compact.py`（`fold_root_experience` / `_count_root_residues`：dispatch 对随单元 L2 折、
  active delegating task 不折、prefer-non-None parent）、
  `core/assembler/sources/agent_recall.py`（删 dispatch 专用配对分支、统一 conversation-turn 渲染）、
  `core/loop/steps/legacy_dispatch.py`（**新增**：§5.5 集中适配层）。均在 `ctx_weft/` 下
- 相关 spec：`docs/spec/06-memory-layers-and-compaction.md`、
  `2026-06-26-root-experience-summary-fold-design.md`、`2026-06-28-task-resident-capsule-design.md`
- 状态：**全部实现完成、TDD 全程、全套 unit(828) + integration + protocols 绿（2026-06-28）**。
  - 第一波（已废止方向）：曾把 dispatch 对在 L1 随 body 删（§2.2 旧稿），**实为误读**——dispatch 对是
    agent 对话的一部分、应与 finish 对同命运（L1 黑盒保留、L2 一起折），非随 body 删。第二波已回退。
  - 最终实现 = §2.1 + §2.2（修正版：L2 与单元一起折）+ §2.3（表示层并入）+ §5.5（集中适配层），一体落地。
  - 剩余（软增强，未做）：§3.3 给 observe / background_observe 的 prompt 加「综合已完成子任务结果」引导
    （落点在 host prompt 模板）。
  - 环境注记：本仓测试用 `uv sync --extra dev` 装 pytest；`psutil`/`pyyaml` 在某些 provider 测试缺失
    （pre-existing，跑时 `--with psutil --with pyyaml` 补）；`test_background_observe.py` 有 pre-existing
    asyncio event-loop 测试隔离 ERROR（与本改动无关，已 git stash 验证）。

## 1. 问题

### 1.1 dispatch 对是 agent 对话里的一组普通 message，却被特殊对待

`TASK_DISPATCH` / `TASK_DISPATCH_RESULT` 在概念上是 **delegating task 对话里的一次工具调用回合**
（actor 调 `delegate_task/delegate_plan`，gateway 透传 `tc.id`，结果异步回填）——它与该 task 自己的
finish 对一样，是这个 task 的 **agent 层对话**的一部分，只是 result 跨 task 异步回填。它**不是**
「另一个 task 的经验单元」，也不是该 task 的 task 层 body（推理 + 本地工具）。

正确的命运对齐对象是 **finish 对**（同属该 task 的 agent 层对话），而非 body：

| | delegating task 的 body（task 层） | 它的 finish 对（agent 层） | 它派出去的 dispatch 对（agent 层） |
|---|---|---|---|
| L0 | 留 | 留 | 留 |
| L1（黑盒） | **删** | 留（黑盒载体） | **应留**（同属黑盒 agent 对话） |
| L2（折成摘要） | （已删） | 折进 `AGENT_COMPACT_SUMMARY` | **应一起折**（同 origin、同命运） |

### 1.2 现象（旧实现的不对称）

旧 `fold_root_experience` 把 dispatch 对当成「准单元」，靠 `parent_task_id` 附挂在 delegating task 下，
走一条**独立的 L2 配对 supersede 路径**（`compact.py` 旧 §2.2 段）。后果：当 delegating task 单元折进
`AGENT_COMPACT_SUMMARY` 时，dispatch 对靠一条与 finish 对不同的专用扫描路径才被一并 supersede——
「同一段 agent 对话，finish 对走主路径折、dispatch 对走特殊路径折」，逻辑分叉、易错。

用户视角的核心怪异：**「dispatch 对只是一次工具调用结果，当 agent conversation 折了，但里面的工具
调用结果没折，不是很奇怪吗」**——根因是 dispatch 对没有被当成 agent 对话的普通 message，而是另起了
一套表示（扁平 `tool_name`/`arguments` + 独立 enum）与一条 fold 特殊路径。

### 1.3 根因

旧实现里 dispatch 对的身份（独立 enum `TASK_DISPATCH`/`RESULT`）与 finish 对（`AGENT_CONVERSATION_TURN`）
分裂，故 fold 的「顶层折叠单元」（由 `origin_task_id` 分组的 conversation turn 定义）认不出 dispatch
对属于哪个单元，只能给它一条 `parent_task_id` 附挂的特殊路径。**把 dispatch 对正确表示为 delegating
task 对话的 conversation turn（同 `origin_task_id`），它自然落入该单元、与 finish 对同命运，特殊路径
即可删除。**

## 2. 设计

dispatch 对 = delegating task 对话里的一组普通 `AGENT_CONVERSATION_TURN`（assistant delegate 调用回合
+ tool result 回合），`origin_task_id = delegating task`，与同单元 finish 对**同 origin、同命运**。
按写入方分两类落地。

### 2.1 同 agent 子任务：不 bubble、supersede 孤立 delegate 回合

same_agent 子任务的真实产出由**嵌套 finish 对**（`_synthesize_dispatch_pair`，origin=child）全权承载，
parent scope 不需要 dispatch result——但 gateway 在 delegate 时已写了一条 delegate 回合（此刻无法判定
same/cross），它会变成孤立（无 result 配对）的悬空 tool_call。

**复核结论（2026-06-28）**：gateway 写 delegate 回合时**无法判定 same_agent**——child task 尚未创建、
`assigned_agent_id` 要到 `runtime._resolve()` 才设（`runtime.py:760-771`）。故采用 **finalize 补偿**。

改动：
1. **gateway 照写 delegate 回合**（此刻无法判定 same/cross）。
2. **finalize same_agent 分支**：(a) 不 bubble result；(b) **supersede 掉 gateway 早先写的那条孤立
   delegate 回合**（按 `task.origin_tool_call_id` 在 `parent_scope` 匹配 `tool_calls[].id` 定位）。
3. 嵌套 finish 对不变，全权承载。

### 2.2 跨 agent 子任务：result 写成配对 tool 回合，随单元 L2 一起折

跨 agent dispatch 对是 **parent scope 里唯一能见的 child 黑盒结果**（真实 output+report），信息不能丢。
它的命运与 delegating task 的 finish 对一致（同属该 task 的 agent 层对话）：

1. **归属明确**：`origin_task_id = delegating task`（gateway 的 delegate 回合 + finalize 的 result 回合
   都用它），与该 task 的 finish 对同 origin → 落入同一折叠单元。
2. **L1（黑盒）保留**：delegating task 进 L1 时只删 **task 层 body**；dispatch 对是 agent 层对话、与
   finish 对一并作为黑盒保留（**不在 L1 删**——这正是第一波误删、第二波回退的点）。
3. **L2 一起折**：单元降 L2 时，其全部 `AGENT_CONVERSATION_TURN`（finish 对 + dispatch 对，同 `origin_task_id`）
   走**同一条** `origin_task_id ∈ l2_set` 主路径一并 supersede、折进 `AGENT_COMPACT_SUMMARY`。其 result
   已被该单元 finish 对的 Process Report 吸收（见 §3 前提），折掉不丢信息。
4. **删除特殊逻辑**：旧 `compact.py` 那段「按 `parent_task_id` 配对 supersede dispatch 对 + 收集
   `tcids` 再扫 `TASK_DISPATCH`」整段移除；dispatch 对随单元在主路径折。
5. **active delegating task 不折**：dispatch 对一旦成为 agent 对话 turn，**活跃（未结束）的 delegating
   task** 也会有 agent 层 turn（在途 dispatch，无 finish 对）。这类是**在途 working set**，绝不可当可
   折顶层单元（否则折掉当前/挂起任务的在途上下文）。判据：`active = has_dispatch − has_finish`
   （有 delegate/delegate_plan 调用回合、但无 finish_task 调用回合的 origin）→ 从 `top` 排除。
   `fold_root_experience` 与 `_count_root_residues` 共用 `_dispatch_finish_sets`。
6. **parent prefer-non-None**：result 回合不定义单元 parent（其 `parent_task_id` 留空）；单元 parent 由
   权威回合（gateway delegate 回合 / finish 对）给出。`parent_of` 计算改为「非 None parent 优先」，
   使 result 回合的空 parent 不覆盖真实 parent。

### 2.3 表示层并入 `AGENT_CONVERSATION_TURN`（核心，非可选）

dispatch 对与 finish 对在渲染出的形状本就同构（`assistant(tool_calls=[{id,name,input}])` +
`tool(content, tool_call_id)`），`AGENT_CONVERSATION_TURN` 已是「任意 role 的真实回合、原样透传」的
通用容器（finish 对 / child seed 已在用）。把 dispatch 对也直接存为两条 `AGENT_CONVERSATION_TURN`：
- assistant 回合（gateway 写）：`tool_calls=[{id, name=delegate_task, input=arguments}]`，`origin_task_id=delegating task`
- tool 回合（finalize 写）：`content = output+report`（fail 时带 `[outcome=fail]` 前缀），`tool_call_id`，`origin_task_id=delegating task`

收益：
- `agent_recall` 的 dispatch 专用配对分支（`dispatches`/`results` 收集 + 未配对隐去）整段删除，
  dispatch 对走与 finish 对完全相同的 conversation-turn 渲染（`record_to_history_block`）——召回装配只剩
  一种回合类型。悬空 tool_call（在途 dispatch 尚无 result）由 `llm_gateway` 的
  `drop_dangling_tool_calls` 兜底，等价旧的「未配对隐去」。
- **消除扁平 `tool_name`/`arguments` 中间表示 + 渲染期重组装**：旧 dispatch 对把 tool name 存成扁平
  `metadata["tool_name"]` + 平行 `arguments`（沿用 `_record_invocation` 的工具审计格式），渲染时才临时
  拼回 `tool_calls`。并入后存储即为 LLMMessage 形状，与 finish 对一致。
- **fold 主路径统一**：dispatch 对与 finish 对同 `origin_task_id` → fold 不需要区分二者（同命运），
  靠 metadata 区分仅用于**渲染**（assistant 携 tool_calls / tool 携 tool_call_id），不用于 fold 命运判别。

**死字段清除**（grep 确认只写不读）：`tool_name`/`arguments`（进 `tool_calls` 结构）、`invocation_id`
（审计走 `CapabilityInvoked`/`CapabilityFinished` 事件流）、`child_task_id`/`title`/`outcome`
（`TASK_DISPATCH_RESULT`）全部清除。`outcome` 的 fail 结构层标记不丢——复用 `report_prefix` 把
`[outcome=fail]` 编进 result 回合 content 前缀（对齐 finish 对 `finalize.py`），失败状态结构层 + 语义层
双重可见。

异步时序：dispatch 对的 assistant 回合由 gateway 写、tool 回合由 child finalize 回填（**两点写入**），
不同于 finish 对一次写两条。但这是异步派发的自然时序（result 必须等 child 完成），两次独立 ingest 即可
（result 由 finalize 一次写定，**不需要 background_observe 的占位→替换机制**）。

## 3. 前提（已复核）

1. **delegating task 的 finish report 是否吸收 child 产出**——§2.2「dispatch 对随单元 L2 折」时其 result
   被 finish report 吸收、折掉不丢信息的前提。**已复核（2026-06-28）：主路径成立，有一处边界 gap。**

   report 三条生成路径（时序前提成立：parent delegate 后 SUSPEND，必等 child 回填才 resume，report 在
   「已见 child 结果」后生成）：
   - **非 root + 有 observe ROLE** → `_llm_observe`：装配含 dispatch 结果 + actor_transcript，LLM 有材料吸收。✓
   - **root** → `_rule_observe` 机械文本，但真实 report 由 `background_observe` 异步产，同样见 dispatch 结果。✓
   - **非 root + 无 observe ROLE** → `_rule_observe` 机械文本、**不触发 background_observe** → report 结构上
     不含 child 产出。✗（边界 gap）

   两点 caveat：
   - **(a) 吸收依赖 LLM 行为**，非结构保证：需 observe / background_observe 的 prompt 显式引导
     「综合已完成子任务的结果」（§3.3 软增强，未做）。
   - **(b) 边界 gap**：无 observe ROLE 的非 root delegating task，report 机械、不吸收 child 产出；其
     dispatch 对在 L2 折掉会比保留更少一层可见。但这类 agent 本就 observe 降级。

2. **产品意图：跨 agent 子任务结果是否要比本地工具调用多保留一级可见性** —— **【已拍板：否】**。
   dispatch 对与 finish 对同命运（agent 对话黑盒），L1 留、L2 折，不给 dispatch 单独分级。

3. **边界 gap（1.b）的处理** —— **【已拍板：A 接受降级】**：统一走主路径，不为「无 observe ROLE」边缘
   case 保留特殊逻辑；配合 §3.3 prompt 引导（软增强）。

## 4. 测试计划（已实现）

1. **同 agent 子任务 close**：共享 agent scope 无 dispatch result、孤立 delegate 回合被 supersede，仅剩
   嵌套 finish 对（`test_subtask_nesting` / `test_close_task` / `test_capsule_golden::H8`）。
2. **跨 agent dispatch result = 配对 tool 回合**：origin=delegating task、配对 `tool_call_id`、content=
   mem_content、不写 legacy enum（`test_subtask_nesting` / `test_close_task` / `test_capsule_golden::H4` /
   `test_open_closed_recall`）。
3. **gateway 写 delegate conversation turn**：assistant + tool_calls、origin=delegating、不写 `TASK_DISPATCH`/
   即时 `TOOL_RESULT`（`test_delegation`）。
4. **fold L1 保留 / L2 一起折**：dispatch 对 L1 随单元黑盒保留、L2 随单元一起 supersede
   （`test_root_subtree_fold::test_cross_agent_dispatch_pair_survives_l1_folds_at_l2` /
   `::test_cross_agent_dispatch_pair_folded_with_parent`）。
5. **active delegating task 不折**：在途 working set（dispatch 回合无 finish 对）存活
   （`test_compaction::test_fold_root_residues_keep_last_and_subtask_survive`）。
6. **§5.5 适配层**：`normalize_legacy_dispatch` 单测（配对转换 / 未配对隐去 / fail 前缀 / 非 legacy 透传，
   `test_legacy_dispatch`）+ 存量 legacy 经适配 fold 端到端（`test_root_subtree_fold::test_legacy_dispatch_pair_folds_with_unit_via_adapter`）。
7. `uv run pytest` 全绿（828 unit + integration + protocols）。

## 5. 前向兼容

兼容性目标：**存量持久 memory（postgres / SQLite backend，含续跑会话）在新代码下免迁移正确读取与 fold**。

### 5.0 硬约束（底线）

`MemoryEventType` 是 StrEnum、按字符串存库。`TASK_DISPATCH` / `TASK_DISPATCH_RESULT` 两 enum 成员
**永远不能物理删除**——否则存量行反序列化失败。只能保留（标 deprecated）。

### 5.1 §2.1 删同 agent 空壳 —— 纯写侧

新代码不再写空壳；存量空壳由 §5.5 适配层归一化渲染（孤立 dispatch 隐去），不悬空、不崩。

### 5.2 §2.2 / §2.3 归属键 —— 由适配层补全 `origin_task_id`

新数据 dispatch 对直接带 `origin_task_id`（=delegating task）。存量 legacy 无此字段：适配层
（§5.5）在读侧补全——`TASK_DISPATCH` 取写入 scope 的 `task_id`、`TASK_DISPATCH_RESULT` 取
`parent_task_id`（均 = delegating task）→ 下游 fold / 渲染统一用 `origin_task_id`。

### 5.3 §2.3 并入 —— 读侧由适配层统一

新写 conversation turn、存量是旧 enum；读侧由 §5.5 适配层把旧 enum 归一化成 conversation turn，
`agent_recall` / `fold_root_experience` 只认单一表示。配合 §5.0「enum 不删」，存量与新数据共存无缝。

### 5.4 落地次序

§2.1 + §2.2 + §2.3 + §5.5 一体落地（适配层是 §2.3 破坏性改动的前向兼容前提，必须同批）。

### 5.5 兼容代码集中 + 日落路径 —— 已实现

所有旧 dispatch enum 的兼容逻辑收敛到单一适配层 `core/loop/steps/legacy_dispatch.py`：

- **形态**：`normalize_legacy_dispatch(records)` 在召回返回后、装配与 fold 之前，把存量
  `TASK_DISPATCH` → delegate assistant 回合（`tool_calls` 承载、补 `origin_task_id`）、
  `TASK_DISPATCH_RESULT` → tool 回合（补 `origin_task_id`、清 `parent_task_id`、fail 补前缀），
  孤立（无配对 RESULT）的 `TASK_DISPATCH` 隐去。`agent_recall` 与 `compact.fold_root_experience` 各调一次。
- **日落**：确认持久 backend 无存量旧 enum 行后，删 `legacy_dispatch.py` + 两处调用 + 移除两个 enum 成员
  一次完成——是「删一个模块 + 两个 enum」，而非满仓库追散落分支。

## 6. 验收

- dispatch 对 = delegating task 对话里的一组普通 `AGENT_CONVERSATION_TURN`，与该单元 finish 对同
  `origin_task_id`、同命运：L1 黑盒一并保留、L2 走主路径一并折。
- `fold_root_experience` 中 dispatch 对的 L2 专用配对 supersede 逻辑被删除（统一 origin 主路径）。
- 同 agent 子任务不再产生空壳 dispatch 占位对；gateway 写 delegate conversation turn、不写 `TASK_DISPATCH`。
- 活跃（未结束）delegating task 的在途 dispatch 对不被 fold（working set 保全）。
- `agent_recall` / `fold_root_experience` 不含旧 enum 分支；旧 enum 兼容逻辑仅存在于
  `legacy_dispatch.py` 单一适配层。
- `uv run pytest` 全绿。
