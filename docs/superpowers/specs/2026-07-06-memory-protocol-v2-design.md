# Memory 协议 v2 设计（目标形态完整稿）

日期：2026-07-06
状态：设计定稿，未实施
一句话：**记忆按 scope 归档、按 address 定位。**

前置讨论：本文档综合 2026-07-06 对 `ctx_weft/protocols/memory.py` 的五轮审计——
① 类型词汇审计（TASK_DISPATCH 对写侧已死、AGENT_CONVERSATION_TURN 成为 agent 层统一对话表示）
② 22 个 recall/count 调用点普查（limit 是伪能力兼潜伏 bug、count 可删、by_agent 是归属维度隐式化的副产品）
③ apply_compact 调用形状审计（仅 2 处、策展政策泄漏进 provider；fold 路径徒手 supersede+ingest 非原子）
④ postgres schema 核实（layer/role 列已物化落库，零数据迁移）
⑤ 命名审定（"layer" 误导为纵向堆叠，实为归属分区；scope 是该概念的行业正名，
原 MemoryScope 数据类实为坐标、让位更名 MemoryAddress）。

---

## 1. 设计原则

1. **协议沿慢轴画**：kind 只表内容种类（agent 根本上产出什么），永不表框架机制
   （框架怎么组织它们）。机制住 metadata，半年一变不穿透协议；种类几乎永不变。
   反面教材即现状：TASK_DISPATCH 对 / OBSERVER_SUMMARY / COMPACT_SUMMARY 全部僵尸化，
   而 §5.0 规定枚举永不可删。
2. **归档维度正交化**：scope 是"归谁"（归属范围），kind 是"是什么"，role 是"对话里的谁"。
   三者独立（what / whose / who），不再用 type 编码 scope×kind 的笛卡尔积。
3. **provider 返回事实、不做渲染；framework 书写、策展、渲染**：
   - 可重建不变量：CONVERSATION_TURN 记录携 role + metadata(tool_calls/tool_call_id)，
     足以无损重建 LLM messages；套标题/排版/悬空 tool_call 兜底全在框架侧。
   - 策展政策（保什么锚点、留几条、怎么分组折叠）是框架语义；provider 只提供
     原子执行原语（fold）。现状 apply_compact 的 keep_last/protect_types 是政策
     泄漏进 provider，v2 收回。
4. **读取面 = 三种记忆动作**：load_view（工作记忆回放）/ recall_topic（流消费）/
   recall_semantic（联想检索）。load_view 召回的不是"发生过什么"（那是事件溯源侧的事），
   而是"框架决定还值得被看见什么"——幸存者视图。
5. **不对称演进规则（写进协议文档的硬规则）**：
   - kind **封死**：新框架机制 = 新 metadata 约定，永不铸新 kind。判据：不同 kind =
     provider 可施加不同的存储/索引/保留策略；仅此才配新成员。
   - scope **可缓慢生长**：它是执行模型（session ⊃ agent ⊃ task）的投影，随执行模型
     演进而扩（如将来的 USER / TENANT 长期记忆）；每个 scope 一行归档/address 规则，
     加一个成员的成本可控。
   - 旧 type 字符串按 §5.0 永久可读，统一由读侧归一化模块兜底。

## 2. 词汇

```python
class MemoryKind(StrEnum):
    CONVERSATION_TURN = "conversation_turn"  # 对话回合（user/assistant/tool），各 scope 通用
    SUMMARY = "summary"                      # 遗忘补偿：段摘要 / 经验摘要（fold 的 replacement）
    TOOL_AUDIT = "tool_audit"                # 真实能力调用审计；默认不进视图装配
    PUBLICATION = "publication"              # topic 发布；按流读取（recall_topic）

class MemoryScope(StrEnum):
    """归属范围：这条记忆归谁。task-scoped 的私有执行转录 / agent-scoped 的跨 task 经验 /
    session-scoped 的共享黑板。命名注记：v1 叫 MemoryLayer——"层"误导为纵向抽象堆叠，
    实为横向归属分区；"scope" 是该概念的行业正名（原同名数据类让位更名 MemoryAddress）。"""
    TASK = "task"; AGENT = "agent"; SESSION = "session"
```

kind 的普适性成分：CONVERSATION_TURN + SUMMARY 是有界上下文 agent 的普适核心
（episodic 经历 + consolidated 固化）；TOOL_AUDIT / PUBLICATION 是本框架的务实搭车
（审计顺 supersede 记账、provider 兼任黑板基底）。scope 成员表是本框架执行模型的投影，
普适的是"按归属分区、不同分区不同生命周期"这条轴本身。

## 3. 数据结构

```python
@dataclass
class MemoryAddress:
    """定位坐标（v1 名 MemoryScope）。全址 = ingest 归档地址；
    半址 = load_view 过滤模式（None 字段 = 通配）。"""
    session_id: str
    task_id: str | None = None
    agent_id: str | None = None

@dataclass
class MemoryEvent:
    kind: MemoryKind
    scope: MemoryScope            # 归属范围，显式字段；不再由 type 推导（EVENT_LAYER 退役）
    address: MemoryAddress        # 归档地址（全址，per-scope 必填字段见 §4 ingest 不变量）
    content: str | list[ContentPart]
    timestamp: datetime           # 框架可回锚（如 dispatch 框锚 started_at）；provider 稳定排序
    role: Literal["user", "assistant", "tool"] | None = None   # 仅 CONVERSATION_TURN 有意义
    topic: str | None = None      # 仅 PUBLICATION 有意义
    causation_id: str | None = None
    metadata: dict = field(default_factory=dict)

@dataclass
class MemoryRecord:
    id: str
    kind: MemoryKind
    scope: MemoryScope
    address: MemoryAddress        # 来源回显（取代现状 metadata["task_id"] 打标）
    content: str | list[ContentPart]
    timestamp: datetime
    role: ... | None = None
    topic: str | None = None
    score: float | None = None    # 仅 recall_semantic 填
    metadata: dict = field(default_factory=dict)
```

删除：`CompactResult`（随 apply_compact 消亡）。
`Subscription` / `MemoryProviderInfo` 保留（Info 的 supports_compact_archival 改为
`archives_superseded: bool`——是否物理归档 superseded 行）。

## 4. 方法面（11 → 8）

### 写（2）

```python
async def ingest(event: MemoryEvent, ctx) -> str
```
- 追加一条事件，返回 id。provider 分配分区内 seq_no；(timestamp, seq_no) 为排序键。
- **ingest 不变量（per-scope 全址要求）**：TASK-scoped 事件的 address 必须携 task_id
  **且** agent_id（跨 task 聚合读依赖 agent_id 落在 task-scoped 行上——postgres 现已如此）；
  AGENT-scoped 必须携 agent_id（task_id 可留作 provenance 回显，不参与过滤）；
  SESSION-scoped 仅 session_id。
- PUBLICATION 特例：同 topic 旧发布标 superseded（覆盖语义，沿现状）。

```python
async def fold(supersede_ids: list[str], replacements: list[MemoryEvent], ctx) -> list[str]
```
- **原子"遗忘 + 补偿"**：把 ids 标 superseded 并写入 replacements（可空 = 纯遗忘；
  可多条 = finish 对替换这类成对写入），一个事务内完成，返回新事件 id 列表。
- 已 superseded / 不存在的 id 跳过（幂等）。
- **取代 supersede 与 apply_compact 两个方法**。统一现有四类调用形状：
  finalize 折末 raw 段（纯遗忘）、observe/bg 段折（遗忘 + 1 条 SUMMARY）、
  fold_root_experience（跨 scope 遗忘 + 1 条 SUMMARY）、_replace_finish_report
  （遗忘 2 条 + 补 2 条）。后三类现状是徒手 supersede+ingest，崩溃窗口内
  raw 已删而摘要未写 = 丢数据；fold 的原子契约修掉这一族隐患。
- keep_last / protect_types 政策整体上移框架侧：框架 load_view → 自行计算
  应折 id 集（protect = role=user 回合 + SUMMARY kind）→ fold。

### 读（3 = 三种记忆动作）

```python
async def load_view(address: MemoryAddress, scope: MemoryScope, ctx,
                    kinds: list[MemoryKind] | None = None) -> list[MemoryRecord]
```
- **工作记忆回放**：返回该归属分区**全量幸存**（未 superseded）记录，
  **时间正序**（timestamp, seq_no 升序——现状 newest-first 且几乎每个调用方都在 reversed()，
  v2 直接把正序定为契约；"最近一条"取 `[-1]`）。
- **无 limit**：22 个调用点普查，仅 2 个冷路径用 limit=1，其余全是"给我全部"的哨兵值；
  且 newest-first 截断丢最老端，会静默砍掉锚在最早的滚动 SUMMARY 与铸框幂等检查
  依赖的历史框。视图天然有界（≈一个 LLM context，超了 compact 触发），
  为不存在的规模问题付静默截断的正确性代价不值。
- **无 count_recent**：len(视图) 即可（3 个计数调用点全部如此替换）。
- **address 半址过滤规则**：非 None 字段皆为过滤条件；每个 scope 的合法过滤字段——
  TASK → task_id / agent_id（跨 task 聚合 = 只给 agent_id）；AGENT → agent_id；
  SESSION → 无。**非法非 None 字段抛 ValueError**（显式失败，抓住"by_agent 调用点
  忘置 task_id=None"这类静默漏召回）。
- kinds=None 默认 = CONVERSATION_TURN + SUMMARY（工作记忆视图的定义）；
  需要 TOOL_AUDIT（如计算折叠 id 集）时显式传。
- `recall_recent_by_agent` 随之消解：它与 recall_recent 在两个 provider 里
  本就是同一条查询、仅差 WHERE task_id= 还是 agent_id=——
  即 `load_view(MemoryAddress(session, agent_id=A), scope=TASK)`。

```python
async def recall_topic(topic: str, since: int, ctx) -> tuple[list[MemoryRecord], int]
async def recall_semantic(query: str, address: MemoryAddress, top_k: int, ctx) -> list[MemoryRecord]
```
- 不变（address 半址语义同 load_view）。semantic 仍是可选能力（describe 声明）。

### 订阅（2）+ 能力（1）

`subscribe_topic` / `list_subscriptions` / `describe`：签名与语义不变。

## 5. metadata 约定注册表（框架侧文档，非协议 schema）

| key | 谁写 | 语义 |
|---|---|---|
| `tool_calls` | assistant 回合 | 无损重建：[{id, name, input}] |
| `tool_call_id` | tool 回合 | 与 assistant 回合配对 |
| `origin_task_id` | agent-scoped 回合 | 所属折叠单元（finish 对/dispatch 对分组键） |
| `parent_task_id` | agent-scoped 回合 | 单元父链（fold 顶层判定；result 回合不定义、prefer-non-None） |
| `inherited_from_task_id` | inherit 快照 | 镜像来源 |
| `outcome` | finish/result 回合 | fail 前缀渲染依据 |
| `invocation_id` / `tool_name` / `arguments` | TOOL_AUDIT | 审计明细 |

新机制（如未来的胶囊变体、新配对协议）在此表加行，协议零改动。

## 6. Legacy 兼容（零数据迁移）

postgres `memory_events` 已有 layer（带索引）与 role 列，存量行二者皆正确 → 不动任何数据。
**列名 `layer` 不改**——协议词汇与存储列名解耦，provider 内部把列值映射到 MemoryScope
（列名是实现细节，不在协议面上）。

**读侧统一归一化模块**（吸收并取代 `legacy_dispatch.py`，全仓唯一认识旧词汇的地方）：

| 旧 type | → kind | scope | 归一化动作 |
|---|---|---|---|
| user_prompt / llm_response / tool_result | CONVERSATION_TURN | TASK | 按已存 role 直通 |
| tool_invocation | TOOL_AUDIT | TASK | 直通 |
| task_compact_summary / agent_compact_summary / compact_summary | SUMMARY | 按已存 layer 列 | 直通 |
| agent_conversation_turn | CONVERSATION_TURN | AGENT | 直通 |
| task_dispatch / task_dispatch_result | CONVERSATION_TURN | AGENT | 配对成回合、孤儿隐藏（现 legacy_dispatch 逻辑） |
| blackboard_publish | PUBLICATION | SESSION | 直通 |
| observer_summary | （不映射） | — | 本就不进装配；v2 落地前先杀两个写点 |

provider 查询侧：kinds → type 字符串集合的别名展开表
（如 CONVERSATION_TURN@TASK → {conversation_turn, user_prompt, llm_response, tool_result}），
展开进 `type IN (...)`；返回前经归一化模块重打 kind。
旧枚举成员按 §5.0 永不物理删除，退役为别名表的键。

## 7. 与现状的差异总表

| 现状 | v2 | 理由 |
|---|---|---|
| MemoryEventType 12 成员（type=scope×kind） | MemoryKind 4 成员 + scope 显式字段 | 慢轴原则；EVENT_LAYER 退役 |
| MemoryLayer | **MemoryScope**（更名） | "层"误导纵向堆叠；scope 是归属分区的行业正名 |
| MemoryScope 数据类 | **MemoryAddress**（更名） | 它是坐标不是范围：全址归档 / 半址过滤（None=通配） |
| recall_recent(newest-first, limit) | load_view(正序, 无 limit) | limit 普查=伪能力兼 bug；正序免去调用方 reversed |
| recall_recent_by_agent | 消解为半址 MemoryAddress(agent_id=A) | 两 provider 里本是同一条查询 |
| count_recent | 删除，len(视图) | 3 调用点全可替换 |
| supersede + apply_compact | fold(ids, replacements) 原子原语 | 策展政策上移框架；修徒手 supersede+ingest 的崩溃丢摘要窗口 |
| CompactResult | 删除 | 随 apply_compact 消亡 |
| metadata["task_id"] 来源打标 | MemoryRecord.address 回显 | 结构化取代约定 |
| legacy_dispatch.py 专用 shim | 并入统一归一化模块 | 全仓一处认识旧词汇 |

方法数 11 → 8；provider 实现净变薄（postgres 删 `_scope_layer_filter` 分组与 count 查询，
in-memory 删 by_agent 特例与 apply_compact 政策逻辑）。更名涟漪零边际成本：
v2 迁移本就触碰全部调用点。

## 8. 落地阶段（每步独立可合、全程绿灯）

1. **预清场**（小）：杀 OBSERVER_SUMMARY 两个写点（runtime tracking flush / suspend 摘要）；
   prepare/act 估算清单剔除死类型。
2. **协议扩容**（中）：新词汇（MemoryKind / MemoryScope / MemoryAddress）+
   load_view / fold 落两个 provider + 别名展开 + 统一归一化模块。旧方法与旧名保留为
   薄包装/别名（委托新方法），不动任何调用点。
3. **调用点迁移**（中偏大）：core 16 文件切新 API。重点盯：
   - by_agent 三调用点（agent_recall.py / runtime.py inherit / compact.py fold）显式
     半址（task_id=None）——address 严格校验会把漏改变成 loud error；
   - compact.py / finalize.py 的类型筛选改 kind+role 筛选，逐个对照语义
     （短叶数 assistant 轮次、末段折保 role=user 锚点）；
   - 徒手 supersede+ingest 四处改 fold。
4. **测试迁移 + 薄包装日落**（大而机械）：ctx-weft 54 + host 3 测试文件；
   删 recall_recent / by_agent / count_recent / supersede / apply_compact 包装与旧名别名。

## 9. 风险

- **降级兼容**：升级后新写入的 kind 字符串旧版本认不出——升级后的新会话降级不可见
  （存量会话不受影响，零数据重写）。发版说明按"新数据不向下兼容"处理，无数据损坏。
- **compact.py 折叠语义**（29 处引用）最易改出行为差异；golden 测试网
  （test_dispatch_fold_golden / test_cross_layer_fold / capsule 一族）是主要安全网。
- **fold 原子性**在 in-memory provider 用锁模拟即可；postgres 单事务天然满足。
- 总量估计：src 约 19 文件实质改动 + 57 测试文件机械迁移，3–5 个专注工作日，
  分四个独立分支/验证周期。
