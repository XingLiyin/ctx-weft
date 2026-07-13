# task 层段摘要承载为 assistant 自述（role + 包装两层统一）

- 日期：2026-06-27
- 范围（均在 `ctx_weft/` 本仓 + host postgres provider）：
  - `providers/memory_blackboard/in_memory.py`（`apply_compact` 按 layer 写 role）
  - `src/ipmastercowork/providers/memory/postgres.py`（同上，host 侧 provider）
  - `core/assembler/sources/_history.py`（`record_to_history_block` 包装判据：按 type → 按 role）
  - `core/loop/steps/finalize.py`（`_synthesize_dispatch_pair` 删 `user→assistant` 覆盖特判）
- 关系 spec（本设计**修订**）：
  - `docs/superpowers/specs/2026-06-26-root-experience-summary-fold-design.md` 的 **§2.3.2 / §2.4**：
    task 层段摘要也承载为 assistant + 不套包装；postgres「统一 role=user」注释作废。
  - `docs/superpowers/specs/2026-06-26-interaction-preserving-capsule-design.md` 的 **§3.3 step2**：
    不再需要「TASK_COMPACT_SUMMARY 覆盖 DB role=user→assistant」——存储已是 assistant，close 自然继承。
  - 底层机制 spec：`docs/spec/06-memory-layers-and-compaction.md`。

---

## 1. 问题

运行层（task 层）的 `TASK_COMPACT_SUMMARY` 是「LLM 对相邻两条用户消息之间自身处理段的自述总结」，
语义上属 **assistant**。但现状把它存成 `role="user"`、渲染时再套「［以下是先前对话/经验的压缩摘要，
供你延续工作参考；并非用户的新指令］」包装前缀，把一段「我干了啥」的自述伪装成「系统给的背景」。

后果两条：

1. **语义错位**：assistant 的工作自述以 user 身份出现，还要靠免责声明（「并非用户的新指令」）消歧义。
2. **两层不一致 / smell**：close 合成胶囊时 `_synthesize_dispatch_pair`（finalize.py:247-248）必须把
   `TASK_COMPACT_SUMMARY` 的 role **从 user 覆盖回 assistant**——存储与胶囊两层 role 不一致，靠一处特判
   缝合。`inherit_memory`（跨 agent 继承 parent OPEN 对话）原样透传 role 时，还会在 parent 的对话流里
   **夹一条伪 user**（段摘要），而同段的 `LLM_RESPONSE` 却是 assistant，更显割裂。

### 1.1 现象（证据）

会话 `ses_01KW6D7BRRSRV0DHNQWHCJR76P`（root agent `agt_01KW6D7BS6WN7KM0DJDCHKTPNZ`）resume 后的
prompt（`LLMPromptSent` req 8）中，段摘要逐条以 `role=user` + 包装前缀呈现：

```
[user] 检查一下你工作目录的项目现状…               ← USER_PROMPT（锚点）
[user] ［…压缩摘要…］### 会话目标 / ### 已完成工作   ← TASK_COMPACT_SUMMARY（本应 assistant 自述）
[user] 帮我把这个ppt转成pdf                         ← USER_PROMPT（锚点）
```

memory_events 里所有 `task_compact_summary` 记录 `role='user'`，写入点硬编码（in_memory.py:257、
postgres.py:364「注入给下一轮 act loop 的上下文统一 role=user」）。

---

## 2. 范围与不变量

1. **只改 A（task 层 `TASK_COMPACT_SUMMARY`）**：渲染/存储为 assistant + 不套包装。
2. **B（agent 层折叠摘要 `AGENT_COMPACT_SUMMARY`）保持 user + 包装不动**：它是 `fold_root_experience`
   折叠后**最靠前的 history 消息**（`anchor−1µs`），是 prompt **首条**；Anthropic 首条 assistant 直接 400，
   故其 role=user 是承重的（相邻 spec §2.3.1）。B 的渲染走 `agent_experience.py` / `agent_recall.py`
   自己的显式 `wrap_compact_summary` + role=user 分支，**与 A 的渲染路径天然分离**，不受本设计波及。
3. **不变量 1（USER_PROMPT 永不折叠）已落地、本设计依赖之**：三个 task 层 compact 入口
   （`observe.py:361`、`background_observe.py:50`、`compact.py:235`）**均已传 `protect_types=(USER_PROMPT,)`**。
   故段摘要前面**恒有 user 锚点垫着、永远非首条**。
4. **段摘要语气不改**：内容仍为现状的报告体（`### 会话目标 / ### 已完成工作`）；只动 role/包装。
   第一人称自述化是独立议题，不绑入本设计（YAGNI）。

---

## 3. 设计

### 3.1 写入层 · `apply_compact` 按 layer 写 role

`apply_compact` 是 A、B 共用写入点，靠 `layer` 分 `summary_type`。把硬编码的 `role="user"` 改为按 layer 分：

| layer | summary_type | role |
|---|---|---|
| `TASK` | `TASK_COMPACT_SUMMARY`（A） | **`assistant`** |
| `AGENT` | `AGENT_COMPACT_SUMMARY`（B） | `user`（不变，首条承重） |

- `in_memory.py`：line 252-258 的 `MemoryEvent(role="user", …)` 改为按 `layer is MemoryLayer.TASK` 取
  `"assistant"` / `"user"`。
- `postgres.py`：line 363-364 同样按 layer 取 role；删/更新「统一 role=user」注释。
- 两份 provider 逻辑同构，须同步改、同测试覆盖。

### 3.2 渲染层 · `_history.py` 包装判据「按 type → 按 role」

`record_to_history_block`（`_history.py:36-38`）现状：
```python
if record.type == MemoryEventType.TASK_COMPACT_SUMMARY:
    text = wrap_compact_summary(text)
role = record.role or "user"
```
改为按 role 决定是否包装（包装本就是给 **user 身份**的摘要消歧义；assistant 自述无需）：
```python
role = record.role or "user"
if record.type == MemoryEventType.TASK_COMPACT_SUMMARY and role == "user":
    text = wrap_compact_summary(text)   # 仅防御旧数据（§4 R3）；新数据 role=assistant 不套
```
- 新数据：`TASK_COMPACT_SUMMARY` role=assistant → **不套包装**，渲染成干净的 assistant 自述。
- B（`AGENT_COMPACT_SUMMARY`）不经过本分支，行为不变。

### 3.3 close 层 · `finalize.py` 删覆盖特判

`_synthesize_dispatch_pair`（finalize.py:243-250）现状对 `TASK_COMPACT_SUMMARY` 特判 `role="assistant"`
覆盖存储的 user。存储改为 assistant 后，该特判冗余：
```python
# 删除：
elif r.type == MemoryEventType.TASK_COMPACT_SUMMARY:
    role = "assistant"
# 保留通用分支即可（自然继承存储的 assistant）：
else:
    role = r.role or "user"   # USER_PROMPT→user 分支保留在前
```
- `USER_PROMPT → user` 分支（line 245-246）保留。
- 更新 docstring（line 224-225）：不再描述「覆盖 DB 存储的 role=user」。

### 3.4 不动的部分

- B（`AGENT_COMPACT_SUMMARY`）全链路：写入 role=user、`agent_experience.py:77-81` /
  `agent_recall.py:91-101` 的显式包装、folding。
- 段摘要的 timestamp/seq 归位（§3.1 写入处的 `summary_ts`/`summary_seq` 逻辑）。
- `apply_compact` 的 `protect_types` / `keep_last` 折叠语义。
- `inherit_memory`（runtime.py:95-123）：仍继承 `TASK_COMPACT_SUMMARY`，`role=r.role` 原样透传
  （现在透传 assistant）；line 109 仅在 `assistant + 有 tool_calls` 时加 tool_calls，段摘要无 tool_calls
  → 不误加，安全。

---

## 4. 风险 / 边界

- **R1 · `ensure_leading_user` 不会吞段摘要（不变量保证，非取舍）**：assistant 段摘要「非首条」由
  不变量 1（`protect_types=(USER_PROMPT,)`，三入口均已落地，§2.3）保证——USER_PROMPT 恒未 superseded、
  恒排在段摘要之前。task 层 live、inherit_memory 继承序列（含 parent USER_PROMPT + 子任务自身
  USER_PROMPT，双重保险）、close 胶囊（首条 USER_PROMPT 镜像）三条路径均成立。**守护测试见 §5 T6**。
- **R2 · composer 去重行为变化（需测试）**：`_progress_already_in_compact`（composer.py:556-568）按
  `type==TASK_COMPACT_SUMMARY` + **内容精确相等**判去重。去包装后 `b.content` 变裸摘要，
  `content == progress`（process_report 裸文本）在 max_turns 复用路径下可能从「不生效」变「生效」。
  语义上正确（同份报告不渲染两遍），但须测试确认不误删非 max_turns 路径的独立摘要。
- **R3 · 历史数据不迁移**：既有 role=user 的旧 `TASK_COMPACT_SUMMARY` 不回填。§3.2 渲染判据按 role：
  旧数据仍按 user + 包装渲染（保持旧行为），新数据 assistant 不包装。无需数据迁移脚本。

---

## 5. 测试计划

全部 `uv run pytest`（pyproject 已配 `pythonpath=["."]`）。

- **T1 · `apply_compact(TASK)` 写 assistant**（in_memory + postgres 各一）：折叠后产出的
  `TASK_COMPACT_SUMMARY` 记录 `role == "assistant"`；USER_PROMPT 仍未 superseded（protect_types）。
- **T2 · `apply_compact(AGENT)` 仍 user**（in_memory + postgres 各一）：`AGENT_COMPACT_SUMMARY`
  记录 `role == "user"`（B 不变）。
- **T3 · 渲染去包装**：`record_to_history_block` 对 role=assistant 的 `TASK_COMPACT_SUMMARY` 产出
  ContextBlock role=assistant 且内容**不含**包装前缀；对（构造的）role=user 旧数据仍套包装（§4 R3）。
- **T4 · B 渲染不变**：`agent_experience` / `agent_recall` 渲染 `AGENT_COMPACT_SUMMARY` 仍 role=user
  且带包装。
- **T5 · finalize 删特判后胶囊正确**：跑 `_synthesize_dispatch_pair`，断言镜像的段摘要回合 role=assistant
  （来自存储，非特判覆盖）；相邻 spec / capsule spec 的 A1/A2 golden 不破。
- **T6 · 守护不变量（R1）**：构造 task 层 compact + inherit_memory 两条路径，断言段摘要**永不是序列首条**、
  其前恒有 USER_PROMPT；经 gateway `ensure_leading_user` 后段摘要未被丢弃。
- **T7 · composer 去重（R2）**：max_turns 复用 process_report 作 `TASK_COMPACT_SUMMARY` 时，去包装后
  `_progress_already_in_compact` 命中、Current Progress 去重生效；独立规则摘要（内容不同）**不**误删。
- **T8 · inherit 透传 assistant（安全）**：parent OPEN 对话含段摘要，inherit 后子 agent 的
  `AGENT_CONVERSATION_TURN` role=assistant、无误加 tool_calls。
- **回归 · 相邻 spec §5 F3 反转**：原断言「task 层 `TASK_COMPACT_SUMMARY` 渲染带包装」→ 更新为
  assistant 不带包装；F2（首条恒 user）仍绿。

---

## 6. 受影响文件清单（实现期由 writing-plans 排序）

- `providers/memory_blackboard/in_memory.py` — `apply_compact` 按 layer 写 role（§3.1）。
- `src/ipmastercowork/providers/memory/postgres.py` — 同上 + 改注释（§3.1）。
- `core/assembler/sources/_history.py` — 包装判据按 role（§3.2）。
- `core/loop/steps/finalize.py` — 删 `TASK_COMPACT_SUMMARY → assistant` 覆盖特判 + 更新 docstring（§3.3）。
- 测试：provider 单测（in_memory/postgres）、`_history` 渲染、finalize golden、composer 去重、inherit；
  相邻 spec F3 回归更新。

> 同步约定（memory：上游 wefta→weft）：本设计动 core（落 `ctx-weft`）+ host postgres provider；
> 若需回灌上游 LoomeX-00 按既有同步流程处理。

## 7. 验收

- A 类 prompt（task 层 live / inherit / close 胶囊）中段摘要均以 **assistant 自述**呈现、无包装前缀。
- B 类（agent 层折叠摘要）仍 user + 包装、prompt 首条恒 user，不破。
- §5 全部测试 + `uv run pytest` 全绿；相邻 spec 复现会话回归不破。
