# ctx-weft 设计文档

> **ctx-weft · v0.17 草案 · 2026-05-16**
>
> 本文档是 miniAgents demo 之后的下一代 agent runtime core 设计。ctx-weft 是 greenfield 重写，仅参考 miniAgents 的设计思路（详见 `miniAgents_设计文档.md`），代码不复用。
> 
---

## 目录

1. [项目目标与定位](#1-项目目标与定位)
2. [设计原则与约束](#2-设计原则与约束)
3. [总体架构](#3-总体架构)
4. [三大协议（Knowledge / Memory / Capability）](#4-三大协议knowledge--memory--capability)
5. [Context Assembler：上下文装配流水线](#5-context-assembler上下文装配流水线)
6. [Step 化 Loop Engine](#6-step-化-loop-engine)
7. [控制面 API（Control Plane）](#7-控制面-apicontrol-plane)
8. [状态模型与持久化](#8-状态模型与持久化)
9. [Event 体系](#9-event-体系)
10. [Streaming 数据流](#10-streaming-数据流)
11. [LLM 适配层](#11-llm-适配层)
12. [Service Shell（CtxWeft.host）](#12-service-shellCtxWefthost)
13. [包结构与模块边界](#13-包结构与模块边界)
14. [关键数据契约（精确签名）](#14-关键数据契约精确签名)
15. [V1 范围 / V2 展望](#15-v1-范围--v2-展望)
16. [设计决议（原 TBD）](#16-设计决议原-tbd)

---

## 1. 项目目标与定位

### 1.1 一句话定位

ctx-weft 是一个**协议化、可控、可观测的 Agent Runtime Core**：
向外承接知识 / 记忆 / 能力三大外部系统（协议化接入），向内完成上下文装配与 Loop 驱动，最终驱动 LLM 完成工作。

### 1.2 形态

提供两种使用方式，共享同一个 core：

| 形态 | 包名 | 适用人群 |
|------|------|----------|
| **Runtime SDK** | `ctx-weft` | 自建 Agent 应用的开发者，import 后构建自己的 agent 程序 |
| **Service Shell** | `ipmastercowork` | 想要一个开箱即用 Agent 服务的团队，HTTP/SSE 直接调用 |

`ipmastercowork` 是 `ctx-weft` 的薄壳——所有业务逻辑在 core，host 只做协议转换、鉴权、持久化适配。

### 1.3 与 miniAgents 的关系

ctx-weft 继承了 miniAgents 的核心设计思路：
- 三阶段 Loop（Reasoner / Actor / Observer）
- 动态多层 Agent 树 + Spawn / Suspend / Resume
- Blackboard 父子单向通信
- HITL 一等公民
- token_budget 硬限制 + Loop Guard

ctx-weft 改进了 miniAgents 的两个核心问题：
1. **耦合震中（Reasoner）**：拆分为协议化 Context Source + 纯函数式 Assembler
2. **运行时控制弱**：把 Loop 拆为 Step，每个 Step 产生事件，控制面提供 pause/resume/inspect/replay 统一 API

---

## 2. 设计原则与约束

### 2.1 核心设计原则

1. **协议高于实现**。Knowledge / Memory / Capability 都先有协议（abstract base class + 数据契约），core 只依赖协议，不依赖任何具体实现。
2. **状态变更走事件唯一入口**。所有 state mutation 都必须经过 `apply(event)`，事件先入 log，再触发投影。代码上看不到"直接改字段"的写法。
3. **Step 是最小调度单元**。每个 Step 必须：可暂停、可恢复、可观测、生产至少 1 个事件。
4. **Streaming First**。LLM、Context Assembly、Events 都是 stream 优先；同步 API 是 stream API 的封装。
5. **Pure Core**。`ctx-weft` 包不直接做任何 I/O——文件、网络、数据库、LLM 调用，全部通过 provider 协议委托给外部。
6. **Greenfield，简单优先**。V1 不为 V2 留太多扩展点，宁可重构。

### 2.2 V1 工程假设（写在显眼处，便于将来重审）

| 假设 | 说明 | 解除时机 |
|------|------|----------|
| 单进程部署 | TaskQueue 内存态、Event Bus 进程内 | V2 引入 Redis Streams |
| 单租户 | 但 core 字段统一用 `context_id` 不写 `user_id` | V1.x 引入 tenant 维度 |
| Python 主语言 | 协议规范以 Python 抽象基类定义 | V2 出 TS SDK |
| 同步 Step + 异步 IO | Step 状态机同步可读，IO 走 asyncio | 不计划改变 |
| LLM 推理是黑盒 | core 不假设 LLM 内部结构，只看请求/响应/工具调用 | 不计划改变 |

### 2.3 强约束（不允许违反）

- core 包不允许有 `import requests` `import httpx` `open()` `Path(...).read_text()` 这类直接 I/O
- core 包不允许直接读环境变量，配置通过 `LoomConfig` 注入
- 任何"全局单例"必须是显式注入而非模块级 import；Provider Registry 是局部 scope，不是 module-level
- 所有 protocol 接口必须是 `async def`，同步用户用 `asyncio.run` 包

---

## 3. 总体架构

### 3.1 分层

```
┌──────────────────────────────────────────────────────────────────┐
│  CtxWeft.host          (Service Shell · 可选)                     │
│    REST / SSE / WebSocket · 鉴权 · 持久化适配 · HITL Webhook       │
└──────────────────────────────────────────────────────────────────┘
                              ▲ import / DI
┌──────────────────────────────────────────────────────────────────┐
│  CtxWeft.core           (Runtime Core)                            │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │ Orchestrator                                                │  │
│  │   SessionManager · TaskManager · LifecycleManager           │  │
│  │   TaskQueue (内存LIFO + blocked DAG)                         │  │
│  └────────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │ Loop Engine                                                 │  │
│  │   Step Driver · Step 集合（Reason/Act/Observe/Finalize/...） │  │
│  │   Guard（token / turns / concurrency）                       │  │
│  └────────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │ Context Assembler                                           │  │
│  │   ContextRequest → Source 调度 → ContextBlock → Prompt       │  │
│  │   Budget Strategy（裁剪 / 摘要 / 优先级）                    │  │
│  └────────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │ Control Plane                                               │  │
│  │   RunHandle · EventBus · CheckpointStore · ReplayEngine     │  │
│  └────────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │ LLM Gateway                                                 │  │
│  │   LLMClient门面 · Adapter · Streaming                        │  │
│  └────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
        ▲ 只依赖协议        ▲ 只依赖协议         ▲ 只依赖协议
┌─────────────────┐  ┌─────────────────┐  ┌──────────────────────┐
│ KnowledgeProto  │  │  MemoryProto    │  │  CapabilityProto     │
│  动态检索参考料  │  │ short / long /  │  │  identity / tool /   │
│  → messages     │  │  blackboard     │  │   skill / sub-agent  │
│                 │  │  → messages     │  │   → system prompt    │
└─────────────────┘  └─────────────────┘  └──────────────────────┘
        ▲                    ▲                       ▲
   外部 Provider 实现 / 外部知识库 / 外部记忆系统 / 外部能力系统
   （ctx-weft 不包含任何 provider 实现）

context 归位口诀：
  - System Prompt ← Capability（我是谁 + 我能做什么）
  - Messages      ← Memory（我记得什么）+ Knowledge（我看到什么）+ Task Spec（我被问什么）
```

### 3.2 数据流概览

> 本节给出 task 执行的简化路径；**完整端到端流程（含 session 启动 / agent 实例化 / capability 绑定 / per-step 解析）见 §4.6.7**。

一次 task 的执行典型路径：

```
User → SessionManager.create_session
     → TaskManager.start_session
     → LoopEngine.run(task)
       │
       ├─ Step[1] = ReasonStep
       │    └─ ContextAssembler.assemble(ContextRequest)
       │         ├─ CapabilityProvider.list(...)              → system prompt
       │         ├─ MemoryProvider.recall_recent(...)         → messages (history)
       │         ├─ MemoryProvider.recall_topic(...)          → messages (订阅 topic + 子任务结果)
       │         ├─ MemoryProvider.recall_semantic(query,...) → messages (语义召回，可选)
       │         └─ KnowledgeProvider.retrieve(...)           → messages (RAG 引用)
       │    → ContextBlock[]（区分 target: system/messages）
       │    → AssembledPrompt
       │    → Event: ReasonCompleted
       │
       ├─ Step[2] = ActStep
       │    ├─ LLMGateway.complete(prompt, stream=True)
       │    │    → Events: LLMTokenReceived (流式) · LLMResponseFinal
       │    ├─ tool_calls → CapabilityProvider.invoke(...)
       │    │    → Events: ToolStarted · ToolFinished
       │    └─ Event: ActCompleted
       │
       ├─ Step[3] = ObserveStep
       │    └─ Event: TaskOutcomeDecided
       │
       └─ Step[N] = FinalizeStep
            ├─ MemoryProvider.write_episodic(...)
            ├─ MemoryProvider.write_blackboard(...)
            └─ Event: TaskFinished
```

每个 Step 都是可暂停的。HITL、debug pause、用户主动 cancel，都通过 control plane 拦截在 step 边界。

---

## 4. 三大协议（Knowledge / Memory / Capability）

### 4.1 设计思想

三协议是 ctx-weft 的接入面。任何外部系统想让 agent 用上自己——只需要实现协议、注册到 ProviderRegistry，core 自动用上。

**三协议在 context 中的归位**：

| Provider | 内容性质 | Context 落位 | 典型来源 |
|---------|---------|------------|---------|
| **Capability** | 设定 / 能力 | **system prompt** | SOUL/ROLE（identity）· tools · skills · sub-agents |
| **Memory** | 记忆 | **messages**（历史/背景/订阅） | 对话窗口 · episodic · BACKGROUND.md · blackboard |
| **Knowledge** | 参考资料 | **messages**（user 引用块） | RAG · Wiki · 文档检索 |

这个划分是 ctx-weft 与上一代 demo 的关键差异。原 demo 中"SOUL.md → system prompt"、"BACKGROUND.md → system prompt"、"工具列表 → system prompt"是三条并列硬编码路径；ctx-weft 把它们统一为协议化的"capability 走 system / memory & knowledge 走 messages"，Composer 不再 case-by-case 处理。

**关键设计选择**：

- 每个协议是一个 Python `abc.ABC` 抽象基类（V2 出 TS 时改成 JSON Schema + Protocol Buffer）
- 所有方法 `async def`
- 所有方法接受 `ctx: ProviderContext`（携带 session_id / task_id / agent_id / tenant_id / trace_id / cancel_token）
- 所有返回值是 dataclass（或 pydantic model），不是 dict——便于 schema 演进
- Provider 可以多实例并存（Memory 例外，见 §4.5），允许混用（一个 agent 同时挂 file knowledge + wiki knowledge）

### 4.2 KnowledgeProvider

**定位**：动态检索的参考资料。Agent 主动询问、按需取回的外部文献——RAG 结果、Wiki 片段、API 文档、行业规则查询。

**关键约束**：Knowledge 是 **dynamic & retrieval-based**，不是静态注入。它通过查询触发，作为 user 角色引用块进入 messages（"这是我刚查到的资料"），不进 system prompt。
- 项目背景之类的静态/半静态长期上下文 → 走 MemoryProvider 的 blackboard / long-term
- 角色/人格定义 → 走 CapabilityProvider 的 identity

```python
class KnowledgeProvider(Protocol):
    name: str

    async def retrieve(
        self,
        query: KnowledgeQuery,
        ctx: ProviderContext,
    ) -> AsyncIterator[KnowledgeDoc]:
        """流式返回相关知识片段。Core 用这些 doc 拼成 user 引用块注入 messages。"""

    async def describe(self, ctx: ProviderContext) -> KnowledgeProviderInfo:
        """返回 provider 元信息：可检索 collection 列表、是否支持 filter、推荐 top_k 范围等。"""


@dataclass
class KnowledgeQuery:
    text: str                  # 查询文本（自然语言或关键词）
    intent: Optional[str]      # "policy" | "reference" | "api_spec" | ...（由 KnowledgeRetrievalSource 推断）
    top_k: int = 5
    filters: dict = field(default_factory=dict)

@dataclass
class KnowledgeDoc:
    id: str
    content: str                       # 知识正文（markdown）
    score: float                       # 相关度
    source: str                        # provider name 或子源标识
    metadata: dict = field(default_factory=dict)
    citation: Optional[Citation] = None  # 引用信息（URL / 段落锚点），用于 message 块内附引用
```

**典型实现**：
- `LightRAGProvider` / `GraphRAGProvider`：对接外部 RAG
- `ConfluenceProvider` / `NotionProvider`：企业知识库
- `VectorSearchProvider`：通用向量检索

> ⚠️ **不属于 Knowledge 的内容**：BACKGROUND.md（→ MemoryProvider long-term）、SOUL.md / ROLE.md（→ CapabilityProvider identity）。这些虽然形态像"知识"，但在 context 装配中的语义不同，归属于其语义对应的 provider。

### 4.3 MemoryProvider（统一协议）

**定位**：ctx-weft 中**唯一的 memory 抽象**。承载所有"短期 / 长期 / Blackboard"职能——它们在 ctx-weft 中是**同一概念**，不再拆分。

**核心思想**：Provider 是一个**事件摄取 + 多模召回**的黑盒：

- core 把所有重要事件（user_prompt / llm_response / tool_invocation / tool_result / observer_summary / compact_summary / blackboard_publish）通过 `ingest()` 喂给 provider
- Provider 自由决定如何内化（按需存储、索引、压缩）
- core 通过三种 recall 接口取回所需 memory：**按时间近度**、**按 topic**、**按语义相似度**

**两种典型部署**：

| 形态 | 实现 | 内化方式 | 召回能力 |
|------|------|---------|---------|
| **core 默认**：StructuredBlackboardMemoryProvider | 单 Postgres，无 pgvector | 按 type 分类存储；compact 后归档旧事件 | recall_recent ✓ / recall_topic ✓ / recall_semantic ✗（返空） |
| **外部接入**：Mem0 / LightRAG / Zep / 自建 RAG | 各自后端（向量库 / 知识图谱 / ...） | 实时索引每个事件 | 三种 recall 全支持 |

外部 provider 必须能 ingest 所有事件类型并提供至少一种召回——这是契约。

```python
class MemoryProvider(Protocol):
    """统一 memory：摄取所有事件 + 多模召回。
    单实例，必需注册。"""

    name: str

    # ── 摄取（write）──
    async def ingest(
        self, event: MemoryEvent, ctx: ProviderContext,
    ) -> str:
        """写入一个 memory event。core 调用方必须为每个重要事件调用 ingest；
        provider 自由决定是否持久化、如何索引。"""

    # ── 召回（read，三种模式）──
    async def recall_recent(
        self, scope: MemoryScope, types: list[MemoryEventType], limit: int,
        ctx: ProviderContext,
    ) -> list[MemoryRecord]:
        """按时间倒序返回最近 N 条指定类型事件。必需实现。
        ReasonStep 装配 messages 段的主路径。"""

    async def recall_topic(
        self, topic: str, since: int, ctx: ProviderContext,
    ) -> tuple[list[MemoryRecord], int]:
        """按 topic 拉取（自 since seq_no 之后），返回 (events, new_cursor)。
        必需实现。父子 task 通信 + 跨 session 订阅式上下文用。"""

    async def recall_semantic(
        self, query: str, scope: MemoryScope, top_k: int,
        ctx: ProviderContext,
    ) -> list[MemoryRecord]:
        """语义相似度召回。可选——core 默认实现返空；外部实现核心能力。
        Provider 通过 describe() 声明是否支持。"""

    # ── 订阅（cross-session 长期上下文）──
    async def subscribe_topic(
        self, session_id: str, topic: str, intent: str,
        ctx: ProviderContext,
    ) -> str:
        """session 订阅 topic。后续 BlackboardSource 装配时按订阅列表批量 recall_topic。"""

    async def list_subscriptions(
        self, session_id: str, ctx: ProviderContext,
    ) -> list[Subscription]: ...

    # ── 压缩（compact 触发时使用）──
    async def apply_compact(
        self, scope: MemoryScope, summary: str, keep_last: int,
        ctx: ProviderContext,
    ) -> CompactResult:
        """CompactStep 调用：摄取一条 compact_summary event，并指示 provider 把
        scope 内此 event 之前、超出 keep_last 范围的事件标记为 superseded
        （core 默认实现物理 archive；外部实现可能仅更新索引）。"""

    # ── 工具 ──
    async def count_recent(
        self, scope: MemoryScope, types: list[MemoryEventType], ctx: ProviderContext,
    ) -> int:
        """计数（供 ReasonStep 估算消息数）。"""

    async def describe(self, ctx: ProviderContext) -> MemoryProviderInfo: ...
```

**核心数据结构**：

```python
class MemoryEventType(StrEnum):
    USER_PROMPT       = "user_prompt"        # 用户任务输入
    LLM_RESPONSE      = "llm_response"       # actor LLM 一轮的完整输出（聚合流式 chunk）
    TOOL_INVOCATION   = "tool_invocation"    # actor 一次 capability 调用
    TOOL_RESULT       = "tool_result"        # capability 返回值
    OBSERVER_SUMMARY  = "observer_summary"   # FinalizeStep 写入的 verdict.summary
    COMPACT_SUMMARY   = "compact_summary"    # CompactStep 产出的 [Context so far]
    BLACKBOARD_PUBLISH = "blackboard_publish"  # 显式 topic 发布


@dataclass
class MemoryEvent:
    type: MemoryEventType
    scope: MemoryScope                   # session/agent/task 上下文
    content: str | list[ContentPart]
    timestamp: datetime
    # 以下字段有默认值，必须放在无默认值字段之后
    role: Optional[Literal["user", "assistant", "system", "tool"]] = None
    topic: Optional[str] = None          # 用于 topic-style 事件（含父子 task 通信）
    causation_id: Optional[str] = None   # 关联上游 event
    metadata: dict = field(default_factory=dict)


@dataclass
class MemoryScope:
    session_id: str
    task_id: Optional[str] = None
    agent_id: Optional[str] = None


@dataclass
class MemoryRecord:
    """召回返回的统一表示。"""
    id: str
    type: MemoryEventType
    content: str | list[ContentPart]
    timestamp: datetime
    role: Optional[Literal["user", "assistant", "system", "tool"]] = None
    topic: Optional[str] = None
    score: Optional[float] = None        # 仅 recall_semantic 时填
    metadata: dict = field(default_factory=dict)


@dataclass
class Subscription:
    session_id: str
    topic: str
    cursor: int                          # 已读到位置（seq_no），持久化
    intent: Literal["parent_child", "long_term_background", "long_term_project_log"]
    priority: int = 5


@dataclass
class CompactResult:
    events_before: int
    events_after: int
    summary_event_id: str                # compact_summary 事件的 id


@dataclass
class MemoryProviderInfo:
    name: str
    supports_semantic: bool = False
    supports_topic: bool = True
    supports_compact_archival: bool = True  # apply_compact 是否真的归档；外部实现可能只更新索引
    max_event_size_bytes: Optional[int] = None
```

#### 4.3.1 Ingest 的契约位置

core 必须在以下位置调用 `ingest()`——这是与 provider 之间的硬契约，确保外部 memory 系统看得到完整事件流：

| Loop 位置 | 摄取的事件 | 触发者 |
|----------|----------|--------|
| 每个 ActStep turn 末尾 | `LLM_RESPONSE`（聚合本轮 LLM 输出） | ActStep |
| 每次 capability invoke 前 | `TOOL_INVOCATION` | ActStep |
| 每次 capability 返回后 | `TOOL_RESULT` | ActStep |
| FinalizeStep | `USER_PROMPT`（若是新 prompt）+ `OBSERVER_SUMMARY` | FinalizeStep |
| CompactStep | `COMPACT_SUMMARY`（通过 apply_compact 间接 ingest） | CompactStep |
| 显式 publish 到 topic | `BLACKBOARD_PUBLISH` | 任何调用方 |

**Provider 自由决定存储策略**：
- core 默认实现：所有事件持久化到 `memory_events` 表；recall_recent 按 type 过滤
- 外部实现（Mem0 等）：所有事件喂给自己的索引管道；按需在召回时检索

> 因此 core 永远调 ingest，**永远不向 provider 隐瞒任何事件**——外部 memory 系统才能完整内化。

#### 4.3.2 Recall 的使用模式

| 调用方 | 召回模式 | 参数典型值 |
|-------|---------|---------|
| RecentMemorySource（ReasonStep 装配 messages 段） | `recall_recent` | types=[USER_PROMPT, OBSERVER_SUMMARY, COMPACT_SUMMARY]，limit=20 |
| BlackboardSource | `recall_topic` × N（每个订阅 topic 一次） | since=订阅 cursor |
| SemanticRecallSource | `recall_semantic` | query=task.user_prompt，top_k=5 |
| ReasonStep token 估算 | `count_recent` | 与 RecentMemorySource 同 type filter |

ReasonStep **不区分** memory 是来自"短期窗口"还是"长期记忆"——它只问 provider 要相关 records。Provider 决定怎么混合"recent 高优先 + 语义召回"等策略。

#### 4.3.3 与 KnowledgeProvider 的区别（澄清）

容易混淆，必须分清：

| 维度 | MemoryProvider | KnowledgeProvider |
|------|----------------|------------------|
| 写入 | core 自动 ingest 所有 agent 运行事件 | 不接受写入，纯只读 |
| 内容来源 | agent 自己经历的（对话、工具、总结） | 外部静态/半静态知识库 |
| 召回语义 | "我（agent）的过去" | "外部参考资料" |
| 典型问题 | "上次用户问过类似的问题吗？" | "Python 的 asyncio 怎么用？" |

虽然 prompt 装配时都进入 messages 段，但语义和数据来源完全不同。

#### 4.3.4 典型实现

**core 默认**：
- `StructuredBlackboardMemoryProvider`（V1 主推）：Postgres-backed，两张表（memory_events + memory_subscriptions），见 §8.7
- `InMemoryProvider`：单测/调试用

**外部接入**（用户根据需要选择，core 不 ship）：
- `Mem0MemoryProvider`：用户级个性化记忆，强 semantic
- `LightRAGMemoryProvider`：项目级 RAG，强 graph 关联
- `ZepMemoryProvider`：对话级 long-term memory，时序友好
- `CustomMemoryProvider`：自建（Pinecone / Weaviate / Qdrant + 用户自己的索引管道）

### 4.4 CapabilityProvider

**定位**：Agent 的**可调用动作**——tool / skill / sub-agent 三类。Capability 协议本质是"可 invoke 的能力"接入面。

> **设计边界**：Identity（SOUL/ROLE）虽然也在 system prompt 渲染，但**不属于** CapabilityProvider——它是 AgentTemplate 的内禀声明式内容，无 invoke 语义。详见 §4.6（AgentTemplate）。
>
> 三协议（Knowledge/Memory/Capability）共同特征是"对接外部运行时系统"。Identity 不是外部系统对接，是 template 自身的属性，因此不需要 protocol。这与 Knowledge/Memory 不包含 identity 概念是对称的。

**三类 capability**：

| kind | 是什么 | invoke 行为 | 在 prompt 中的呈现 |
|------|-------|-----------|------------------------|
| **tool** | 可调用工具（bash / http / read / write / MCP tool） | 执行工具，返回 result | LLM tools 数组 + system prompt 描述块 |
| **skill** | 可触发的过程性技能（SKILL.md） | 加载 skill 指令、按指令执行 | LLM tools 数组 + system prompt 描述块（指令按需懒加载） |
| **sub-agent** | 可派生的子 agent | 通过 LifecycleManager 派生子 agent | LLM tools 数组 + system prompt 描述块 |

```python
class CapabilityProvider(Protocol):
    name: str

    async def list(
        self, ctx: ProviderContext,
    ) -> list[Capability]:
        """返回 provider 暴露的全部 capability，含每个 capability 的 purposes 声明。
        Provider 知道自己每项能力适用于什么 purpose——这是 provider 的领域知识。"""

    async def invoke(
        self,
        capability_id: str,
        arguments: dict,
        ctx: ProviderContext,
    ) -> AsyncIterator[CapabilityEvent]:
        """流式返回执行事件（开始、进度、stdout、结束、错误）。"""

    async def cancel(
        self, invocation_id: str, ctx: ProviderContext,
    ) -> None: ...

    async def describe(self, ctx: ProviderContext) -> CapabilityProviderInfo: ...


@dataclass
class Capability:
    id: str                        # 全局唯一：通常是 "provider:cap_name"
    name: str
    kind: Literal["tool", "skill", "agent"]
    purposes: list[Purpose] = field(default_factory=lambda: ["act"])
                                   # 该 capability 在哪些 purpose 可见（见 §4.4.1）
                                   # 由 provider 在 list() 中声明，host 不 override
    description: str = ""          # 单字符串——所有 purpose 共用同一份描述
                                   # 设计选择：避免维护多份描述带来的不一致；
                                   # 若同一逻辑能力在不同 purpose 下行为差异大，
                                   # 应拆为两个独立 capability（不同 id）
    input_schema: dict = field(default_factory=dict)  # JSON Schema；按 kind 不同字段含义不同
    side_effects: bool = False     # 决定是否需 HITL 审批
    cost_hint: Optional[CostHint] = None
    tags: list[str] = field(default_factory=list)


Purpose = Literal["act", "observe", "compact"]
```

#### 4.4.1 Purpose 概念与过滤规则

**Purpose 定义**：purpose 标识 capability 在哪个 LLM 调用阶段可见，对应 ctx-weft 的 Step 模型：

| Purpose | 由哪个 Step 触发的 LLM 调用 | 典型 capability |
|---------|--------------------------|----------------|
| `act` | ActStep 内部的 actor LLM 调用（多轮） | bash_exec / http_request / submit_plan / submit_task |
| `observe` | ObserveStep 内部的 observer LLM 调用 | submit_task_assessment / request_human_input |
| `compact` | CompactStep 内部的 compaction LLM 调用 | read / glob（仅查阅类） |

**声明者职责划分**：
- **Provider 声明 purposes**：capability provider 在 `list()` 中为每个 capability 标注它适用于哪些 purpose。这是 provider 的领域知识——`submit_task_assessment` 显然只有 observe 阶段才有意义，`bash_exec` 显然不该在 observe 出现。
- **Host 在 agent 配置层只做允许/禁用**：host 通过 agent template 的 capability_refs 列表决定 agent 能用哪些 capability，但**不修改 capability 自身的 purposes 字段**。
- **Core 做过滤**：CapabilitySource 在装配时按 `request.purpose ∈ c.purposes` 过滤。

**过滤规则**（在 CapabilitySource 内执行）：

```python
async def fetch(self, request: ContextRequest, providers):
    bound = capability_cache.get(request.agent.id)         # 实例化时已 resolve 的快照
    visible = [c for c in bound if request.purpose in c.purposes]
    authorized = await self._authorizer.filter(visible, request.agent, ctx)
    for cap in authorized:
        yield ContextBlock(
            source="capability", kind="capabilities", target="system",
            content=render_capability_for_llm(cap),
            metadata={"capability_id": cap.id, "input_schema": cap.input_schema, ...},
        )
```

> **同一 capability 不同 purpose 需要不同 description 怎么办**：拆成两个独立 capability（不同 id）。例：`hitl:request_input_during_act`（purposes=["act"]）+ `hitl:request_review_during_observe`（purposes=["observe"]）。

**设计要点**：
- 三类 kind 都是"可 invoke 的能力"——保持 Capability 协议语义纯净
- `invoke` 返回事件流，tool stdout / 子 agent 中间结果都通过 event 流式上行
- `cancel` 必须支持——和 control plane 联动
- 鉴权由 core 的 PolicyEngine（`Authorizer`）负责，provider 不做鉴权决策

**典型实现**：
- `BuiltinToolsProvider`：bash / http / file 等
- `MCPCapabilityProvider`：对接外部 MCP server
- `SkillScriptProvider`：基于 SKILL.md 的脚本化技能
- `SubAgentProvider`：把派生子 agent 包装成 capability

### 4.5 ProviderRegistry

```python
class ProviderRegistry:
    def register_knowledge(self, provider: KnowledgeProvider, *, priority: int = 0) -> None: ...
    def register_memory(self, provider: MemoryProvider) -> None: ...
    def register_capability(self, provider: CapabilityProvider) -> None: ...

    def get_knowledge_providers(self) -> list[KnowledgeProvider]: ...
    def get_memory(self) -> MemoryProvider: ...  # 单实例，必需
    def get_capability_providers(self) -> list[CapabilityProvider]: ...

    async def resolve_capability(
        self, capability_id: str, ctx: ProviderContext,
    ) -> Capability | None:
        """按 capability_id 跨所有 capability provider 查找，返回首个命中。
        Agent 实例化时用此方法把 capability_refs 解析为具体 Capability 对象。"""
```

实例数约定：

| Provider | 实例数 | 必需 |
|----------|-------|------|
| MemoryProvider | 单实例 | ✅ 必需（core 默认 StructuredBlackboardMemoryProvider，外部可替换为 Mem0/LightRAG 等） |
| KnowledgeProvider | 多实例 | ❌ 可选 |
| CapabilityProvider | 多实例 | ✅ 必需（至少 ControlCapabilityProvider） |

**为什么 MemoryProvider 单实例**：core 通过 ingest 向 provider 喂送所有事件，单实例保证完整事件流不分裂；多实例会引入"哪个 provider 是权威"的歧义。如果用户想组合多种 memory 后端（如 Mem0 + 自建 RAG），应在 provider 外层做组合，向 core 暴露单一接口。

**为什么 Knowledge / Capability 允许多实例**：自然需求——多个 RAG 源、多个 MCP server 等。

---

### 4.6 AgentTemplate / Identity / Agent 实例化 / Per-Step 解析

本节把 **"loop 启动到 prompt 装配"** 的完整流转明确化。它是 §4 三协议的运行时主线，回答：

- AgentTemplate 长什么样？由谁管？core 怎么读？
- Agent 实例化时刻发生了什么？identity 和 capability 各自怎么从 template 落到具体 agent 上？
- 每个 step 装配 prompt 时是怎么用上这些已绑定 capability 与 identity 的？

#### 4.6.1 Identity 与 Capability 的并列关系

ctx-weft v0.5 起，**Identity 与 Capability 是 system prompt 的两个并列贡献者，不再嵌套**：

| 概念 | 本质 | 在 system prompt 中 | 协议归属 |
|------|------|------------------|---------|
| **Identity** | 声明式内容（"我是谁"） | identity 段（人格 / 角色） | AgentTemplate 内禀字段（无独立 protocol） |
| **Capability** | 可调用动作（"我能做什么"） | capabilities 段 + LLM tools | CapabilityProvider 协议 |

**为什么不把 identity 也做成 Capability**：
- Capability 协议核心语义是 invoke / cancel。Identity 无 invoke 语义，强行包装会引入 noop 占位
- Capability dataclass 不需要 tagged union 字段（如 `Optional[IdentityFacet]`）——保持 schema 纯净
- 三协议（K/M/C）都是"对接外部运行时系统"的接口，Identity 是 template 自身的属性，**不需要 protocol**

**对称性**：Knowledge / Memory 协议也不包含 identity 概念。Identity 与三协议在抽象层级上**平级**——它是 template 提供的"agent 自我描述"，不是外部系统对接面。

#### 4.6.2 三层时间尺度

| 时间尺度 | 关注的事 | 谁管 |
|---------|---------|------|
| **设计期** | Template 长什么样（identity + 引用哪些 capability + 配置） | host（template registration） |
| **实例化期**（session/spawn 时一次） | 给当前 agent 绑定 capability 快照；identity 在 template 中无需 resolve | core（startup） |
| **Per-step 期**（每个 ReasonStep / ObserveStep） | 从 template 读 identity facet；从快照按 purpose 过滤 capability | core（IdentitySource / CapabilitySource） |

**关键约束**：实例化时把所有 capability resolve 成快照，运行期不再查 provider。Identity 不需要快照——它在 template 里，TemplateResolver 自带缓存即可。这避免了：
- 每轮 reason 重查 provider 的开销
- Provider 运行时变化导致 prompt 不稳定（如 MCP server 暴露/隐藏 tool）
- Template 版本升级影响运行中 session（version pin）

#### 4.6.3 AgentTemplate 数据结构

```python
@dataclass
class AgentTemplate:
    """Agent 实例化的蓝图。由 host 在 template registration 阶段构建并管理。
    core 通过 TemplateResolver 协议读取。"""

    id: str
    name: str
    version: str                              # semver；既是 template 版本也是 identity 版本（用于 prompt 缓存 key 与实例化 pin）

    identity: dict[Purpose, IdentityFacet]    # 一等字段，host 从 SOUL.md/ROLE.md 解析得到
                                              # key=purpose，value=该 purpose 的身份呈现
                                              # 例：identity["act"]→SOUL，identity["observe"]→ROLE
                                              # 缺失某 purpose 时 IdentitySource 回退到 identity["act"]
    capability_refs: list[CapabilityRef]      # 外部能力引用：tool / skill / sub-agent

    memory_config: MemoryConfig
    loop_config: LoopConfig
    metadata: dict = field(default_factory=dict)


@dataclass
class IdentityFacet:
    """Agent 在某个 purpose 下的身份呈现。"""
    text: str                                  # 该 purpose 下的身份文本（SOUL 或 ROLE 内容）
    style: Optional[str] = None                # 该 purpose 的输出风格偏好（可选）


@dataclass
class CapabilityRef:
    """Template 中对外部 capability 的声明引用。"""
    capability_id: str                        # 匹配某 provider 返回的 Capability.id
    enabled: bool = True
    # purposes 不在 ref 中——以 Capability 自身声明为准（v0.3 决议）


@dataclass
class AgentTemplateSummary:
    """用于发现/列表场景（如 sub-agent 选择）。"""
    id: str
    name: str
    version: str
    description: str


@dataclass
class MemoryConfig:
    short_window_size: int = 20
    summary_threshold: int = 20
    use_long_term: bool = True
    subscribed_blackboard_topics: list[str] = field(default_factory=list)
                                              # 订阅哪些 blackboard topic 作为长期记忆


@dataclass
class LoopConfig:
    """模板级配置。完整字段见 §6.8.2（含 compact 阈值等）。"""
    max_turns_per_act: int = 10
    max_turns_per_agent: int = 20
    timeout_per_step_sec: int = 120
    failure_threshold: int = 3
    max_spawn_depth: int = 4
    compact_token_ratio: float = 0.8
    compact_message_delta: int = 20
    compact_keep_last: int = 6
```

**设计决定**：
- **Identity 在 template 中 inline**：identity 是 template 本质属性，host 把 SOUL/ROLE markdown 解析后**直接放进 template.identity**。运行期不再合成为 Capability——保持 Capability schema 干净
- **capability_refs 只引用外部能力**：工具、技能、可派生子 agent 这类"非 template 内禀"的能力

#### 4.6.4 TemplateResolver 协议

core 不知道 template 怎么存、怎么从 markdown 解析——这是 host 的事。core 只通过协议读取：

```python
class TemplateResolver(Protocol):
    """core 与 host 之间关于 template 的唯一接口。host 实现。"""

    async def get(
        self, template_id: str, version: str | None, ctx: ProviderContext,
    ) -> AgentTemplate:
        """获取 template 定义。version=None 取最新；指定 version 用于实例化时 pin。"""

    async def list_summaries(
        self, ctx: ProviderContext,
    ) -> list[AgentTemplateSummary]:
        """列出可用 template 摘要——用于 sub-agent 发现、admin 界面等。"""
```

TemplateResolver 在 CtxWeftRuntime 构造时由 host 注入（同 ProviderRegistry 一样的 DI 模式）。

#### 4.6.5 Agent 实例化时序

发生在两个时机：session 启动（root agent）和 spawn 子 agent。

```
core.LifecycleManager.instantiate_agent(template_id, parent_id=None):

  1. template = TemplateResolver.get(template_id, version=None, ctx)
     # 拿到 template，记录 template.version 用于后续 pin
     # 注：template.identity 不需要 resolve——它已经在 template 内，
     #     运行期由 IdentitySource 直接读 template.identity[purpose]

  2. # 步骤 A：逐个 resolve 外部 capability ref
     bound: list[Capability] = []
     for ref in template.capability_refs:
         if not ref.enabled:
             continue
         cap = await providers.resolve_capability(ref.capability_id, ctx)
         if cap is None:
             raise UnknownCapability(ref.capability_id, template_id=template.id)
         bound.append(cap)

  3. # 步骤 B：创建 Agent 实例
     agent = Agent(
         id = generate_id("agt"),
         session_id = ctx.session_id,
         template_id = template.id,
         template_version = template.version,   # pin 住版本
         parent_agent_id = parent_id,
         spawn_depth = (parent.spawn_depth + 1) if parent_id else 0,
         status = "IDLE",
         bound_capability_ids = [c.id for c in bound],
         memory_config = template.memory_config,
         loop_config = template.loop_config,
     )

  4. # 步骤 C：把绑定快照放进 CapabilityCache（per-session 生命周期）
     capability_cache.put(agent.id, bound)

  5. # 步骤 D：apply Event AgentInstantiated
     await apply_event(AgentInstantiated(
         agent_id=agent.id,
         template_id=template.id,
         template_version=template.version,
         bound_capability_ids=agent.bound_capability_ids,
     ))

  6. return agent
```

**关键点**：
- 实例化**只 resolve capability，不处理 identity**——identity 在 template 里，按需读
- bound 是**完整 Capability 对象列表**，不是 id——避免后续每次用还要再 resolve
- CapabilityCache 是 core 内部的 in-memory cache，scope 跟 session 生命周期一致，session 结束清理
- `template_version` 字段持久化到 Agent 表，保证即使 host 升级 template，该 agent 用的还是实例化时的版本
- Identity 跟随 template_version pin——`TemplateResolver.get(template_id, template_version)` 总能拿到该 agent 当初实例化时的 identity

#### 4.6.6 Per-Step Identity 与 Capability 解析时序

每个 ReasonStep / ObserveStep / CompactStep 调用 ContextAssembler 时，**两个独立 Source** 各自工作，互不感知：

##### IdentitySource

从 template 读 identity，按 purpose 选 facet：

```
IdentitySource.fetch(request: ContextRequest, deps):

  1. template = await template_resolver.get(
         request.agent.template_id,
         request.agent.template_version,   # pin
         ctx,
     )
     # TemplateResolver 自带 (template_id, version) 级别缓存

  2. # 按 purpose 选 facet，缺失则 fallback 到 act
     facet = (template.identity.get(request.purpose)
              or template.identity.get("act"))
     if facet is None:
         return   # 无 identity 定义，跳过

  3. yield ContextBlock(
         id = f"identity:{template.id}@{template.version}:{request.purpose}",
         source = "identity",
         kind = "identity",
         target = "system",
         content = facet.text,
         priority = 0,                     # 最高，几乎不可裁
         token_estimate = estimate_tokens(facet.text),
         metadata = {
             "template_id": template.id,
             "template_version": template.version,
             "facet_purpose": request.purpose,
             "style": facet.style,
         },
     )
```

##### CapabilitySource

从 CapabilityCache 读，按 purpose 过滤 + Authorizer 过滤：

```
CapabilitySource.fetch(request: ContextRequest, deps):

  1. bound = capability_cache.get(request.agent.id)
     # 快照命中（实例化时已填充）；未命中视为内部 bug 抛异常

  2. # 按 purpose 过滤（基于 provider 在 Capability.purposes 中的声明）
     visible = [c for c in bound if request.purpose in c.purposes]

  3. # Authorizer 过滤（agent 运行时禁用清单 / 临时降权）
     authorized = await authorizer.filter(visible, request.agent, request.task, ctx)

  4. # 产出 ContextBlock —— 注意：只产出 kind="capabilities"，无 identity 分支
     for cap in authorized:
         yield ContextBlock(
             id = f"capability:{cap.id}",
             source = "capability",
             kind = "capabilities",
             target = "system",
             content = render_capability_for_llm(cap),
             priority = 1,
             token_estimate = ...,
             metadata = {
                 "capability_id": cap.id,
                 "kind": cap.kind,           # tool | skill | agent
                 "input_schema": cap.input_schema,
             },
         )
```

##### 收益对比

| 维度 | v0.4（identity 是 capability） | v0.5（identity 独立） |
|------|---------------------------|---------------------|
| Capability dataclass | 含 identity 相关 tagged union 字段 | 纯净，只描述可 invoke 动作 |
| purposes 语义 | 一字段二义（invoke 可见 / facet 选择） | 单义：invoke 可见 |
| Source 职责 | CapabilitySource 内 if cap.kind == "identity" 分支 | 两个 Source 各司其职，无 kind 分支 |
| 实例化时序 | 5 步含 synthesize 假 capability | 4 步无 synthesize |
| 概念匹配度 | identity 被迫"行动化" | identity 保持"声明化"本质 |

**为什么 capability 还是 render 成 block 而不是直接传 LLM tools 数组**：让 BudgetStrategy 有机会在 token 不足时**裁掉低优先级 capability**——统一作为 block 才能参与裁剪。Composer 最终拼装时会把 kind="capabilities" 的 block 转换回 LLM API 的 tools 数组格式。

#### 4.6.7 完整端到端流程

把 §4.6.5 + §4.6.6 + §5 + §6 串起来——一次完整的"用户提问到 LLM 调用"流程：

```
[Session 启动]
User → POST /sessions {template_id, prompt}
Host → core.start_session(template_id, prompt, ctx)
     core:
       1. TemplateResolver.get(template_id) → AgentTemplate
       2. LifecycleManager.instantiate_agent(template_id)
          → bound capabilities resolved → CapabilityCache.put
          → Agent 持久化（含 bound_capability_ids）
       3. TaskManager.init_session + push initial task
       4. LoopEngine.run(session_id) 异步启动
       → 返回 RunHandle

[LoopEngine 驱动 ReasonStep]
ReasonStep.execute(state, ctx):
  1. 构造 ContextRequest(purpose="act", agent=..., task=..., session=...)
  2. ContextAssembler.assemble(request):
     ├─ IdentitySource.fetch:
     │    TemplateResolver.get(agent.template_id, agent.template_version)
     │    facet = template.identity["act"]  即 SOUL
     │    → ContextBlock(kind=identity, target=system, priority=0)
     ├─ CapabilitySource.fetch:
     │    bound = CapabilityCache.get(agent.id)
     │    visible = [c for c in bound if "act" in c.purposes]
     │    → ContextBlock(kind=capabilities, target=system, priority=1) × N
     ├─ RecentMemorySource.fetch → ContextBlock(kind=history, target=messages)
     ├─ BlackboardSource.fetch → ContextBlock(kind=blackboard, target=messages)
     ├─ KnowledgeRetrievalSource.fetch → ContextBlock(kind=reference, target=messages)
     ├─ TaskSpecSource.fetch → ContextBlock(kind=task_spec, target=messages)
     ↓
     all_blocks → BudgetStrategy.apply → kept_blocks
     ↓
     Composer.compose(kept_blocks):
       system_prompt = render(blocks where target=system, sorted by priority/kind)
       messages = render(blocks where target=messages, sorted)
       tools = extract_llm_tools(blocks where kind=capabilities)
     ↓
     AssembledPrompt(system, messages, tools)
  3. StepOutcome(next_step="act", state_patch={prompt: ...}, events=[ReasonCompleted])

[LoopEngine 驱动 ActStep]
ActStep.execute(state, ctx):
  1. prompt = state.prompt
  2. for turn in range(loop_config.max_turns_per_act):
       3. async for chunk in LLMClient.complete(prompt, stream=True):
            emit(LLMTokenStreamed, ...)
       4. if response has tool_calls:
            for call in tool_calls:
                cap = capability_cache.get_by_name(agent.id, call.name)
                # control tools 在此分流：submit_plan / submit_task 等触发 SUSPENDED
                async for ev in cap.provider.invoke(cap.id, call.args, ctx):
                    emit(CapabilityProgress / Finished, ...)
                append tool_result to prompt
       5. else: break
  3. StepOutcome(next_step="observe", state_patch={transcript: ...})

[LoopEngine 驱动 ObserveStep]
ObserveStep.execute(state, ctx):
  1. 构造 ContextRequest(purpose="observe", ...)
  2. ContextAssembler.assemble:
     IdentitySource → facets["observe"] 即 ROLE
     CapabilitySource → 只含 purposes 有 "observe" 的 capability（submit_task_assessment 等）
     ActorTranscriptSource → 上一步 Actor 的 transcript
     ...
  3. LLM call → task verdict
  4. StepOutcome(next_step="finalize", ...)
```

#### 4.6.8 Capability 在 LLM tool_call 路径上的对应

LLM 看到的 tools 数组里每个 tool 名（`call.tool_name`）需要能反向找到原始 Capability 对象，以便 invoke。约定：

- `Capability.id` 是全局唯一标识（含 provider 命名空间，如 `mcp:filesystem:read_file`）
- LLM tools 数组里用 `name` 字段对应 capability name（去掉命名空间，更友好），但 metadata 里带上完整 `capability_id` 用于回查
- ActStep 收到 tool_call 后：`cap = capability_cache.get_by_name(agent_id, call.name)` 拿到 Capability，再用 `cap.provider_name` 找 provider invoke
- 重名冲突：CapabilityCache 在 put 时检测同名，重名抛 `DuplicateCapabilityName` 实例化失败（强制 template 维护者 disambiguate）

### 4.7 V1 参考 Capability Provider 实现

§4.4 定义了 CapabilityProvider 协议。本节给出 V1 必须交付的几个参考实现的详细设计——它们覆盖了绝大多数实际接入需求。

#### 4.7.1 V1 Provider 一览

| Provider | 范围 | invoke 行为 | 典型实例数 | 注册方 |
|----------|------|-----------|----------|-------|
| `ControlCapabilityProvider` | 内置控制能力（submit_plan / submit_task / submit_task_assessment / replan / request_human_input） | 直接操作 task/session 状态，通过 metadata.task_suspended 等字段与 ActStep 通信 | 1（core 内部） | CtxWeftRuntime 自动 |
| `BuiltinToolsCapabilityProvider` | 进程内 Python 函数（bash_exec / http_request / read_file / write_file / glob / ...） | 直接执行 Python 函数 | 1 | host 配置 |
| `MCPCapabilityProvider` | 一个 MCP server 暴露的所有 tool | 通过 MCP 协议远程调用 | 每接入一个 MCP server 一个实例 | host 配置 |
| `LocalSkillCapabilityProvider` | 本地目录中的 SKILL.md | 返回 skill 指令文本作为 result（让 LLM 在下一轮按指令执行） | 通常 1 | host 配置 |
| `RemoteSkillCapabilityProvider` | 远程 skill 源（Git/HTTP）+ 本地缓存 | 同 Local，但 SKILL.md 来自远程同步 | 每个远程源一个实例 | host 配置 |

ControlCapabilityProvider 已在 §6.7.7 详述。下面展开 MCP / Skill / Remote Skill 三类（用户场景最常见）。

#### 4.7.2 MCPCapabilityProvider

**定位**：把一个外部 MCP server 暴露的所有 tool 映射为一组 Capability 对象，桥接 MCP 协议与 ctx-weft 的 invoke 模型。

##### 构造与生命周期

```python
@dataclass
class MCPServerConfig:
    name: str                          # provider 名 / capability 命名空间
    transport: Literal["stdio", "http", "streamable_http"]
    command: Optional[list[str]] = None    # stdio: 启动命令
    url: Optional[str] = None              # http: server URL
    headers: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    default_purposes: list[Purpose] = field(default_factory=lambda: ["act"])
    capability_purpose_override: dict[str, list[Purpose]] = field(default_factory=dict)
                                       # 个别 tool 的 purpose 覆写（如某个评估类 tool 给 observe）
    timeout_per_call_sec: int = 60
    max_concurrent_invocations: int = 4


class MCPCapabilityProvider(CapabilityProvider):
    def __init__(self, config: MCPServerConfig, mcp_client_factory):
        self._cfg = config
        self.name = f"mcp:{config.name}"
        self._client = None              # 懒连接
        self._capabilities_cache: Optional[list[Capability]] = None
        self._invocations: dict[str, MCPInvocationHandle] = {}  # invocation_id → handle，用于 cancel
```

**生命周期**：
1. **构造时**：仅记录配置，不立即连接（避免启动期阻塞）
2. **首次 list() / invoke()** → 触发 `_ensure_connected()`：建立 MCP 连接，调 `initialize` + `tools/list`，缓存 capability 列表
3. **运行期**：复用连接；按 invoke 频率管理并发槽
4. **失联**：连接断开 → 标记 `_client = None`；下次操作触发重连；同时 emit `MCPServerDisconnected` 事件
5. **provider 销毁** → 调 MCP `shutdown` + 关连接

##### list() 实现

```python
async def list(self, ctx) -> list[Capability]:
    if self._capabilities_cache is not None:
        return self._capabilities_cache
    await self._ensure_connected()
    mcp_tools = await self._client.call("tools/list")
    capabilities = []
    for t in mcp_tools["tools"]:
        cap_id = f"mcp:{self._cfg.name}:{t['name']}"
        purposes = self._cfg.capability_purpose_override.get(t["name"], self._cfg.default_purposes)
        capabilities.append(Capability(
            id=cap_id,
            name=t["name"],                  # LLM tool_call.name 用这个
            kind="tool",
            purposes=purposes,
            description=t.get("description", ""),
            input_schema=t.get("inputSchema", {}),
            side_effects=self._infer_side_effects(t),    # 启发式：写类工具 / 命令类工具
            tags=["mcp", f"mcp:{self._cfg.name}"],
        ))
    self._capabilities_cache = capabilities
    return capabilities
```

##### invoke() 实现（流式映射）

```python
async def invoke(self, capability_id, arguments, ctx):
    await self._ensure_connected()
    tool_name = capability_id.rsplit(":", 1)[-1]
    invocation_id = ctx.invocation_id            # 上层（ActStep）分配
    handle = self._client.call_streaming("tools/call", {
        "name": tool_name, "arguments": arguments,
        "_meta": {"progressToken": invocation_id},
    })
    self._invocations[invocation_id] = handle
    try:
        async for message in handle.stream():
            if message.kind == "progress":
                # MCP notifications/progress
                yield CapabilityEvent(kind="progress", payload={
                    "progress": message.params["progress"],
                    "total": message.params.get("total"),
                    "text": message.params.get("message", ""),
                })
            elif message.kind == "log":
                # MCP notifications/message (logging)
                yield CapabilityEvent(kind="stdout", payload={"data": message.params["data"]})
            elif message.kind == "result":
                yield CapabilityEvent(kind="result", payload={
                    "content": _render_mcp_content(message.params["content"]),
                    "metadata": {"isError": message.params.get("isError", False)},
                })
            elif message.kind == "error":
                yield CapabilityEvent(kind="error", payload={
                    "code": str(message.error["code"]),
                    "message": message.error["message"],
                })
    finally:
        self._invocations.pop(invocation_id, None)


async def cancel(self, invocation_id, ctx):
    handle = self._invocations.get(invocation_id)
    if handle:
        # MCP notifications/cancelled
        await self._client.notify("notifications/cancelled", {"requestId": invocation_id})
        handle.close()
```

##### 错误与重连策略

| 场景 | 行为 |
|------|------|
| 连接首次建立失败 | provider 标记 `_connection_error`，list() 返回 `[]`（agent 实例化时 fail-fast） |
| 运行中连接断开 | emit `MCPServerDisconnected`，当前 invoke 抛 error event；下次操作触发重连（指数退避） |
| MCP server 返回 invalid JSON-RPC | 包装为 CapabilityEvent(kind="error", payload.code="MCP_PROTOCOL_ERROR") |
| 超时（默认 60s） | 调用 cancel notification + 抛 timeout error |
| 工具调用本身报错（isError=True） | 不抛异常，正常返回 result event 但 metadata.isError=True，让 LLM 自己处理 |

##### 命名约定

- `Capability.id`：`mcp:{server_name}:{tool_name}` —— 全局唯一
- `Capability.name`：`{tool_name}` —— LLM 看到的；同 server 内唯一
- 跨 server 同名 tool 冲突时由 CapabilityCache.put 检测（§4.6.8），强制 host 在 server 配置层 disambiguate（如 prefix）

##### Host 责任

- 配置 MCP servers（YAML/env），每个 server 一个 MCPCapabilityProvider 实例注册到 ProviderRegistry
- 健康检查（定期 ping）
- 进程级生命周期管理（host shutdown 时关连接）
- 不做：mcp tool 的鉴权决策（由 core Authorizer 负责）

#### 4.7.3 LocalSkillCapabilityProvider

**定位**：把本地目录中的 SKILL.md 文件暴露为 Capability。invoke 时返回 SKILL.md 的指令文本，让 LLM 在下一轮按指令执行——**本质是把"声明式技能"按需注入 LLM 工作上下文**。

##### SKILL.md 文件结构

```markdown
---
name: financial_analysis
version: 1.0.0
description: |
  Run standardized financial analysis: parse statements, compute key ratios,
  generate one-page summary with recommendations.
purposes: [act]                    # 可选；默认 ["act"]
side_effects: false
scripts:                           # 可选；脚本路径声明（白名单）
  - scripts/parse_statement.py
references:                        # 可选；引用文件（提示 LLM 可读的资源）
  - references/ratio_definitions.md
---

# Steps

1. Read the financial statement from `{input_path}` using `bash_exec` + `cat`.
2. Extract these data points using regex or LLM analysis:
   - Revenue, COGS, Operating Income, Net Income, Total Assets, Total Equity
3. Compute ratios using `bash_exec` to run `scripts/parse_statement.py`:
   - Gross Margin, Operating Margin, ROE, ROA, Debt-to-Equity
4. Generate a one-page summary in markdown format.
5. Submit the summary via `submit_task_assessment`.
```

**约定**：
- Frontmatter 字段在 list() 时读取（廉价）
- Body（# Steps 部分）在 invoke 时才读全文（懒加载）
- `scripts/` 目录里的脚本可被 LLM 通过 `bash_exec` 调用；`references/` 是只读参考资料
- skill 自身不需要包含可执行代码——它是"指令 + 资源引用"

##### list() 实现

```python
class LocalSkillCapabilityProvider(CapabilityProvider):
    def __init__(self, skills_dir: Path):
        self._dir = skills_dir
        self.name = "skill:local"
        self._index: dict[str, SkillEntry] = {}    # name → metadata + path

    async def list(self, ctx) -> list[Capability]:
        if not self._index:
            await self._scan_directory()
        return [
            Capability(
                id=f"skill:{name}",
                name=name,
                kind="skill",
                purposes=entry.purposes,
                description=entry.description,         # 来自 frontmatter
                input_schema=SKILL_INVOKE_SCHEMA,      # 见下方
                side_effects=entry.side_effects,
                tags=["skill", "local"],
            )
            for name, entry in self._index.items()
        ]

    async def _scan_directory(self):
        for skill_file in self._dir.rglob("SKILL.md"):
            metadata, _body_pos = parse_frontmatter(skill_file)
            self._index[metadata["name"]] = SkillEntry(
                path=skill_file,
                description=metadata["description"],
                purposes=metadata.get("purposes", ["act"]),
                side_effects=metadata.get("side_effects", False),
            )
```

**Skill 的统一 input_schema**：

```python
SKILL_INVOKE_SCHEMA = {
    "type": "object",
    "properties": {
        "context": {
            "type": "string",
            "description": "Brief context for why you're using this skill (one sentence).",
        },
        "params": {
            "type": "object",
            "description": "Skill-specific parameters as JSON object; varies by skill.",
            "additionalProperties": True,
        },
    },
    "required": ["context"],
}
```

LLM 调用 skill 时只需提供"调用上下文"和可选 params。ctx-weft 不强制每个 skill 自定义 schema——参数细节在 SKILL.md 指令文本里说明。

##### invoke() 实现：返回指令文本

```python
async def invoke(self, capability_id, arguments, ctx):
    name = capability_id.rsplit(":", 1)[-1]
    entry = self._index.get(name)
    if entry is None:
        yield CapabilityEvent(kind="error", payload={"code": "SKILL_NOT_FOUND", "message": ...})
        return

    # 懒加载完整 body
    body = entry.path.read_text(encoding="utf-8").split("---", 2)[-1].strip()

    # 渲染 params（如果 skill body 用了 jinja-like 占位符 {input_path} 等）
    rendered = self._render(body, arguments.get("params", {}))

    # 返回指令文本作为 result——LLM 在下一轮 prompt 里会看到它
    yield CapabilityEvent(kind="result", payload={
        "content": rendered,
        "metadata": {
            "skill_name": name,
            "skill_version": entry.version,
            "scripts_available": entry.scripts,
            "references_available": entry.references,
        },
    })
```

##### Skill 与 LLM 交互的完整路径

```
[ReasonStep 装配 prompt]
  CapabilitySource 列出本 agent 绑定的所有 capability，包括：
    - skill:financial_analysis  (description: "Run standardized financial analysis...")
    - bash_exec
    - read_file
    - ...
  渲染为 system prompt + LLM tools 数组

[ActStep turn 1]
  LLM 决定："我需要做财务分析，调用 financial_analysis 这个 skill"
  emit tool_call(name="financial_analysis", args={"context": "user asked to analyze Q3 results", "params": {"input_path": "/data/q3.csv"}})

[ActStep 处理 tool_call]
  invoke skill → returns SKILL.md body 渲染后的指令文本
  把指令文本作为 tool_result 拼回 prompt

[ActStep turn 2]
  LLM 看到了完整指令，按 step 1 调用 bash_exec → cat 文件 → ...
  按 step 2 提取数据 → ...
  按 step 3 调 bash_exec 执行 scripts/parse_statement.py → ...
  ...
  按 step 5 调 submit_task_assessment

[ObserveStep → FinalizeStep]
  正常路径
```

##### scripts/ 与 references/ 的安全约束

- LLM 只能通过 `bash_exec` / `read_file` 等 builtin tool 访问这些路径
- BuiltinToolsCapabilityProvider 在执行时校验：路径必须在 skill 声明的 `scripts:` 或 `references:` 白名单内
- 这避免 LLM "脱离 skill 指令"乱跑文件系统

#### 4.7.4 RemoteSkillCapabilityProvider

**定位**：把远程源（Git / HTTP / 自定义 registry）的 SKILL.md 同步到本地缓存，然后通过和 LocalSkillCapabilityProvider 相同的逻辑暴露为 Capability。

##### 配置

```python
@dataclass
class RemoteSkillSourceConfig:
    name: str                              # 远程源标识（如 "team-skills" / "community-skills"）
    kind: Literal["git", "http", "oci"]
    url: str                               # git URL / HTTP manifest URL / OCI ref
    ref: Optional[str] = None              # git: branch/tag/commit
    cache_dir: Path = Path("./cache/skills")
    sync_interval_sec: int = 3600          # 后台同步周期；0 = 不自动同步
    auth: Optional[dict] = None            # 私有源的鉴权
    allowed_skill_names: Optional[list[str]] = None    # 白名单；None = 全部允许
```

##### 实现策略：组合 + 同步

```python
class RemoteSkillCapabilityProvider(CapabilityProvider):
    def __init__(self, config: RemoteSkillSourceConfig, syncer: SkillSyncer):
        self._cfg = config
        self.name = f"skill:remote:{config.name}"
        # 内部复用一个 Local provider，指向 cache_dir
        self._local = LocalSkillCapabilityProvider(
            config.cache_dir / config.name,
        )
        self._syncer = syncer                # 注入的同步器，负责 git pull / http fetch
        self._last_sync_at: Optional[datetime] = None

    async def list(self, ctx):
        await self._maybe_sync()
        caps = await self._local.list(ctx)
        # 重命名 capability id 加上 remote 前缀，避免与 local skills 冲突
        return [self._rebrand(c) for c in caps]

    async def invoke(self, capability_id, args, ctx):
        await self._maybe_sync()                  # invoke 时也确保缓存最新
        local_id = capability_id.replace(f"skill:remote:{self._cfg.name}:", "skill:")
        async for ev in self._local.invoke(local_id, args, ctx):
            yield ev

    async def _maybe_sync(self):
        if self._cfg.sync_interval_sec == 0:
            return  # 完全手动同步模式
        now = datetime.utcnow()
        if self._last_sync_at is None or (now - self._last_sync_at).total_seconds() >= self._cfg.sync_interval_sec:
            try:
                await self._syncer.sync(self._cfg)
                self._local._index = {}           # 失效缓存，下次 list 重扫
                self._last_sync_at = now
                emit_event(RemoteSkillSyncCompleted(source=self._cfg.name, ...))
            except Exception as e:
                emit_event(RemoteSkillSyncFailed(source=self._cfg.name, error=str(e)))
                # 同步失败：继续用旧 cache，不中断业务
```

##### SkillSyncer：实际拉取逻辑

```python
class SkillSyncer(Protocol):
    async def sync(self, config: RemoteSkillSourceConfig) -> SyncResult: ...

class GitSkillSyncer:
    async def sync(self, cfg):
        # git clone / pull 到 cfg.cache_dir / cfg.name
        # 校验 SKILL.md 文件格式
        # 应用 allowed_skill_names 白名单（删除未授权的）
        ...

class HttpSkillSyncer:
    async def sync(self, cfg):
        # GET cfg.url → 拿到 manifest（JSON 列表，每条含 name/version/url）
        # 对比本地版本 → 拉取新增/更新的 SKILL.md
        ...

class OciSkillSyncer:
    async def sync(self, cfg):
        # 使用 OCI artifact 协议
        ...
```

##### 命名约定

- `Capability.id`：`skill:remote:{source_name}:{skill_name}`
- `Capability.name`：通常带前缀避免与本地冲突，如 `team:financial_analysis`
- 多个 RemoteSkillCapabilityProvider 并存：每个用不同的 source_name 隔离命名空间

##### 安全考量

- 远程 SKILL.md 可能包含恶意指令——host 应：
  - 仅订阅可信源
  - 应用 `allowed_skill_names` 白名单
  - 显式 review SKILL.md 后才放进白名单
- Scripts 必须在 sandboxed bash_exec 内运行（V1 单进程 = 用户自己负责沙箱；V2 引入 firejail / docker exec）

#### 4.7.5 三类协同的运行时图景

```
CtxWeftRuntime
  ├── ProviderRegistry
  │     ├── ControlCapabilityProvider          (core 内置)
  │     ├── BuiltinToolsCapabilityProvider     (host 配置)
  │     ├── MCPCapabilityProvider("filesystem")  ← 连接到 @modelcontextprotocol/server-filesystem
  │     ├── MCPCapabilityProvider("postgres")    ← 连接到自定义 MCP server
  │     ├── LocalSkillCapabilityProvider          ← ./skills/ 目录
  │     ├── RemoteSkillCapabilityProvider("team")   ← git@internal/team-skills.git
  │     └── RemoteSkillCapabilityProvider("community") ← https://skill-registry.example.com
  └── TemplateResolver  (host)
```

Agent template 通过 `capability_refs` 引用具体 capability：

```yaml
capability_refs:
  - capability_id: "builtin:bash_exec"
  - capability_id: "mcp:filesystem:read_file"
  - capability_id: "mcp:postgres:query"
  - capability_id: "skill:financial_analysis"           # 本地 skill
  - capability_id: "skill:remote:team:cash_flow_model"  # 远程 team skill
```

实例化时（§4.6.5）：core 通过 `ProviderRegistry.resolve_capability(id)` 跨 provider 查找——每个 capability 都能精确定位到唯一一个 provider 实例。CapabilityCache 持有完整 Capability 对象快照。

#### 4.7.6 V2 扩展点

| 扩展方向 | 备注 |
|---------|------|
| MCP resources / prompts | V1 只接 MCP tools；resources（数据源）走 KnowledgeProvider，prompts 走 Identity capability 自然映射 |
| Skill 版本协商 | template 指定 `skill:financial_analysis@1.2.0`，多版本并存 |
| Skill marketplace | RemoteSkillSyncer 增加 marketplace 客户端（评分、搜索、自动更新） |
| MCP authorization 信任策略 | V1 不限；V2 引入 per-tool 白名单 + 调用频率限制 |
| Skill 沙箱化执行 | scripts/ 在隔离环境（firejail / docker exec）执行 |

---

## 5. Context Assembler：上下文装配流水线

### 5.1 设计思想

把 miniAgents 中 Reasoner 的"装配 ReasoningContext"职责，拆解为**纯函数式的多阶段流水线**：

```
ContextRequest ─► [Sources 并行调度] ─► ContextBlock[] ─► [BudgetStrategy] ─► [Composer] ─► AssembledPrompt
```

每个阶段：
- 接受明确输入，产生明确输出
- 无状态、纯函数（依赖只通过参数注入）
- 可独立测试、可独立替换

### 5.2 关键抽象

```python
@dataclass
class ContextRequest:
    """装配请求。

    purpose 标识装配的 prompt 给哪个 LLM 调用阶段消费，直接驱动 Capability 过滤与 Identity facet 选取：
      - "act"     → ReasonStep 装配给下一个 ActStep 的 LLM 用
      - "observe" → ObserveStep 装配给自己的 LLM 用
      - "compact" → CompactStep 装配给摘要 LLM 用
    """
    purpose: Purpose                                # 与 Capability.purposes 对齐
    scope: MemoryScope
    task: Task
    agent: Agent
    session: Session
    extra: dict = field(default_factory=dict)


@dataclass
class ContextBlock:
    """装配阶段的中间块。每个 Source 产出 0..N 个 block。"""
    id: str
    source: str                   # 来源标识（"capability:identity" / "memory:blackboard:bg" 等）
    kind: Literal[
        # → system prompt
        "identity", "capabilities", "directive",
        # → messages
        "history", "blackboard", "summary", "reference", "transcript", "task_spec",
    ]
    target: Literal["system", "messages"]  # 由 Source 决定，Composer 据此分发
    content: str | list[ContentPart]
    priority: int                 # 0 最高，budget 不够时高 priority 优先保留
    token_estimate: int
    metadata: dict = field(default_factory=dict)


class ContextSource(Protocol):
    """装配源——把 provider 数据转换为 ContextBlock。"""

    async def fetch(
        self,
        request: ContextRequest,
        providers: ProviderRegistry,
    ) -> AsyncIterator[ContextBlock]: ...


class BudgetStrategy(Protocol):
    async def apply(
        self,
        blocks: list[ContextBlock],
        token_limit: int,
        request: ContextRequest,
    ) -> list[ContextBlock]: ...


class Composer(Protocol):
    """把 blocks 渲染成最终 prompt（system + messages）。"""

    async def compose(
        self,
        blocks: list[ContextBlock],
        request: ContextRequest,
    ) -> AssembledPrompt: ...
```

### 5.3 V1 内置 Source 集合

| Source | 产出 block kind | 数据来源 | 落位 |
|--------|----------------|---------|------|
| `IdentitySource` | identity | TemplateResolver.get → template.identity[purpose] | **system** |
| `CapabilitySource` | capabilities | CapabilityCache.get(agent_id) 按 purpose 过滤（tool/skill/agent） | **system** |
| `RecentMemorySource` | history | MemoryProvider.recall_recent（types=[USER_PROMPT, OBSERVER_SUMMARY, COMPACT_SUMMARY]） | messages |
| `BlackboardSource` | blackboard | MemoryProvider.recall_topic（含 session 订阅的长期 topic + 当前 task 的子 task topic） | messages |
| `SemanticRecallSource` | recall | MemoryProvider.recall_semantic（provider 不支持时返空） | messages |
| `KnowledgeRetrievalSource` | reference | KnowledgeProvider.retrieve（RAG/Wiki） | messages（user 引用块） |
| `TaskSpecSource` | task_spec | 当前 task 的 title/description/prompt | messages（最后 user 消息） |
| `ActorTranscriptSource` | transcript | 上一步 Actor 的执行 transcript（仅 observe 用） | messages |

V1 限定**Reason / Observe / Compact 三个 Step 用不同的 Source 组合 + 不同的 purpose**：

```python
# ReasonStep 调用：purpose="act"（装配给下一个 ActStep 的 LLM）
REASON_SOURCES_AT_PURPOSE_ACT = [
    Identity,                # → system prompt（facets["act"] 即 SOUL）
    Capability,              # → system prompt（tool/skill/agent 中 purposes 含 act 的）
    RecentMemory,            # → messages（recall_recent: user_prompt + observer_summary + compact_summary）
    Blackboard,              # → messages（recall_topic：父子任务结果 + 订阅的长期 topic）
    SemanticRecall,          # → messages（recall_semantic：仅当 provider 支持）
    KnowledgeRetrieval,      # → messages（reference：RAG）
    TaskSpec,                # → messages（current ask）
]

# ObserveStep 调用：purpose="observe"
OBSERVE_SOURCES_AT_PURPOSE_OBSERVE = [
    Identity,                # → system prompt（facets["observe"] 即 ROLE）
    Capability,              # → system prompt（capability 中 purposes 含 observe 的）
    TaskSpec,                # → messages
    ActorTranscript,         # → messages（上一步 Actor 的执行 transcript）
]

# CompactStep 调用：purpose="compact"
COMPACT_SOURCES_AT_PURPOSE_COMPACT = [
    Identity,                # → system prompt（facets["compact"] 或 fallback 到 act facet）
    Capability,              # → system prompt（只读类 capability）
    ShortMemoryFull,         # → messages（待压缩的完整历史）
]
```

**关键**：所有 Source 看到的都是同一个 `ContextRequest.purpose`，Source 不需要硬编码 act/observe 逻辑——通过 purpose 字段统一调度。这样新增 purpose 不需要重写 Source。

### 5.4 Budget Strategy

V1 内置一种 `PriorityBudgetStrategy`：

1. 按 priority 升序保留 block（priority 0 永远保留）
2. 同 priority 内按 token_estimate 降序裁剪
3. 裁剪 history 类 block 时优先压缩成 summary（调用 MemoryProvider.consolidate）
4. 实在不够 → 抛 `ContextOverflowError`，由 Control Plane 接收

可替换为：用户提供的 `BudgetStrategy` 实现（例如基于 cost、基于召回率训练的策略）。

### 5.5 Composer

V1 内置 `DefaultComposer`，按 block.target 分发组装：

```
System Prompt:
  [identity blocks]                  ← Capability:identity
  ---
  ## Available Capabilities
  [capabilities blocks]              ← Capability:tool/skill/agent
  ---
  [directive blocks]                 ← 可选

Messages:
  # 早期：长期记忆 / 项目背景（由订阅的 blackboard topic 提供）
  [blackboard blocks marked long_term_background]  ← Memory:blackboard
  ---
  # 中期：历史对话
  [history blocks, sorted by timestamp]            ← Memory:short
  ---
  # 后期：本轮检索得到的参考资料 + 子任务结果
  [reference blocks]                                ← Knowledge:retrieve
  [blackboard blocks marked parent_child]           ← Memory:blackboard (子任务结果)
  [summary blocks]                                  ← Memory:long retrieval
  ---
  # 最终 user 消息：当前任务请求
  [task_spec block as last user message]            ← TaskSpec
```

**关键规则**：
- `target="system"` 的 block 永远只在 system prompt 段，不进 messages
- `target="messages"` 的 block 按 priority + kind 分组按时序拼接
- Knowledge 检索的 reference block 以 user 角色引用块形式注入（带 citation），明确告诉 LLM "这是刚查到的资料"
- 长期记忆类 blackboard（intent=long_term_background）注入在 messages 最早段，模拟"先验认知"位置；父子任务结果类 blackboard（intent=parent_child）注入在 messages 后段，紧邻当前任务

**Identity 双 purpose 渲染**（参考 §4.6）：IdentitySource 产出 identity block 时，根据 `ContextRequest.purpose` 从 `template.identity` 字典选取对应 IdentityFacet。同一个 agent 在 act / observe 两个 purpose 看到的 identity 内容不同：

```
purpose="act"（ReasonStep 装配 → 给 Actor LLM）：
  System Prompt:
    [identity from facets["act"]]    ← SOUL（人格 + 行为风格）
    [capabilities where "act" in purposes]
    ...

purpose="observe"（ObserveStep 装配 → 给 Observer LLM）：
  System Prompt:
    [identity from facets["observe"]]  ← ROLE（职责 + 评估准则）
    [capabilities where "observe" in purposes]  ← submit_task_assessment / request_human_input
    ...
```

这等价于 miniAgents 的双阶段 system prompt 行为，但实现路径统一为协议化的 purpose 过滤 + facet 选取。

可替换：用户自定义 Composer 实现完全不同的 prompt 排版（例如 XML 风格、JSON 风格）。

### 5.6 Assembler 主流程

```python
class ContextAssembler:
    def __init__(
        self,
        sources: list[ContextSource],
        budget: BudgetStrategy,
        composer: Composer,
        providers: ProviderRegistry,
    ) -> None: ...

    async def assemble(self, request: ContextRequest) -> AssembledPrompt:
        # 1. 并发触发所有 sources
        all_blocks = []
        async with asyncio.TaskGroup() as tg:
            futs = [tg.create_task(self._collect(s, request)) for s in self.sources]
        for f in futs:
            all_blocks.extend(f.result())

        # 2. budget
        token_limit = request.session.context_limit
        kept = await self.budget.apply(all_blocks, token_limit, request)

        # 3. compose
        prompt = await self.composer.compose(kept, request)

        # 4. 发事件
        emit(ContextAssembled(request_id=..., blocks=[b.id for b in kept], tokens=prompt.tokens))
        return prompt
```

---

## 6. Step 化 Loop Engine

### 6.1 为什么 Step 化

miniAgents 的 AgentLoop.run 是一个**不可中断的同步过程**——一旦进入循环，要么跑完要么抛异常，没法在中间 pause/inspect/resume。这正是"运行时控制弱"的根源。

ctx-weft 把 loop 拆成 **Step 序列**：每个 Step 是一个可单独执行、单独观察的最小单元。Driver 决定下一步执行哪个 Step。

### 6.2 Step 抽象

```python
class Step(Protocol):
    name: str

    async def execute(
        self,
        state: LoopState,
        ctx: LoopContext,
    ) -> StepOutcome:
        """执行此 step，产生 outcome。所有 IO 通过 ctx.providers / ctx.llm 委托。"""


@dataclass
class StepOutcome:
    """每步执行的统一返回。"""
    next_step: Optional[str]     # 下一步 step name，None 表示 loop 结束
    state_patch: dict            # 对 LoopState 的增量更新
    events: list[Event]          # 本步产生的事件
    request_pause: bool = False  # 主动请求暂停（如等待 HITL）
```

### 6.3 V1 内置 Step

| Step | 职责 | 详见 |
|------|------|------|
| `ReasonStep` | 调用 ContextAssembler 装配 prompt；入口处做 token 估算与 compact 触发判断 | §6.8.4 |
| `CompactStep` | 触发记忆压缩；自带 purpose="compact" 装配 + LLM 调用 + 重写消息 + 重置 loop_guard | §6.8.5 |
| `ActStep` | LLM 推理 + capability 调用（多 turn 内嵌）；含 control 行为路由 | §6.7 |
| `ObserveStep` | 评估 actor 结果，决定 task outcome | — |
| `FinalizeStep` | 写 memory、publish blackboard、清理 | — |
| `SuspendStep` | task 进入 SUSPENDED，写入挂起摘要 | — |
| `HitlWaitStep` | 等待人工审批（也可通过 pause_token 内嵌于 ActStep，见 §6.7.6） | — |

**无 PlanStep**：规划行为（submit_plan）发生在 ActStep 内部，是 actor 调用 control capability 的结果——它产出多个子 task 并把当前 task 转 SUSPENDED。整个过程在 ActStep 框架内即可完成，没有独立的 Plan 阶段。

`ActStep` 内部仍可能多轮 LLM 调用——这种"子步"通过 sub-step 机制实现，但不展开到 step 层面（保持 step 列表简洁）。

### 6.4 Step Driver

```python
class StepDriver:
    def __init__(
        self,
        steps: dict[str, Step],
        initial_step: str = "reason",
    ) -> None: ...

    async def run(
        self,
        initial_state: LoopState,
        ctx: LoopContext,
    ) -> AsyncIterator[StepOutcome]:
        """流式驱动 step 链。每完成一步 yield 一次 outcome。"""
        state = initial_state
        next_step_name = self._initial

        while next_step_name is not None:
            await ctx.cancel_token.checkpoint()  # 检查是否被 cancel
            await ctx.pause_token.checkpoint()   # 检查是否被 pause

            step = self._steps[next_step_name]
            emit(StepStarted(step=step.name))
            outcome = await step.execute(state, ctx)
            state = state.apply_patch(outcome.state_patch)
            for ev in outcome.events:
                emit(ev)
            emit(StepCompleted(step=step.name))

            yield outcome

            if outcome.request_pause:
                await ctx.pause_token.wait()  # 主动暂停

            next_step_name = outcome.next_step
```

### 6.5 LoopContext

```python
@dataclass
class LoopContext:
    """每次 loop run 一个，包装所有跨 step 的依赖与令牌。"""
    providers: ProviderRegistry
    assembler: ContextAssembler
    llm: LLMClient
    event_bus: EventBus
    cancel_token: CancelToken
    pause_token: PauseToken
    config: LoomConfig
```

### 6.6 Guard 机制

Guard 不是单独的 Step，而是**贯穿所有 Step 的预检 + 后检**：

- token_budget：每个 Step 的 `execute` 末尾检查；超限抛 `BudgetExceededError`，Driver 捕获后切到 `FailStep`
- max_turns：在 ActStep 内部子轮次计数
- concurrent_agents：LifecycleManager 在 spawn 时检查
- failure_threshold：FailedStep 后由 session-level guard 检查

### 6.7 ActStep 详细展开

ActStep 是 loop 中最复杂的 Step，承担**多轮 LLM 推理 + capability 调用 + control 副作用 + streaming 传导**。本节给出其完整内部行为。

#### 6.7.1 行为概述

ActStep 一次 `execute()` 包含一个 **turn 子循环**（最多 `loop_config.max_turns_per_act` 轮）。每轮：

1. 调用 LLM（流式），收集文本 + tool_calls
2. 如无 tool_calls → 本步正常结束，next_step="observe"
3. 如有 tool_calls → 顺序 invoke 每个 capability，把结果拼回 prompt，进入下一轮
4. 任一 capability 返回 `task_suspended=True` 元数据 → 立即退出 ActStep，next_step=None（不进 observe，task 已 SUSPENDED）

终止条件四种：normal（无 tool_call）/ suspended（control 命中）/ max_turns / 异常。

**Memory 摄取契约（v0.14 关键改动）**：ActStep 每个 LLM 调用 + 每次 capability invoke + 每次 tool 返回都**必须 ingest 到 MemoryProvider**——这是与外部 memory 系统的硬契约（§4.3.1）。Provider 自由决定如何内化：

- **core 默认 StructuredBlackboard impl**：存储所有类型事件，但 `recall_recent` 默认只返回 `USER_PROMPT / OBSERVER_SUMMARY / COMPACT_SUMMARY` 三类——actor 的 LLM_RESPONSE / TOOL_INVOCATION / TOOL_RESULT 被存档但**不进入下一轮 prompt 的 messages 段**。视觉上仍保持 miniAgents 的"每 task memory +2 条"效果。
- **外部 impl（Mem0/LightRAG/...）**：实时索引所有事件，未来可用 `recall_semantic` 召回相关 actor transcript（如"我之前怎么处理类似的 bash 错误"）。

也就是说：transcript 一定会被 ingest，但**默认情况下不直接出现在 messages 段**——它是给外部 memory 系统索引用的，由 Observer 的 verdict.summary 代表当前轮"输出"进入下一轮上下文。

ActStep 内即使发现某轮 prompt_tokens 接近 context_limit，也**不主动触发 compact**——交给后续 Observer 压缩本轮噪声，下次 task 的 ReasonStep 入口若仍超限自然 compact（§6.8.4）。

#### 6.7.2 内部状态机

```
                   ┌──────────────┐
                   │   START      │
                   └──────┬───────┘
                          ▼
              ┌────────────────────┐
        ┌────►│ WaitingForLLM      │  ◄─ checkpoint(cancel,pause)
        │     └──────┬─────────────┘
        │            ▼ (stream chunks → emit LLMTokenStreamed)
        │     ┌────────────────────┐
        │     │ LLMStreamFinished  │
        │     └──────┬─────────────┘
        │            ▼ (guard.add_tokens → check budget)
        │     ┌────────────────────┐
        │     │ AfterGuardCheck    │
        │     └──┬────────┬────────┘
        │        │        │
        │ no tc  │        │ has tc
        │        ▼        ▼
        │  ┌────────┐  ┌──────────────────────┐
        │  │ DONE   │  │ InvokingCapabilities  │  ◄─ checkpoint
        │  │next=   │  └──────┬───────────────┘
        │  │observe │         ▼ (sequential invoke + emit events)
        │  └────────┘  ┌──────────────────────┐
        │              │ AllInvocationsDone   │
        │              └──┬───────────┬───────┘
        │                 │ no susp.  │ task_suspended
        │                 ▼           ▼
        │       ┌─────────────────┐ ┌────────┐
        └───────┤ AppendResults   │ │ DONE   │
                │ (turn+=1)       │ │next=   │
                └─────────────────┘ │ None   │
                  if turn>max_turns └────────┘
                  → DONE(failed)
```

#### 6.7.3 主算法

```python
class ActStep:
    name = "act"

    async def execute(self, state: LoopState, ctx: LoopContext) -> StepOutcome:
        agent = state.agent
        prompt: AssembledPrompt = state.assembled_prompt  # 由 ReasonStep 装配
        transcript: list[TurnRecord] = []
        events: list[Event] = []
        exit_reason: str = "max_turns"

        for turn in range(1, agent.loop_config.max_turns_per_act + 1):
            await ctx.cancel_token.checkpoint()
            await ctx.pause_token.checkpoint()
            emit(events, ActTurnStarted(turn=turn))

            # 1) LLM 流式调用
            llm_text, tool_calls, usage = await self._run_llm_turn(
                prompt, ctx, events,
            )
            ctx.guard.add_tokens(usage.total_tokens)
            ctx.guard.check_budget()    # 超限抛 BudgetExceededError

            # ★ Memory ingest：LLM 响应（外部 memory 内化的关键事件）
            await ctx.providers.memory.ingest(MemoryEvent(
                type=MemoryEventType.LLM_RESPONSE,
                scope=state.scope, role="assistant",
                content=llm_text,
                metadata={"turn": turn, "tool_call_count": len(tool_calls),
                          "usage": dataclasses.asdict(usage)},
                timestamp=now_utc(),
            ), ctx.provider_ctx)

            record = TurnRecord(
                turn=turn, sent=prompt.snapshot(),
                assistant_text=llm_text, tool_calls=tool_calls,
            )

            # 2) 无 tool_call → 本步结束
            if not tool_calls:
                transcript.append(record)
                exit_reason = "normal"
                emit(events, ActTurnCompleted(turn=turn, reason=exit_reason))
                break

            # 3) 顺序 invoke 每个 capability（每次 invoke 内部 ingest TOOL_INVOCATION + TOOL_RESULT）
            results, suspended = await self._invoke_all(
                tool_calls, state, ctx, events,
            )
            record.tool_results = results
            transcript.append(record)
            emit(events, ActTurnCompleted(turn=turn, reason="tool_calls_processed"))

            if suspended:
                exit_reason = "suspended"
                break

            # 4) 把 tool_results 拼回 prompt，进入下一轮
            prompt = self._append_tool_results(prompt, tool_calls, results)
        else:
            # for-else：循环正常结束（未 break），达到 max_turns
            exit_reason = "max_turns"
            emit(events, MaxTurnsReached(agent_id=agent.id, max=agent.loop_config.max_turns_per_act))

        next_step = None if exit_reason in ("suspended", "max_turns") else "observe"
        return StepOutcome(
            next_step=next_step,
            state_patch={
                "transcript": transcript,
                "act_exit_reason": exit_reason,
            },
            events=events,
        )
```

#### 6.7.4 单 turn LLM 子调用

`_run_llm_turn` 处理一次 LLM 调用——streaming，把每个 chunk 转事件，最后聚合：

```python
async def _run_llm_turn(
    self, prompt: AssembledPrompt, ctx: LoopContext, events: list,
) -> tuple[str, list[ToolCall], LLMUsage]:
    request_id = generate_id("llmreq")
    emit(events, LLMRequestStarted(request_id=request_id, model=ctx.config.llm.model))

    accumulated_text = ""
    tool_calls: list[ToolCall] = []
    usage = LLMUsage()

    async for chunk in ctx.llm.complete(prompt, stream=True):
        # 注：默认仅响应 cancel；pause 不在 chunk 中间生效，避免半截响应
        await ctx.cancel_token.checkpoint()

        if chunk.kind == "token":
            accumulated_text += chunk.text
            emit(events, LLMTokenStreamed(request_id=request_id, delta=chunk.text))
        elif chunk.kind == "tool_call":
            tool_calls.append(chunk.tool_call)
        elif chunk.kind == "usage":
            usage = chunk.usage

    emit(events, LLMResponseFinished(
        request_id=request_id, content=accumulated_text,
        tool_call_count=len(tool_calls), usage=usage,
    ))
    return accumulated_text, tool_calls, usage
```

#### 6.7.5 Capability 调度策略

**V1 选择：顺序执行**（按 LLM 返回顺序）。理由：

- LLM 返回 `[bash_exec, submit_task]` 这种组合时，顺序语义对用户更直观
- 并发执行需要处理"半成功"状态机（部分 fail，另一部分仍跑），复杂度显著上升
- Streaming 优先的场景下，单 capability 也能流式 emit progress，并发收益有限

并发作为 V2 增强：通过 `Capability.parallel_safe: bool` 标记 + 用户配置开启。

```python
async def _invoke_all(
    self, tool_calls: list[ToolCall], state, ctx: LoopContext, events: list,
) -> tuple[list[CapabilityResult], bool]:
    """顺序 invoke。返回 (results, suspended)。"""
    bound = ctx.capability_cache.get(state.agent.id)
    by_name = {c.name: c for c in bound}
    results: list[CapabilityResult] = []
    suspended = False

    for call in tool_calls:
        cap = by_name.get(call.name)
        if cap is None:
            # LLM 编造的 capability 名——返回 error，下一轮 LLM 自己处理
            results.append(CapabilityResult.error(
                call=call, error_code="CAPABILITY_NOT_FOUND",
                content=f"No capability named '{call.name}' in your toolbelt.",
            ))
            continue

        result = await self._invoke_one(call, cap, state, ctx, events)
        results.append(result)

        # control capability 把 task 标记为 SUSPENDED
        if result.metadata.get("task_suspended"):
            suspended = True
            # 不 break，让后续 tool_calls 也得到响应（但它们的 result
            # 会被 LLM 在 resume 后处理，因为本轮 transcript 完整保留）
            # 严格策略：break 立即退出。V1 选择 break。
            break

    return results, suspended
```

#### 6.7.6 单 capability invoke（含 HITL 嵌入）

```python
async def _invoke_one(
    self, call: ToolCall, cap: Capability,
    state, ctx: LoopContext, events: list,
) -> CapabilityResult:
    invocation_id = generate_id("inv")
    emit(events, CapabilityInvoked(
        invocation_id=invocation_id, capability_id=cap.id,
        capability_kind=cap.kind, arguments=call.args,
    ))

    # ★ Memory ingest：tool 调用
    await ctx.providers.memory.ingest(MemoryEvent(
        type=MemoryEventType.TOOL_INVOCATION,
        scope=state.scope, role="assistant",
        content=f"Calling {cap.name}",
        metadata={"capability_id": cap.id, "arguments": call.args, "invocation_id": invocation_id},
        timestamp=now_utc(),
        causation_id=invocation_id,
    ), ctx.provider_ctx)

    # ── HITL 拦截 ────────────────────────────────────────────────
    if cap.side_effects and ctx.authorizer.requires_approval(cap, state.agent):
        approval_id = await ctx.hitl_manager.request(
            session_id=state.session.id, task_id=state.task.id,
            invocation_id=invocation_id, capability_id=cap.id, arguments=call.args,
        )
        emit(events, HitlRequired(approval_id=approval_id, ...))
        # 关键：通过 pause_token 让整个 ActStep 暂停，但不离开 step；
        # HitlManager 在 approve/reject 时 resume token，ActStep 续跑。
        decision = await ctx.hitl_manager.wait(approval_id)
        if decision.status != "APPROVED":
            return CapabilityResult.error(
                call=call, error_code="REJECTED_BY_HUMAN",
                content=f"User rejected: {decision.reason or ''}",
            )
        # 如果是 MODIFIED：用 decision.modified_arguments 替换
        if decision.modified_arguments is not None:
            call = call.with_args(decision.modified_arguments)

    # ── 实际 invoke ───────────────────────────────────────────────
    provider = ctx.providers.find_provider_for(cap.id)
    stdout_buf: list[str] = []
    final_metadata: dict = {}
    error: Optional[CapabilityError] = None

    try:
        async for ev in provider.invoke(cap.id, call.args, ctx.provider_ctx):
            await ctx.cancel_token.checkpoint()
            emit(events, CapabilityProgress(
                invocation_id=invocation_id, event_kind=ev.kind, payload=ev.payload,
            ))
            if ev.kind in ("stdout", "stderr"):
                stdout_buf.append(ev.payload.get("data", ""))
            elif ev.kind == "result":
                final_metadata = ev.payload.get("metadata", {})
            elif ev.kind == "error":
                error = CapabilityError(**ev.payload)
                break

    except asyncio.CancelledError:
        # cancel token fired
        await provider.cancel(invocation_id, ctx.provider_ctx)
        emit(events, CapabilityCanceled(invocation_id=invocation_id))
        raise   # 让 Driver 处理 cancel

    if error is not None:
        emit(events, CapabilityFailed(invocation_id=invocation_id, error=error))
        return CapabilityResult.error(call=call, error_code=error.code, content=error.message)

    emit(events, CapabilityFinished(
        invocation_id=invocation_id, content_length=sum(len(s) for s in stdout_buf),
    ))

    # ★ Memory ingest：tool 结果
    result_content = "".join(stdout_buf)
    await ctx.providers.memory.ingest(MemoryEvent(
        type=MemoryEventType.TOOL_RESULT,
        scope=state.scope, role="tool",
        content=result_content,
        metadata={"capability_id": cap.id, "invocation_id": invocation_id,
                  "is_error": error is not None, **final_metadata},
        timestamp=now_utc(),
        causation_id=invocation_id,
    ), ctx.provider_ctx)

    return CapabilityResult.success(
        call=call,
        content=result_content,
        metadata=final_metadata,   # 包含 task_suspended 等 control 信号
    )
```

#### 6.7.7 Control Capability 内部机制

Control capability（`submit_plan` / `submit_task` / `submit_task_assessment` / `replan` 等）通过 **专门的核心内 provider** 暴露——`ControlCapabilityProvider`。它的特殊之处：

1. **由 core 内部构造**（不是用户外部注册），构造时注入 `TaskService` / `SessionService` / `EventBus` 等核心依赖
2. **invoke 时直接操作 task/session 状态**，并通过 `CapabilityEvent(kind="result").payload["metadata"]["task_suspended"] = True` 通知 ActStep
3. **对 ActStep 完全透明**——ActStep 不感知"control capability"是特殊的，只读 metadata

```python
class ControlCapabilityProvider(CapabilityProvider):
    """Core-internal provider. 通过 metadata 与 ActStep 通信。"""
    name = "control"

    def __init__(self, task_svc, session_svc, event_bus):
        self._task = task_svc; self._session = session_svc; self._events = event_bus

    async def list(self, ctx):
        return [
            Capability(id="control:submit_plan", name="submit_plan", kind="tool",
                       purposes=["act"], description=..., input_schema=...),
            Capability(id="control:submit_task", name="submit_task", kind="tool",
                       purposes=["act"], description=..., input_schema=...),
            Capability(id="control:submit_task_assessment", name="submit_task_assessment",
                       kind="tool", purposes=["observe"], description=..., input_schema=...),
            ...
        ]

    async def invoke(self, capability_id, args, ctx):
        if capability_id == "control:submit_plan":
            # 创建子 task + 把当前 task 设为 SUSPENDED
            subtask_ids = []
            for spec in args["tasks"]:
                tid = await self._task.create_subtask(
                    parent_id=ctx.task_id, **spec,
                )
                subtask_ids.append(tid)
            await self._task.suspend(ctx.task_id, reason="spawn_plan")
            yield CapabilityEvent(
                kind="result",
                payload={
                    "content": f"Plan submitted with {len(subtask_ids)} subtasks.",
                    "metadata": {
                        "task_suspended": True,        # ← ActStep 据此退出
                        "subtask_ids": subtask_ids,
                    },
                },
            )
        elif capability_id == "control:submit_task_assessment":
            # 仅在 observe purpose 调用——写入 task verdict
            await self._task.record_assessment(ctx.task_id, args["verdict"], args["reason"])
            yield CapabilityEvent(kind="result", payload={"content": "Assessment recorded.", "metadata": {}})
        # ... 其他
```

**收益**：协议层零特殊化——control capability 与 tool/skill/agent 走同一个 `CapabilityProvider.invoke` 接口，只通过 metadata 协议字段表达副作用。第三方 provider 也可以模拟 control 行为（理论上），但实际上他们拿不到 task_svc 这种核心依赖，所以无法真正 suspend task。这天然形成了"core 内置 vs 外部"的能力差异，不需要额外的信任机制。

#### 6.7.8 Guard 检查点位置

| 位置 | 检查项 | 行为 |
|------|-------|------|
| 每个 turn 开始 | cancel_token / pause_token | cancel → 抛 CancelledError；pause → await 直到 resume |
| LLM chunk 之间 | cancel_token（不响应 pause） | 避免半截 response；cancel 时丢弃本次 LLM 输出 |
| LLM 调用结束后 | token_budget 累加 + 上限 | 超限抛 BudgetExceededError |
| Capability invoke 前 | side_effects + HITL 要求 | 需要审批时挂起 ActStep（pause_token） |
| Capability 流式事件之间 | cancel_token | 取消时调用 provider.cancel |
| 一轮 capability 全部完成后 | 失败计数 | 连续失败超阈值 → emit warning，可能触发 PAUSED_HITL |
| Turn 结束（while 检查） | turn ≥ max_turns_per_act | 达到上限 → exit_reason=max_turns |

#### 6.7.9 Cancel / Pause 传导

| 信号 | 来源 | 传导路径 | 响应粒度 |
|------|------|---------|---------|
| **cancel** | `RunHandle.cancel()` | RunHandle → ctx.cancel_token → 各 checkpoint + LLM iterator + Capability iterator + provider.cancel() | 立即（最长一个 chunk 延迟） |
| **pause** | `RunHandle.pause()` 或 HITL Manager | RunHandle → ctx.pause_token → 各 checkpoint await | turn 之间（默认） |
| **timeout** | `LoopConfig.timeout_per_step_sec` | Driver 包 `asyncio.wait_for` → 抛 TimeoutError | step 整体 |

**急停模式**（`PauseMode.IMMEDIATE`）：把 pause checkpoint 也加到 LLM chunk 之间——会牺牲 response 完整性，仅 debug 用。

#### 6.7.10 错误与重试策略

| 错误来源 | 默认行为 | 可配置 |
|---------|---------|-------|
| LLM 网络错误 | LLMClient 内置指数退避（最多 3 次） | `LLMConfig.retry_policy` |
| LLM 返回不合法 tool_call（schema fail） | 把 schema error 作为 `CapabilityResult.error` 注入下一轮 prompt | （不可关闭，是 self-healing 路径） |
| Capability invoke 失败（provider error） | 把 error 作为 tool_result，让 LLM 下一轮决定 | `AgentConfig.capability_retry_policy` |
| 同一 capability 连续失败 N 次 | emit warning，guard 计数 | `GuardConfig.same_cap_failure_threshold` |
| LLM 编造 capability 名 | error 注入下一轮，告诉 LLM 该 capability 不存在 | （固定行为） |
| BudgetExceededError | ActStep 立即终止，next_step=None，session FAILED | （固定行为） |
| CancelledError | ActStep 立即终止，provider.cancel 被调 | （固定行为） |

#### 6.7.11 与外部观察者的 Event 时序

一次 ActStep 在事件流上呈现的形态（订阅者按时序看到）：

```
ActTurnStarted(turn=1)
LLMRequestStarted(request_id=req_1)
LLMTokenStreamed(request_id=req_1, delta="To answer your question, I need to")
LLMTokenStreamed(request_id=req_1, delta=" check the file system first.")
LLMTokenStreamed(...)                              ← 流式逐 token emit
LLMResponseFinished(request_id=req_1, tool_call_count=2)
CapabilityInvoked(invocation_id=inv_1, capability_id=builtin:read_file)
CapabilityProgress(invocation_id=inv_1, event_kind=stdout, payload={...})
CapabilityFinished(invocation_id=inv_1)
CapabilityInvoked(invocation_id=inv_2, capability_id=builtin:bash_exec)
CapabilityProgress(invocation_id=inv_2, event_kind=stdout, ...)   ← 流式
CapabilityFinished(invocation_id=inv_2)
ActTurnCompleted(turn=1, reason=tool_calls_processed)
ActTurnStarted(turn=2)
LLMRequestStarted(request_id=req_2)
...
LLMResponseFinished(request_id=req_2, tool_call_count=0)
ActTurnCompleted(turn=2, reason=normal)
StepCompleted(step=act)
```

订阅者完全可以根据这些事件构建实时 UI——每个 LLMTokenStreamed 推一个增量，每个 CapabilityProgress 显示工具执行进度，每个 ActTurnCompleted 给一个分隔符。Streaming First 在这里落地。

### 6.8 Token 估算与 Compact 触发

本节解决"agent 怎么知道自己快要超出上下文窗口，以及怎么及时压缩"。

**两级压缩模型**（v0.10 厘清）：

| 级别 | 谁做 | 何时 | 输入 | 输出 |
|------|------|------|------|------|
| **第一级：per-task 压缩** | Observer | 每个 task 末尾（§6.9.1） | ActStep 的多轮 tool use transcript | 一条 verdict.summary（assistant message） |
| **第二级：累积压缩** | CompactStep | 累积超阈值时（本节） | 多个 task 累积的 summary 消息 | 一条 `[Context so far]` 摘要 + 保留最近 N 条 |

**核心设计原则**：
- ActStep 的 transcript（多轮 LLM + tool 调用）是**临时 in-memory 状态**，不写 long-term memory
- Memory 每个 task 只增长 2 条（user_prompt + verdict.summary）——增长是受控的
- Observer 已完成 per-task 压缩本职工作，Compact 只在多个 task 的 summary 累积过多时才需要

**Compact 唯一触发点：ReasonStep 入口**
- 触发时机的本质：**有新工作进来时，先腾空间**
- 其他位置（ActStep 内 / Observe / Finalize）都不触发：
  - ActStep 内超限 → 不动，让 Observer 接手压缩本轮噪声
  - Observer 是本职压缩，不需要额外 compact
  - FinalizeStep 没有新需求进来，compact 是浪费 LLM 算力

#### 6.8.1 两层机制：精确测量 + 增量估算

核心洞察：**LLM 每次返回都附带精确的 `prompt_tokens`，把它当作锚点比每轮重新 tokenize 全量历史便宜得多**。

| 层 | 谁做 | 何时做 | 精度 |
|----|------|-------|------|
| 精确测量 | ActStep 在 turn 完成后 | 每个 LLM 调用结束 | 精确（来自 LLM API usage 字段） |
| 增量估算 | ReasonStep 在装配开始前 | 每次 ReasonStep 入口 | 估算（基线 + 自基线后新增消息的 token 估计） |

闭环：
```
ActStep 测量 prompt_tokens → 写入 agent.loop_guard.context_tokens
                                          ↓
ReasonStep 读 loop_guard → 估算 = context_tokens + estimate(new_messages_since_baseline)
                                          ↓
若 estimate ≥ limit × compact_threshold → next_step="compact"
                                          ↓
CompactStep 压缩 + 写新 summary → 重置 loop_guard.context_tokens = 0
                                          ↓
回到 ReasonStep → 无基线，走粗估 → 正常装配
                                          ↓
ActStep 新一次 LLM 调用 → 测量新基线 ...
```

#### 6.8.2 LoopGuard / LoopConfig 字段

```python
@dataclass
class LoopGuard:
    """Agent 运行时计数与测量值（mutable，每轮可能更新）。"""
    turns_used: int = 0                       # 累计 LLM 调用数（跨 task）
    context_tokens: int = 0                   # 上次 LLM 调用返回的 prompt_tokens（基线）；0=无基线
    context_message_count: int = 0            # 测得基线时的消息总数
    context_limit: int = 180_000              # LLM 的硬上下文上限（实例化时从 LLMClient 拷贝）
    last_compact_at_message: int = 0          # 上次 compact 时的消息数；用于消息数触发判断


@dataclass
class LoopConfig:
    """模板级配置（immutable，跟随 template 版本）。"""
    max_turns_per_act: int = 10
    max_turns_per_agent: int = 20             # 跨所有 task 的累计上限（root=20，sub 默认 10）
    timeout_per_step_sec: int = 120
    failure_threshold: int = 3
    max_spawn_depth: int = 4
    # Compact 相关：
    compact_token_ratio: float = 0.8          # 达到 context_limit × 此比例触发
    compact_message_delta: int = 20           # 自上次 compact 后新增消息数超此值触发
    compact_keep_last: int = 6                # 压缩时保留最近 N 条原始消息
```

Agent dataclass 持有 `loop_guard: LoopGuard`（不再用 `runtime: dict`）和 `loop_config: LoopConfig`。

#### 6.8.3 ActStep 中的测量记录

在 §6.7.3 主算法的 for 循环结束后追加：

```python
# ── 测量回写：取所有 turn 中最大的 prompt_tokens 作为新基线 ──
if transcript:
    max_pt = max(record.usage.prompt_tokens for record in transcript)
    # 取 USER_PROMPT + OBSERVER_SUMMARY + COMPACT_SUMMARY 类型计数（与下轮 ReasonStep 的 recall_recent 过滤一致）
    new_msg_count = await ctx.providers.memory.count_recent(
        state.scope,
        types=[MemoryEventType.USER_PROMPT, MemoryEventType.OBSERVER_SUMMARY, MemoryEventType.COMPACT_SUMMARY],
        ctx=ctx.provider_ctx,
    )
    new_guard = dataclasses.replace(
        state.agent.loop_guard,
        context_tokens=max_pt,
        context_message_count=new_msg_count,
        turns_used=state.agent.loop_guard.turns_used + len(transcript),
    )
    state_patch["loop_guard"] = new_guard
    emit(events, ContextTokensMeasured(
        agent_id=state.agent.id,
        measured_tokens=max_pt,
        message_count=new_msg_count,
    ))
```

**为什么取 max 不是 last**：本步内可能多轮 LLM 调用，每轮 prompt 不同（拼了不同的 tool_results）。取 max 是"本步内最坏情况"的保守估计——下次 reason 拿到的窗口至少能容纳这个值。

#### 6.8.4 ReasonStep 入口的 Compact 触发（唯一触发点）

ReasonStep 是 task 的起点——也是"新工作即将进入"的唯一入口。Compact 的判断逻辑只在这里执行。

```python
class ReasonStep:
    name = "reason"

    async def execute(self, state, ctx):
        agent = state.agent
        guard = agent.loop_guard
        cfg = agent.loop_config
        events: list[Event] = []
        scope = MemoryScope(session_id=state.session.id, agent_id=agent.id)

        # 1) 估算当前 context 长度（增量或粗估）
        estimated, current_msg_count = await self._estimate_context_tokens(agent, scope, ctx)
        emit(events, ContextTokensEstimated(
            agent_id=agent.id, estimated_tokens=estimated,
            has_baseline=(guard.context_tokens > 0),
        ))

        # 2) Compact 触发判断（唯一触发点）
        needs_compact = self._should_compact(estimated, current_msg_count, guard, cfg)
        if needs_compact:
            emit(events, CompactTriggered(
                agent_id=agent.id,
                reason=needs_compact,
                estimated_tokens=estimated,
            ))
            return StepOutcome(next_step="compact", state_patch={}, events=events)

        # 3) 正常装配 prompt 给下一个 ActStep
        request = ContextRequest(purpose="act", agent=agent, task=state.task,
                                 session=state.session, scope=scope)
        prompt = await ctx.assembler.assemble(request)
        emit(events, ReasonCompleted(
            estimated_tokens=estimated, assembled_token_count=prompt.token_count,
        ))
        return StepOutcome(
            next_step="act",
            state_patch={"assembled_prompt": prompt},
            events=events,
        )

    async def _estimate_context_tokens(self, agent, scope, ctx) -> tuple[int, int]:
        """增量估算或粗估，返回 (estimated_tokens, current_message_count)。"""
        # 计数与 ActStep 写 baseline 时使用同一组 type 过滤
        msg_types = [MemoryEventType.USER_PROMPT,
                     MemoryEventType.OBSERVER_SUMMARY,
                     MemoryEventType.COMPACT_SUMMARY]
        current_msg_count = await ctx.providers.memory.count_recent(scope, types=msg_types, ctx=ctx.provider_ctx)
        guard = agent.loop_guard
        if guard.context_tokens > 0:
            # 取自 baseline 之后新增的同类事件，估算其文本 token 增量
            recent = await ctx.providers.memory.recall_recent(
                scope, types=msg_types,
                limit=current_msg_count - guard.context_message_count,
                ctx=ctx.provider_ctx,
            )
            new_text = " ".join(_content_to_text(r.content) for r in recent)
            estimated = guard.context_tokens + estimate_tokens(new_text)
        else:
            estimated = await self._coarse_estimate(agent, scope, ctx)
        return estimated, current_msg_count

    def _should_compact(self, estimated, msg_count, guard, cfg) -> Optional[str]:
        """返回触发原因字符串；不触发返回 None。"""
        if estimated >= guard.context_limit * cfg.compact_token_ratio:
            return "token_ratio"
        if (msg_count - guard.last_compact_at_message) >= cfg.compact_message_delta:
            return "message_delta"
        return None
```

**为什么不在 ActStep 内 / Observer / FinalizeStep 触发**：

- **ActStep 内超限**：即使 ActStep 内某轮发现 prompt_tokens 接近上限，也不直接 compact——Observer 即将介入处理本轮 tool use 噪声（产出简短 summary），且 actor 多轮的 transcript 本就不写入 long-term memory。此时 compact 没意义。
- **ObserveStep**：Observer 已经在做第一级压缩（把 actor 的多轮 tool use 浓缩为一条 verdict.summary）——这是本职工作，无需额外 compact。
- **FinalizeStep**：task 已结束、verdict.summary 已落 memory，**没有新需求进来**。此时 compact 是为可能根本不来的下一个 task 腾空间——是浪费 LLM 算力。即使下一个 task 真来了，下一轮 ReasonStep 入口本就会 check。

**自然形成的良性循环**：
- 每个 task：ReasonStep（check，通常通过）→ Act → Observer（per-task 压缩）→ Finalize（写 summary 入 memory）
- 多个 task 之后 memory 积累了多条 summary，下一次 ReasonStep 估算时超阈值 → 触发 compact
- Compact 后回到 ReasonStep，估算通过，正常 Act → Observer → Finalize
- 良性循环持续

> **设计要点**：`_should_compact` 是独立方法——但 V1 只有 ReasonStep 调用它。未来若用户场景需要"主动 compact"（如手动 RunHandle.compact()），这个方法可被外部复用。

#### 6.8.5 CompactStep

```python
class CompactStep:
    name = "compact"

    async def execute(self, state, ctx):
        agent = state.agent
        cfg = agent.loop_config
        events: list[Event] = []
        scope = MemoryScope(session_id=state.session.id, agent_id=agent.id)
        emit(events, MemoryCompactStarted(agent_id=agent.id))

        # 1) 自己装配 prompt（purpose="compact"）
        #    包含 identity[compact]（fallback 到 act） + 只读 capability + 完整待压缩的 short memory
        request = ContextRequest(purpose="compact", agent=agent, task=state.task, session=state.session, scope=scope)
        prompt = await ctx.assembler.assemble(request)

        # 2) 调 LLM 生成 summary（一次性调用，非多 turn）
        try:
            summary = await self._run_compact_llm(prompt, ctx, events)
        except Exception as e:
            # LLM 失败 → 走 fallback：纯机械截断，无 summary
            emit(events, MemoryCompactFailedFallback(error=str(e)))
            summary = None

        # 3) 调 MemoryProvider.apply_compact 摄取 COMPACT_SUMMARY + 归档旧事件
        #    内部隐含一次 ingest(COMPACT_SUMMARY)，外部 memory 系统也能感知
        result = await ctx.providers.memory.apply_compact(
            scope=scope,
            summary=summary or "",           # 空串走机械截断（保留 keep_last 条，无摘要头）
            keep_last=cfg.compact_keep_last,
            ctx=ctx.provider_ctx,
        )

        # 4) 重置 loop_guard：context_tokens=0 强制下轮 reason 重新测量
        new_guard = dataclasses.replace(
            agent.loop_guard,
            context_tokens=0,
            context_message_count=result.events_after,
            last_compact_at_message=result.events_after,
        )
        emit(events, MemoryCompacted(
            agent_id=agent.id,
            events_before=result.events_before,
            events_after=result.events_after,
            summary_tokens=estimate_tokens(summary) if summary else 0,
            fallback=(summary is None),
            summary_event_id=result.summary_event_id,
        ))

        return StepOutcome(
            next_step="reason",                # 回到 reason 重新装配
            state_patch={"loop_guard": new_guard},
            events=events,
        )
```

**`apply_compact` 返回 `CompactResult`**（与 §4.3 协议定义一致）：

```python
@dataclass
class CompactResult:
    events_before: int          # apply_compact 之前未 superseded 的事件数
    events_after: int           # 之后未 superseded 的事件数（含新写入的 COMPACT_SUMMARY）
    summary_event_id: str       # 新写入的 COMPACT_SUMMARY 事件 id
```

> 注：core 默认 StructuredBlackboard 实现里，"归档" = 标记 `is_superseded=true`（参见 §8.7 memory_events 表）；外部 provider（Mem0/LightRAG）可能内部用 tombstone / 索引剪枝等其他方式实现"归档"语义，但对外契约一致。

#### 6.8.6 Step 序列变化（含 Compact）

**默认路径（绝大多数 task）**——memory 未超限：

```
[task start，由 TaskManager 派发]
  ↓
ReasonStep（估算 → 未超阈值）
  ↓ next="act"
ActStep（多轮 LLM；transcript 仅在内存中；末尾测量 context_tokens 回写 loop_guard）
  ├─ normal     → next="observe"
  ├─ suspended  → next=None       （task SUSPENDED，等子任务；不进 observe）
  └─ max_turns  → next=None       （task FAILED）
  ↓
ObserveStep（per-task 压缩：actor 多轮 tool use → 一条 verdict.summary）
  ↓ next="finalize"
FinalizeStep（写 user_prompt + verdict.summary → memory；publish 到 blackboard；
              更新 task 状态——但不 check compact）
  ↓ next=None    task 结束，返回 TaskManager
```

**Compact 触发路径**——memory 累积超限时：

```
[task start，由 TaskManager 派发]
  ↓
ReasonStep（估算 → 超阈值！多个历史 task 的 summary 累积过多）
  ↓ next="compact"
CompactStep（压缩多条 summary 为 [Context so far]；
             保留最近 N 条；重置 loop_guard.context_tokens=0）
  ↓ next="reason"
ReasonStep（重测估算 → 现在已通过）
  ↓ next="act"
ActStep → ObserveStep → FinalizeStep → next=None
```

**关键性质**：
- CompactStep 只在 task **起点**前置触发，永远不在 task 中段或末尾
- Compact 的本质是"为新工作腾空间"，所以只在新工作即将进入时（ReasonStep 入口）检查
- Observer 是 per-task 压缩器（每轮一次，本职工作）；Compact 是累积压缩器（偶尔一次，抢救机制）
- 两级压缩**协作**：Observer 控制单 task memory 增长（每轮只 +2 条），Compact 控制累积总量（超限时缩减）

#### 6.8.7 BudgetStrategy 在 Composer 阶段的兜底

§6.8.4 的早期 check 是**粗粒度的**（基于 message-level 估算）。但 ContextAssembler 在所有 Source fetch 完毕后，可以拿到**每个 block 的精确 token_estimate**——此时 BudgetStrategy 再做一次精确裁剪：

```
total = sum(block.token_estimate for block in blocks)
if total > limit:
    # 裁剪顺序（priority 数字越大越早裁）：
    #   reference (knowledge retrieval)  → 裁
    #   long_memory summary              → 裁
    #   history (oldest first)           → 裁
    #   capabilities (低使用频率优先)     → 裁
    #   identity / task_spec             → priority=0 不可裁
    while total > limit and 有可裁 block:
        drop_lowest_priority_block()
        total -= dropped.token_estimate
    if total > limit:
        raise ContextOverflowError
```

ReasonStep 捕获 `ContextOverflowError`：
- 若本步刚从 compact 过来（state.just_compacted=True）→ session FAILED（无解，identity+capabilities+一条消息已超）
- 否则 → 再次 next_step="compact"（强制 compact 一次）

#### 6.8.8 上下文长度演变示意

```
context tokens
        ▲
  180k  ┤ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ context_limit (hard)
        │
  144k  ┤ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ compact_threshold (80%)
        │                        ▲
  100k  ┤              ╱╱╱╱╱╱╱╱╱╱│
        │         ╱╱╱╱╱           │ COMPACT triggered
   60k  ┤    ╱╱╱╱╱                ▼            ╱╱╱╱╱
        │ ╱╱╱                  ↓ reset ↓   ╱╱╱
   20k  ┤                       ╲╲╲       ╱
        │                          ╲╲╲ ╱╱╱
    0k  └─┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬─►
         R₁ A₁ R₂ A₂ R₃ A₃ R₄ C  R₅ A₅ R₆ A₆
         
R = ReasonStep   A = ActStep（LLM 调用，产生精确测量）
C = CompactStep  ↓ = context_tokens 重置为 0
```

#### 6.8.9 关键事件

| 事件 | emit 位置 | 用途 |
|------|----------|------|
| `ContextTokensMeasured` | ActStep 末尾 | 把精确测量值通知外部（可用于实时显示进度条） |
| `ContextTokensEstimated` | ReasonStep 入口 | 让外部看到估算逻辑结果 |
| `CompactTriggered` | ReasonStep 入口决定进 compact 时 | reason（token_ratio / message_delta） |
| `MemoryCompactStarted` | CompactStep 开始 | UI 显示"正在压缩历史" |
| `MemoryCompacted` | CompactStep 成功 | 完成 + 统计（前/后消息数、summary token 数） |
| `MemoryCompactFailedFallback` | CompactStep LLM 失败 | 走机械截断，外部可警告 |
| `ContextOverflowError` | BudgetStrategy 兜底失败 | 兜底失败的严重错误信号 |

### 6.9 ObserveStep 与 FinalizeStep——每轮 Memory 的正常写入路径

本节阐明每个 task 的**正常 memory 写入路径**：Observer 产出 verdict、FinalizeStep 落盘。Compact 是这条路径**之外**的抢救机制，绝不替代它。

#### 6.9.1 ObserveStep 详细行为

ObserveStep 的核心任务是**评估当前 task 是否完成 + 产出本轮工作的摘要**。它走 purpose="observe" 的装配（参见 §4.6.6 / §5.3 / §6.7 的 observe 路径）。

```python
class ObserveStep:
    name = "observe"

    async def execute(self, state, ctx):
        agent = state.agent
        transcript = state.transcript   # ActStep 写入的本轮 LLM 多轮记录
        events: list[Event] = []

        # 1) 装配 observe-purpose 上下文
        request = ContextRequest(
            purpose="observe", agent=agent, task=state.task,
            session=state.session, scope=...,
        )
        prompt = await ctx.assembler.assemble(request)
        # 主要来源：
        #   - IdentitySource → facets["observe"]（ROLE）
        #   - CapabilitySource → purposes 含 "observe" 的 control capability
        #     （主要是 submit_task_assessment、request_human_input）
        #   - TaskSpecSource → 当前 task 描述
        #   - ActorTranscriptSource → ActStep 的 transcript

        # 2) 调 LLM（流式）
        verdict_text, tool_calls, usage = await self._run_observer_llm(prompt, ctx, events)
        ctx.guard.add_tokens(usage.total_tokens)

        # 3) 处理 control tool_call: submit_task_assessment
        #    它会写入 task.process_report / 决定 task_outcome（success/failed/needs_user_input）
        verdict = await self._dispatch_observer_capabilities(tool_calls, state, ctx, events)
        # verdict 包含：
        #   - task_outcome: Literal["success", "failed", "needs_user_input"]
        #   - summary: str    # 本轮工作的总结文本——这是 memory 的关键产出

        # 4) 测量 + 记录
        max_pt = max(
            (r.usage.prompt_tokens for r in [TurnRecord(usage=usage)]),
            default=agent.loop_guard.context_tokens,
        )
        new_guard = dataclasses.replace(
            agent.loop_guard,
            context_tokens=max(max_pt, agent.loop_guard.context_tokens),
        )
        emit(events, ObserveCompleted(
            agent_id=agent.id, task_id=state.task.id,
            outcome=verdict.task_outcome,
            summary_length=len(verdict.summary),
        ))
        return StepOutcome(
            next_step="finalize",
            state_patch={
                "verdict": verdict,
                "loop_guard": new_guard,
            },
            events=events,
        )
```

**关键点**：
- ObserveStep **产出**verdict 但不直接写 memory，交给 FinalizeStep
- verdict.summary 是 Observer 对本轮工作的总结文本——后续将作为 assistant 消息写入 memory
- Observer 可调的 capability 主要是 control 类（submit_task_assessment）和 HITL 类（request_human_input）

#### 6.9.2 FinalizeStep 详细行为

FinalizeStep 是 task 的纯收尾步骤：写本轮 memory + publish blackboard + 更新 task 状态。**不做 compact**——compact 是为新工作腾空间，本步处理的是已结束的当前轮次，没有新需求进来，做 compact 是浪费 LLM 算力。

```python
class FinalizeStep:
    name = "finalize"

    async def execute(self, state, ctx):
        agent = state.agent
        task = state.task
        verdict = state.verdict
        events: list[Event] = []
        scope = MemoryScope(session_id=state.session.id, agent_id=agent.id)

        # 1) ★ Ingest USER_PROMPT（若尚未写过）
        if task.user_prompt and not task.user_prompt_in_memory:
            await ctx.providers.memory.ingest(MemoryEvent(
                type=MemoryEventType.USER_PROMPT,
                scope=scope, role="user",
                content=task.user_prompt,
                metadata={"task_id": task.id},
                timestamp=now_utc(),
            ), ctx.provider_ctx)
            await ctx.state.mark_user_prompt_in_memory(task.id)
            emit(events, MemoryIngested(agent_id=agent.id, type="user_prompt", source="task_prompt"))

        # 2) ★ Ingest OBSERVER_SUMMARY（normal memory 写入路径）
        if verdict.summary or task.outputs:
            content = self._compose_memory_content(verdict.summary, task.outputs)
            await ctx.providers.memory.ingest(MemoryEvent(
                type=MemoryEventType.OBSERVER_SUMMARY,
                scope=scope, role="assistant",
                content=content,
                metadata={"task_id": task.id, "outcome": verdict.task_outcome},
                timestamp=now_utc(),
            ), ctx.provider_ctx)
            emit(events, MemoryIngested(
                agent_id=agent.id, type="observer_summary",
                content_length=len(content) if isinstance(content, str) else 0,
            ))

        # 3) Publish 到 task.id topic（父 task resume 时消费）
        if task.outputs:
            await ctx.providers.memory.ingest(MemoryEvent(
                type=MemoryEventType.BLACKBOARD_PUBLISH,
                scope=scope, content=task.outputs,
                topic=task.id,           # 该 task 自己的 topic
                metadata={"task_id": task.id},
                timestamp=now_utc(),
            ), ctx.provider_ctx)

        # 4) 更新 task 状态
        if verdict.task_outcome == "success":
            await ctx.state.mark_task_finished(task.id)
        elif verdict.task_outcome == "failed":
            await ctx.state.mark_task_failed(task.id)
        elif verdict.task_outcome == "needs_user_input":
            await ctx.state.mark_task_pending_hitl(task.id)

        # 5) 收尾完成——不做 compact，让下一轮 ReasonStep 入口自然检查
        emit(events, TaskFinalized(task_id=task.id, outcome=verdict.task_outcome))
        return StepOutcome(next_step=None, state_patch={}, events=events)
```

**关键点**：
- 写 memory 是 FinalizeStep 的**显式动作**（不再隐式靠 ActStep 或其他）
- verdict.summary 是这条写入的 content（Observer 总结的"本轮做了什么"）
- **不 check compact**——本步没有"为谁腾空间"的需求；若 memory 真的累积过多，下一个 task 的 ReasonStep 入口自然会发现并触发

#### 6.9.3 Compact 唯一触发点

| Check 点 | 位置 | 触发条件 |
|---------|------|---------|
| **唯一触发** | ReasonStep 入口 | estimated_tokens ≥ limit × ratio 或 messages_since_last_compact ≥ delta |

详见 §6.8.4。

#### 6.9.4 与 Compact 的职责对比

| 维度 | ObserveStep + FinalizeStep（normal path） | CompactStep（rescue path） |
|------|----------------------------------------|--------------------------|
| 触发频率 | 每个 task 都执行 | 偶尔（满足触发条件时） |
| 写入对象 | `ingest()` 一条 `OBSERVER_SUMMARY` 事件到 memory_events | `apply_compact()` 写入一条 `COMPACT_SUMMARY` 并把之前同 scope 的旧事件（除保留 N 条外）标记 `is_superseded=true` |
| 输入 | ActStep 的 transcript（单轮 task 的工作） | 全部当前未 superseded 的同类事件（多轮积累） |
| 输出 | 单条简短 summary（"本轮做了什么"） | `[Context so far]` 长摘要（"过去这么久做了什么"） |
| LLM 调用 | observer LLM（purpose="observe"） | compactor LLM（purpose="compact"） |
| Memory 影响 | memory 大小 +1 条 | memory 大小大幅缩减（N 条 → 1 条 + last 6） |

**形象的类比**：
- Observer 是**写日记**——每天晚上写一句"今天做了什么"
- Compact 是**整理日记**——日记本快满了，把过去几个月的内容压成一段"过去做过这些事"，腾出新页

两者并不冲突，前者是后者的输入来源——日记内容多了才需要整理。

---

## 7. 控制面 API（Control Plane）

### 7.1 RunHandle

每次 `loop_engine.run(...)` 返回一个 `RunHandle`，外部通过 handle 控制运行：

```python
class RunHandle:
    run_id: str
    session_id: str
    task_id: str

    @property
    def status(self) -> RunStatus: ...   # PENDING / RUNNING / PAUSED / FINISHED / FAILED / CANCELED

    async def events(self) -> AsyncIterator[Event]:
        """订阅本次 run 的事件流（SSE/WebSocket 直接消费）。"""

    async def pause(self) -> None:
        """请求暂停。在下一个 step 边界生效。"""

    async def resume(self) -> None:
        """从暂停状态恢复。"""

    async def cancel(self, reason: str = "") -> None:
        """请求取消。在下一个 step 边界生效，正在跑的 capability 收到 cancel 信号。"""

    async def step(self) -> StepOutcome:
        """单步模式：执行一个 step 后自动暂停（调试用）。"""

    async def inspect(self) -> RunSnapshot:
        """取当前状态快照（state + recent events）。"""

    async def replay(self, until_event_id: str) -> RunSnapshot:
        """从事件日志重放到指定 event，返回该时刻的快照。需要可重现性。"""
```

### 7.2 三类 Token 协作

`CancelToken` / `PauseToken` / `Deadline` 通过 `LoopContext` 注入每个 step：

- step 内的长操作（LLM 流式调用、tool 调用）必须周期性 `await token.checkpoint()`
- `checkpoint()` 在被 cancel 时抛 `CancelledError`，被 pause 时挂起 await
- LLM Gateway / CapabilityProvider 必须把 token 透传到底层 IO

### 7.3 Checkpoint

为了让 replay 不必从头开始，按以下频率自动 snapshot：

- 每个 task 开始前
- 每个 step 完成后（轻量增量 checkpoint）
- 每 N 个 event（用户可配，默认 50）
- 显式 HITL 暂停前（必须）

```python
@dataclass
class RunSnapshot:
    run_id: str
    snapshot_at: datetime
    loop_state: LoopState                    # 完整 loop state
    last_event_id: str
    last_event_sequence: int                 # 与 events.sequence 对齐
    pending_steps: list[str]                 # 还没执行的 step
    snapshot_reason: str                     # step_boundary / event_count / hitl_pause / manual
```

### 7.4 Replay / Inspect / Step 三能力概览

V1 控制面提供三种相关但语义不同的能力：

| 能力 | 输入 | 行为 | 输出 | 副作用 |
|------|------|------|------|--------|
| **Inspect** | run_id（隐含=当前时刻） | 取当前 state 快照 | RunStateView | 无 |
| **Pure Replay** | run_id + 目标 event_id 或 timestamp | 从最近 snapshot 出发，reduce 事件到目标点 | RunStateView at target | 无（**不重新调用 LLM/tool**） |
| **Step**（单步调试） | run_id | 执行一个 step 后立即 pause | StepOutcome | 真实副作用（含 LLM/tool） |
| Effective Replay（V2） | run_id + 起点 + 修改 | 从 checkpoint 实际重跑 | 新事件流 | 真实副作用 |

下面依次展开 V1 三种能力的算法。

### 7.5 Pure Replay 算法

#### 7.5.1 核心思路

```
target_event_sequence = T
  ↓
1. 找最近 snapshot：last_event_sequence ≤ T 且最大
  ↓
2. 反序列化 snapshot.state_blob → base_state
  ↓
3. 读取事件流：sequence ∈ (snapshot.last_event_sequence, T]，按 sequence 升序
  ↓
4. 对每个事件应用 reducer：state = reduce(state, event)
  ↓
5. 返回 RunStateView(state, target_event_id, ...)
```

关键性质：
- **纯函数 reducer**：每个 event 类型对应一个 `state, event → new_state` 函数，**不做任何 IO**
- **幂等**：同一起点、同一目标事件，重复 replay 结果一致
- **不重新调用 LLM/tool**：LLM 输出和 tool 结果已经记录在 `LLMResponseFinished` / `CapabilityFinished` 等事件的 payload 里，reducer 直接使用

#### 7.5.2 Snapshot 选择 SQL

```sql
SELECT * FROM event_snapshots
WHERE run_id = $run_id
  AND last_event_sequence <= $target_sequence
ORDER BY last_event_sequence DESC
LIMIT 1;
```

若无 snapshot（极早期事件）→ 从空 state 开始 reduce 全部事件。

#### 7.5.3 Event Reducer 分类

事件按 reducer 行为分四类：

| 类别 | 示例事件 | Reducer 行为 |
|------|---------|------------|
| **state-mutating** | TaskCreated / TaskFinished / AgentInstantiated / AgentSpawned / TokenBudgetExceeded | 修改 state.tasks / state.agents / state.session 等字段 |
| **measurement** | ContextTokensMeasured / LLMResponseFinished（usage 部分） | 更新 loop_guard.context_tokens 等度量字段 |
| **memory-tracking** | MemoryIngested / MemoryCompacted / BlackboardSubscribed | 更新 state.memory_indices（订阅 cursor 等），不重新 ingest |
| **observation-only** | LLMTokenStreamed / CapabilityProgress / StepStarted / StepCompleted | reducer 为 no-op（仅 inspect 模式按时间序展示，不影响 state） |

> Reducer 是 core 内的纯函数表（dict[event_type → reducer_fn]），加新事件类型时配套加 reducer。

#### 7.5.4 Reducer 接口示意

```python
EventReducer = Callable[[LoopState, Event], LoopState]

REDUCERS: dict[str, EventReducer] = {
    "TaskCreated": _reduce_task_created,
    "TaskStarted": _reduce_task_started,
    "TaskFinished": _reduce_task_finished,
    "TaskFailed": _reduce_task_failed,
    "TaskSuspended": _reduce_task_suspended,
    "AgentInstantiated": _reduce_agent_instantiated,
    "AgentSpawned": _reduce_agent_spawned,
    "AgentFinalized": _reduce_agent_finalized,
    "ContextTokensMeasured": _reduce_tokens_measured,
    "MemoryCompacted": _reduce_compacted,
    # ... observation-only events not in this dict
}


def _reduce_task_finished(state: LoopState, ev: Event) -> LoopState:
    new_tasks = dict(state.tasks)
    t = new_tasks[ev.task_id]
    new_tasks[ev.task_id] = dataclasses.replace(t,
        status="FINISHED",
        outputs=ev.payload.get("outputs"),
        process_report=ev.payload.get("summary"),
        finished_at=ev.timestamp,
    )
    return dataclasses.replace(state, tasks=new_tasks)


def _reduce_tokens_measured(state: LoopState, ev: Event) -> LoopState:
    agents = dict(state.agents)
    a = agents[ev.agent_id]
    new_guard = dataclasses.replace(a.loop_guard,
        context_tokens=ev.payload["measured_tokens"],
        context_message_count=ev.payload["message_count"],
    )
    agents[ev.agent_id] = dataclasses.replace(a, loop_guard=new_guard)
    return dataclasses.replace(state, agents=agents)


def reduce_event(state: LoopState, event: Event) -> LoopState:
    reducer = REDUCERS.get(event.type)
    return reducer(state, event) if reducer else state  # observation-only no-op
```

#### 7.5.5 完整 Replay 算法

```python
async def pure_replay(
    self, run_id: str,
    target_event_id: Optional[str] = None,
    target_timestamp: Optional[datetime] = None,
) -> RunStateView:
    # 1) 解析 target_sequence
    if target_event_id:
        target_event = await event_store.get(target_event_id)
        target_seq = target_event.sequence
    elif target_timestamp:
        target_seq = await event_store.find_seq_at_or_before(run_id, target_timestamp)
    else:
        target_seq = await event_store.latest_sequence(run_id)

    # 2) 找最近 snapshot
    snapshot = await event_store.latest_snapshot_at_or_before(run_id, target_seq)

    # 3) 反序列化起点
    if snapshot is None:
        state = LoopState.empty(run_id)
        from_seq = 0
    else:
        state = LoopState.from_blob(snapshot.state_blob)
        from_seq = snapshot.last_event_sequence

    # 4) Reduce 事件流到目标
    events_replayed = 0
    async for event in event_store.read_range(run_id, from_seq + 1, target_seq):
        state = reduce_event(state, event)
        events_replayed += 1

    # 5) 返回视图
    return RunStateView(
        run_id=run_id,
        target_event_id=target_event_id or "(latest)",
        target_event_sequence=target_seq,
        snapshot_base_sequence=from_seq,
        events_replayed=events_replayed,
        state=state,
    )
```

#### 7.5.6 RunStateView 数据结构

```python
@dataclass
class RunStateView:
    """Replay/Inspect 的统一返回。"""
    run_id: str
    target_event_id: str
    target_event_sequence: int
    target_timestamp: datetime

    snapshot_base_sequence: int     # 从哪个 snapshot 出发的（用于诊断 replay 性能）
    events_replayed: int

    state: LoopState                # 重建出的完整 state（含 sessions / tasks / agents / guards / subscriptions）

    # 可选附带：在 [snapshot_base, target] 范围内的关键事件列表（供 UI 展示时间轴）
    timeline: Optional[list[EventSummary]] = None
```

#### 7.5.7 性能考量

- 默认 snapshot 频率（每 50 事件）下，replay 跨度通常 < 50 个事件，~ms 级
- 长跑 task：单 ActStep 可能产生 100+ events（每个 LLM chunk + tool call），会快速触发 snapshot
- Snapshot blob 大小：典型 5-50KB（含 agents + tasks + 关键 guards），可 JSONB 直接存

### 7.6 Inspect 实现

```python
class RunHandle:
    async def inspect(self) -> RunStateView:
        """取当前 state 快照——等价于 pure_replay(target=latest)。"""
        return await self._replay_engine.pure_replay(self.run_id)

    async def replay(
        self,
        until_event_id: Optional[str] = None,
        at_timestamp: Optional[datetime] = None,
    ) -> RunStateView:
        return await self._replay_engine.pure_replay(
            self.run_id, target_event_id=until_event_id, target_timestamp=at_timestamp,
        )
```

Inspect 是 Pure Replay 的特例（target = latest），用同一套机制。**这是关键设计简化**——一套引擎覆盖两种用法。

#### 7.6.1 Inspect 的典型用途

- **Admin UI 实时面板**：定期 inspect 显示当前 session/task/agent 状态
- **崩溃恢复后状态校验**：进程重启后 inspect 当前持久化 state 与 inspect 比对一致性
- **调试 stuck session**：管理员 inspect 看 task 卡在什么状态、guard 计数等

### 7.7 Step（单步执行）模式

#### 7.7.1 用途

为调试 agent 行为，允许"执行一个 step 后立即 pause"，让开发者逐步观察 reasoning / acting / observing。

#### 7.7.2 实现机制

```python
class RunHandle:
    async def step(self) -> StepOutcome:
        """单步：当前若 paused 则执行一个 step 后再 pause，否则等待当前 step 完成后立即 pause。"""
        await self.resume()                              # 若处于 pause 状态，先恢复
        await self._driver.set_single_step_mode(True)    # 标记下一个 step 完成后自动 pause
        outcome = await self._driver.wait_step_done()    # 等待 step 边界
        await self._driver.set_single_step_mode(False)
        return outcome
```

Driver 内部支持 `single_step_mode` 标志：

```python
class StepDriver:
    async def run(self, initial_state, ctx):
        state = initial_state
        next_step_name = self._initial
        while next_step_name is not None:
            await ctx.cancel_token.checkpoint()
            await ctx.pause_token.checkpoint()

            step = self._steps[next_step_name]
            emit(StepStarted(step=step.name))
            outcome = await step.execute(state, ctx)
            state = state.apply_patch(outcome.state_patch)
            for ev in outcome.events:
                emit(ev)
            emit(StepCompleted(step=step.name))

            yield outcome

            # ★ 单步模式：本 step 完成后自动请求 pause
            if self._single_step_mode:
                ctx.pause_token.request_pause(reason="single_step_completed")

            if outcome.request_pause:
                await ctx.pause_token.wait()

            next_step_name = outcome.next_step
```

#### 7.7.3 与 HITL 的区别

| 维度 | Step（调试单步） | HITL（人工审批） |
|------|----------------|-----------------|
| 触发 | 外部 RunHandle.step() | capability.side_effects + Authorizer 要求 |
| 暂停位置 | step 边界 | capability invoke 前 |
| 谁恢复 | 开发者再次调 step() / resume() | 用户 approve/reject |
| 持久化 | 仅 pause 标志（snapshot） | hitl_approvals 表（§8.5） |

两者都通过 pause_token 实现暂停，但 Step 是"主动调试控制"，HITL 是"业务流转中的审批门"。

### 7.8 Replay 的边界与限制

| 能力 | V1 状态 |
|------|--------|
| 重建任意时刻 state（含 session/task/agent） | ✅ |
| 时间轴展示（按时序列出 events） | ✅ |
| 跨 schema 版本 replay | ⚠ 部分——events 表加字段是兼容的，删字段需要 event 版本管理 |
| 重新执行（产生新事件） | ❌ V1 不支持，需要 V2 Effective Replay |
| 分叉重放（从历史点改一个参数重跑） | ❌ V2 |
| 跨 LLM 模型版本（"换 GPT 模型重跑"） | ❌ V2 |
| 大型 run 的快速 replay（含 10k+ events） | ⚠ 依赖 snapshot 频率；典型 < 100ms（默认配置下） |

**已知约束**：
- Reducer 必须保持向前兼容：events 表新加字段不能让旧 reducer 报错；reducer 函数库需要按 event version 注册多版本（V2 计划）
- LLM/tool 输出已记录在 event payload——所以 replay 不需要重新调用，但意味着 `LLMResponseFinished` / `CapabilityFinished` 的 payload **必须完整携带可重建状态的数据**（详见 §9 各事件的 payload schema）
- Observation-only 事件不被 reducer 处理，但仍按时间序参与 `timeline` 展示

### 7.9 与 §8 持久化的协作

Replay 引擎与持久化层的依赖关系：

```
RunHandle.replay(...)
        │
        ▼
ReplayEngine.pure_replay(target)
        │
        ├─► EventStore.latest_snapshot_at_or_before(run_id, seq) ─► event_snapshots 表
        │
        ├─► EventStore.read_range(run_id, from, to)             ─► events 表
        │     按 (run_id, sequence) 索引，单调拉取
        │
        └─► reduce_event(state, event) × N                      ─► 纯内存计算
```

主要查询模式（命中 §8.10 中的索引）：
- snapshot 选择：`event_snapshots` 上 `(run_id, last_event_sequence DESC)` 索引
- 事件范围拉取：`events` 上 `(run_id, sequence)` 唯一索引

---

## 8. 状态模型与持久化

### 8.1 选定方案

**B 方案：Current State + Event Log，event 是状态变更唯一入口**。

具体规则：
- 主表（`sessions` `tasks` `agents` `runs`）记录当前状态
- 任何字段修改必须经过 `apply_event(event)` 函数 ——`apply_event` 既负责事件落日志，也负责状态字段更新（在同一事务里）
- 业务代码不允许 `task.status = "FINISHED"` 这种直接赋值，必须 `apply(TaskFinished(...))`

这样后续切到全量 event sourcing 是平滑的：只需要把 `apply_event` 的实现从"先存事件后改状态"切到"先存事件，状态由 projection 异步重算"。

### 8.2 StateStore 协议

```python
class StateStore(Protocol):
    """state 持久化抽象。"""
    async def get_session(self, session_id: str) -> Session: ...
    async def get_task(self, task_id: str) -> Task: ...
    async def get_agent(self, agent_id: str) -> Agent: ...

    async def apply_event(
        self,
        event: Event,
        state_mutation: Callable[[Connection], Awaitable[None]],
    ) -> None:
        """在同一事务里：写 event log + 执行 state mutation。"""

class EventStore(Protocol):
    async def append(self, event: Event) -> None: ...
    async def read_since(self, run_id: str, cursor: int) -> AsyncIterator[Event]: ...
    async def snapshot(self, snapshot: RunSnapshot) -> None: ...
    async def latest_snapshot(self, run_id: str) -> RunSnapshot | None: ...
```

V1 参考实现：`PostgresStateStore + PostgresEventStore`（同一个 DB，同一事务）。

### 8.3 核心实体（精简版）

只保留 core 必须知道的字段，存储相关细节属于 host 层：

```python
@dataclass
class Session:
    id: str
    goal: str
    user_prompt: str
    status: SessionStatus
    root_agent_id: str
    token_budget: int
    token_used: int
    context_limit: int
    config: dict     # 灵活配置：max_turns / failure_threshold 等

@dataclass
class Task:
    id: str
    session_id: str
    kind: TaskKind
    parent_task_id: Optional[str]
    assigned_agent_id: str
    status: TaskStatus
    inputs: dict
    outputs: Optional[dict]
    dag_deps: list[str]
    requires_approval: bool

@dataclass
class Agent:
    id: str
    session_id: str
    template_id: str
    template_version: str                    # 实例化时 pin 的 template 版本（§4.6.5）
    parent_agent_id: Optional[str]
    spawn_depth: int
    status: AgentStatus
    bound_capability_ids: list[str]          # 实例化时一次性 resolve 的 capability id 列表
    memory_config: MemoryConfig              # 从 template 拷贝（实例化时快照）
    loop_config: LoopConfig                  # 从 template 拷贝（含 compact 阈值等，见 §6.8.2）
    loop_guard: LoopGuard                    # 运行时计数与测量（见 §6.8.2）
    runtime: dict = field(default_factory=dict)
                                             # 其他运行时数据：working_dir 等
```

注意**不直接存 identity / capability 详细内容**——它们由 TemplateResolver + CapabilityProvider 在实例化阶段 resolve 为完整 Capability 对象，并通过 in-memory `CapabilityCache` 缓存（§4.6.5）。Agent 表只持久化轻量级 ID 列表 + 版本 pin，崩溃恢复时按 template_id + template_version 重新 resolve（CapabilityCache 是软状态）。

### 8.4 PostgreSQL 参考 Schema 概览

V1 参考实现使用单一 PostgreSQL 实例（**无 pgvector 依赖**）。所有表分为四类：

| 类别 | 表 | 谁用 | 性质 |
|------|-----|------|------|
| **State 表** | `sessions` / `tasks` / `agents` / `hitl_approvals` | core | 可变状态，承载业务真相 |
| **Event 表** | `events` / `event_snapshots` | core | 不可变追加，承载历史与 replay 基础 |
| **Memory 表** | `memory_events` / `memory_subscriptions` | StructuredBlackboardMemoryProvider（core 默认 impl） | 统一 memory：摄取 + 召回 |
| **辅助表** | `capability_invocations` / `llm_providers` / `agent_templates` | host + 审计 | 配置、审计、template 元数据 |

**Memory schema 极简**：core 默认实现的 memory 仅两张表（事件 + 订阅）；外部 memory provider（Mem0/LightRAG/...）有自己的 schema，与 ctx-weft 无关。

**统一约定**：
- 所有表含 `created_at TIMESTAMPTZ NOT NULL DEFAULT now()` 与 `updated_at`（state 表）
- 主键统一 `VARCHAR(40)` 容纳 ULID（如 `ses_01H8K9XPYJ7DRT2RY3JFXSF7M2`）
- 多租户：所有表带 `tenant_id VARCHAR(64) NOT NULL DEFAULT 'default'`（V1 单租户硬编码 `default`）
- JSONB 字段不可为 NULL（用 `{}` 占位），简化查询
- 软删除：仅 template / provider 配置类表使用 `is_deleted`；运行时数据不删除

### 8.5 State 表 DDL

#### sessions

```sql
CREATE TABLE sessions (
    id                     VARCHAR(40) PRIMARY KEY,        -- ses_ULID
    tenant_id              VARCHAR(64) NOT NULL DEFAULT 'default',
    goal                   TEXT,                            -- 由 metadata_filler 提炼后写入
    user_prompt            TEXT NOT NULL,
    status                 VARCHAR(32) NOT NULL,            -- QUEUED/RUNNING/SUCCEEDED/FAILED/TIMEOUT/CANCELED/PAUSED_HITL
    root_agent_id          VARCHAR(40),                     -- 启动后填充

    -- Session-level guards
    token_budget           INTEGER NOT NULL DEFAULT 200000,
    token_used             INTEGER NOT NULL DEFAULT 0,
    context_limit          INTEGER NOT NULL DEFAULT 180000,
    max_concurrent_tasks   INTEGER NOT NULL DEFAULT 8,
    max_concurrent_agents  INTEGER NOT NULL DEFAULT 4,
    failure_counter        INTEGER NOT NULL DEFAULT 0,
    failure_threshold      INTEGER NOT NULL DEFAULT 3,

    config                 JSONB NOT NULL DEFAULT '{}',
    runtime_summary        JSONB,

    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at            TIMESTAMPTZ
);

CREATE INDEX idx_sessions_active        ON sessions(status) WHERE status IN ('QUEUED','RUNNING','PAUSED_HITL');
CREATE INDEX idx_sessions_tenant_recent ON sessions(tenant_id, created_at DESC);
```

#### tasks

```sql
CREATE TABLE tasks (
    id                     VARCHAR(40) PRIMARY KEY,        -- tsk_ULID
    tenant_id              VARCHAR(64) NOT NULL DEFAULT 'default',
    session_id             VARCHAR(40) NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    type                   VARCHAR(32) NOT NULL,           -- reasoning/tool-call/sub-agent/skill/hitl

    -- Topology
    assigned_agent_id      VARCHAR(40),
    creator_agent_id       VARCHAR(40),
    parent_task_id         VARCHAR(40) REFERENCES tasks(id),

    -- State machine
    status                 VARCHAR(32) NOT NULL,
    dag_deps               VARCHAR(40)[] NOT NULL DEFAULT '{}',

    -- HITL
    requires_approval      BOOLEAN NOT NULL DEFAULT false,
    approval_id            VARCHAR(40),                     -- 关联 hitl_approvals.id

    -- Inputs/outputs/verdict
    user_prompt            TEXT,
    user_prompt_in_memory  BOOLEAN NOT NULL DEFAULT false,
    inputs                 JSONB NOT NULL DEFAULT '{}',
    outputs                JSONB,                           -- 含多模态：[{type:"text",text:...},{type:"image",...}]
    process_report         TEXT,                            -- verdict.summary 快照

    -- Retry & timeout
    retry_count            INTEGER NOT NULL DEFAULT 0,
    max_retries            INTEGER NOT NULL DEFAULT 3,
    timeout_ms             INTEGER NOT NULL DEFAULT 60000,

    compensation           JSONB,
    priority               INTEGER NOT NULL DEFAULT 5,
    settings               JSONB NOT NULL DEFAULT '{}',     -- skill_name / working_dir / use_subagent / etc.

    error                  TEXT,
    error_code             VARCHAR(64),

    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at            TIMESTAMPTZ
);

CREATE INDEX idx_tasks_session_status   ON tasks(session_id, status);
CREATE INDEX idx_tasks_parent           ON tasks(parent_task_id) WHERE parent_task_id IS NOT NULL;
CREATE INDEX idx_tasks_assigned_agent   ON tasks(assigned_agent_id) WHERE assigned_agent_id IS NOT NULL;
CREATE INDEX idx_tasks_session_created  ON tasks(session_id, created_at);
-- 用于 TaskQueue 重建（崩溃恢复）
CREATE INDEX idx_tasks_runnable         ON tasks(session_id, status, priority DESC, created_at)
    WHERE status IN ('PENDING','ACTIVE','SUSPENDED');
```

#### agents

```sql
CREATE TABLE agents (
    id                          VARCHAR(40) PRIMARY KEY,    -- agt_ULID
    tenant_id                   VARCHAR(64) NOT NULL DEFAULT 'default',
    session_id                  VARCHAR(40) NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    template_id                 VARCHAR(40) NOT NULL,
    template_version            VARCHAR(32) NOT NULL,        -- 实例化时 pin

    -- Tree
    parent_agent_id             VARCHAR(40) REFERENCES agents(id),
    spawn_depth                 INTEGER NOT NULL DEFAULT 0,

    -- Capability binding (snapshot of resolved IDs; full Capability objects rebuilt from providers on resume)
    bound_capability_ids        TEXT[] NOT NULL DEFAULT '{}',

    -- Status
    status                      VARCHAR(32) NOT NULL,        -- IDLE/RUNNING/WAITING/FINISHED/FAILED
    active_task_id              VARCHAR(40),

    -- LoopGuard (§6.8.2)
    turns_used                  INTEGER NOT NULL DEFAULT 0,
    context_tokens              INTEGER NOT NULL DEFAULT 0,
    context_message_count       INTEGER NOT NULL DEFAULT 0,
    context_limit               INTEGER NOT NULL DEFAULT 180000,
    last_compact_at_message     INTEGER NOT NULL DEFAULT 0,

    -- Config snapshots (immutable for this agent)
    memory_config               JSONB NOT NULL,
    loop_config                 JSONB NOT NULL,

    runtime                     JSONB NOT NULL DEFAULT '{}',

    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_agents_session     ON agents(session_id);
CREATE INDEX idx_agents_alive       ON agents(status) WHERE status IN ('RUNNING','WAITING','IDLE');
CREATE INDEX idx_agents_parent      ON agents(parent_agent_id) WHERE parent_agent_id IS NOT NULL;
```

#### hitl_approvals

```sql
CREATE TABLE hitl_approvals (
    id                  VARCHAR(40) PRIMARY KEY,             -- hal_ULID
    tenant_id           VARCHAR(64) NOT NULL DEFAULT 'default',
    session_id          VARCHAR(40) NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    task_id             VARCHAR(40) NOT NULL REFERENCES tasks(id),
    invocation_id       VARCHAR(40),                          -- 关联 capability_invocations（若是 capability 触发）

    trigger_reason      VARCHAR(32) NOT NULL,                 -- explicit/failure_threshold/tool_risk
    capability_id       VARCHAR(128),
    task_snapshot       JSONB NOT NULL,                       -- 待审批 task 完整快照
    arguments           JSONB,

    status              VARCHAR(32) NOT NULL,                 -- PENDING/APPROVED/REJECTED/MODIFIED/TIMEOUT
    decided_by          VARCHAR(64),
    modified_arguments  JSONB,
    decision_reason     TEXT,

    expire_at           TIMESTAMPTZ NOT NULL,                 -- default created_at + 30 min
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at          TIMESTAMPTZ
);

CREATE INDEX idx_hitl_pending     ON hitl_approvals(status, expire_at) WHERE status = 'PENDING';
CREATE INDEX idx_hitl_task        ON hitl_approvals(task_id);
CREATE INDEX idx_hitl_session     ON hitl_approvals(session_id, created_at DESC);
```

**为什么 HITL 单表而非走 events**（§16 决议）：查询模式不同——"列出所有 PENDING"、"按过期扫描"、"按 task 查"都是热路径，专表索引更合适；event log 仍记录 `HitlRequired`/`HitlApproved` 等事件用于审计。

### 8.6 Event 表 DDL

#### events

```sql
CREATE TABLE events (
    id              VARCHAR(40) PRIMARY KEY,                  -- evt_ULID
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    run_id          VARCHAR(40),                              -- 一次 loop run 的标识；某些 session 级事件可为 NULL
    sequence        BIGINT NOT NULL,                          -- 在同一 (run_id) 内单调递增
    session_id      VARCHAR(40) NOT NULL,
    task_id         VARCHAR(40),
    agent_id        VARCHAR(40),

    type            VARCHAR(64) NOT NULL,                     -- e.g. 'TaskCreated' / 'LLMTokenStreamed' / 'CompactTriggered'
    payload         JSONB NOT NULL DEFAULT '{}',
    metadata        JSONB NOT NULL DEFAULT '{}',
    causation_id    VARCHAR(40),                              -- 引起此 event 的上游 event id

    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 主查询路径：按 run replay
CREATE UNIQUE INDEX uq_events_run_sequence ON events(run_id, sequence) WHERE run_id IS NOT NULL;
-- 按 session 查事件流（SSE 订阅）
CREATE INDEX idx_events_session_occurred  ON events(session_id, occurred_at);
-- 按 task 查相关事件
CREATE INDEX idx_events_task              ON events(task_id, occurred_at) WHERE task_id IS NOT NULL;
-- 按类型查（监控/告警）
CREATE INDEX idx_events_type_occurred     ON events(type, occurred_at DESC);

-- 分区建议（生产环境）：BY RANGE(occurred_at)，按月分区
-- ALTER TABLE events PARTITION BY RANGE(occurred_at);
```

#### event_snapshots

```sql
CREATE TABLE event_snapshots (
    id                       VARCHAR(40) PRIMARY KEY,         -- snp_ULID
    tenant_id                VARCHAR(64) NOT NULL DEFAULT 'default',
    run_id                   VARCHAR(40) NOT NULL,
    session_id               VARCHAR(40) NOT NULL,

    last_event_id            VARCHAR(40) NOT NULL,
    last_event_sequence      BIGINT NOT NULL,

    -- 快照 blob：含 LoopState + 关键 Task/Agent 字段
    state_blob               JSONB NOT NULL,
    snapshot_reason          VARCHAR(64),                      -- step_boundary / event_count / hitl_pause / manual

    created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_snapshots_run_seq ON event_snapshots(run_id, last_event_sequence DESC);
```

**Replay 算法（§7 RunHandle.replay）**：
1. 找到 ≤ target_event_sequence 的最近 snapshot：`SELECT ... ORDER BY last_event_sequence DESC LIMIT 1`
2. 从该 snapshot.state_blob 反序列化为内存 state
3. 顺序 apply event log 从 `snapshot.last_event_sequence + 1` 到 `target_sequence`
4. 返回最终 RunSnapshot

### 8.7 Memory Provider 持久化 schema（StructuredBlackboardMemoryProvider）

core 默认 memory 实现仅两张表——一张事件表 + 一张订阅表。所有 ingest 进来的事件统一进 `memory_events`；topic 没有独立的"topic 表"——topic 字段就是 entry 的属性，topic 元数据按需在查询时聚合。

#### memory_events（统一事件表）

```sql
CREATE TABLE memory_events (
    id              VARCHAR(40) PRIMARY KEY,                   -- mev_ULID
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    session_id      VARCHAR(40) NOT NULL,
    agent_id        VARCHAR(40),
    task_id         VARCHAR(40),

    type            VARCHAR(32) NOT NULL,                       -- MemoryEventType: user_prompt/llm_response/
                                                                -- tool_invocation/tool_result/observer_summary/
                                                                -- compact_summary/blackboard_publish
    role            VARCHAR(16),                                -- user/assistant/system/tool（仅适用类型）
    content         JSONB NOT NULL,                             -- str 或 list[ContentPart]
    metadata        JSONB NOT NULL DEFAULT '{}',

    topic           VARCHAR(256),                               -- 仅 topic-style 事件（blackboard_publish）；
                                                                -- 父子任务通信用 topic = task_id
    causation_id    VARCHAR(40),                                -- 关联上游事件
    token_count     INTEGER,

    seq_no          BIGINT NOT NULL,                            -- per (agent_id) 单调递增
    is_superseded   BOOLEAN NOT NULL DEFAULT false,             -- compact 后旧事件标记 superseded

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 主访问路径：recall_recent（按 agent + type 过滤、按时间倒序）
CREATE INDEX idx_mev_recent     ON memory_events(agent_id, is_superseded, type, seq_no DESC)
    WHERE is_superseded = false;
-- recall_topic（按 topic + seq 自 cursor 后拉取）
CREATE UNIQUE INDEX uq_mev_topic_seq ON memory_events(topic, seq_no) WHERE topic IS NOT NULL;
-- 按 session 查事件历史
CREATE INDEX idx_mev_session    ON memory_events(session_id, created_at);
-- 按 type 全局监控
CREATE INDEX idx_mev_type_time  ON memory_events(type, created_at DESC);
```

**核心查询模式**：

```sql
-- recall_recent(scope=agent_id=A1, types=[USER_PROMPT, OBSERVER_SUMMARY, COMPACT_SUMMARY], limit=20)
SELECT * FROM memory_events
WHERE agent_id = $1 AND is_superseded = false
  AND type = ANY($2::varchar[])
ORDER BY seq_no DESC LIMIT $3;

-- recall_topic(topic=T, since=cursor)
SELECT * FROM memory_events
WHERE topic = $1 AND seq_no > $2
ORDER BY seq_no LIMIT $3;

-- apply_compact: 把 scope 内 <= keep_last 之前的事件标记 superseded，并 insert COMPACT_SUMMARY
UPDATE memory_events SET is_superseded = true
WHERE agent_id = $1 AND is_superseded = false
  AND seq_no NOT IN (SELECT seq_no FROM memory_events
                     WHERE agent_id = $1 AND is_superseded = false
                     ORDER BY seq_no DESC LIMIT $2);
-- 然后 insert 新的 COMPACT_SUMMARY 事件
```

#### memory_subscriptions

```sql
CREATE TABLE memory_subscriptions (
    id              VARCHAR(40) PRIMARY KEY,                   -- mes_ULID
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    session_id      VARCHAR(40) NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    topic           VARCHAR(256) NOT NULL,

    cursor          BIGINT NOT NULL DEFAULT 0,                 -- 已读到的 seq_no（持久化）
    intent          VARCHAR(32) NOT NULL,                      -- parent_child/long_term_background/long_term_project_log
    priority        INTEGER NOT NULL DEFAULT 5,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX uq_mem_subs              ON memory_subscriptions(session_id, topic);
CREATE INDEX idx_mem_subs_session_intent     ON memory_subscriptions(session_id, intent);
```

**与外部 memory provider 的关系**：上述两张表**仅是 core 默认实现的内部 schema**。外部 provider（Mem0/LightRAG/Zep/...）有自己的 schema 与索引管道，ctx-weft 不规定。从 core 视角，只看到统一的 `MemoryProvider` 接口。

### 8.8 辅助表 DDL

#### capability_invocations（审计）

```sql
CREATE TABLE capability_invocations (
    id                  VARCHAR(40) PRIMARY KEY,               -- inv_ULID
    tenant_id           VARCHAR(64) NOT NULL DEFAULT 'default',
    session_id          VARCHAR(40) NOT NULL,
    task_id             VARCHAR(40) NOT NULL,
    agent_id            VARCHAR(40) NOT NULL,

    capability_id       VARCHAR(128) NOT NULL,
    capability_kind     VARCHAR(32) NOT NULL,                  -- tool/skill/agent

    arguments_redacted  JSONB,                                 -- 脱敏后参数
    result_summary      TEXT,                                  -- 短摘要（避免 result 太大无法快速查看）
    result_redacted     JSONB,                                 -- 完整 result（脱敏后）
    status              VARCHAR(32) NOT NULL,                  -- RUNNING/SUCCEEDED/FAILED/CANCELED
    is_error            BOOLEAN NOT NULL DEFAULT false,
    error_code          VARCHAR(64),
    error_message       TEXT,

    duration_ms         INTEGER,
    tokens_consumed     INTEGER,                               -- 若是 LLM 类 capability

    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ
);

CREATE INDEX idx_invocations_task              ON capability_invocations(task_id);
CREATE INDEX idx_invocations_session_started   ON capability_invocations(session_id, started_at DESC);
CREATE INDEX idx_invocations_capability_status ON capability_invocations(capability_id, status, started_at DESC);
```

`capability_invocations` 与 `events` 中的 `CapabilityInvoked`/`Finished`/`Failed` 是部分重复——之所以保留专表，是因为它支持高效的"查最近 N 次某 capability 调用"、"查 task 内所有 invocation 链"等审计查询；events 是流式时序，不适合这些查询。**保留 90 天**（合规审计）。

#### llm_providers（运行时供应商配置）

```sql
CREATE TABLE llm_providers (
    id              VARCHAR(40) PRIMARY KEY,                   -- llm_ULID
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    name            VARCHAR(64) NOT NULL,                      -- 唯一标识，用于 session.config 引用
    style           VARCHAR(32) NOT NULL,                      -- openai/anthropic/custom
    base_url        VARCHAR(256),
    model           VARCHAR(64) NOT NULL,
    timeout_sec     INTEGER NOT NULL DEFAULT 60,
    api_key_ref     VARCHAR(128),                              -- 引用外部 KMS / vault 中的 key 名，不存明文
    config          JSONB NOT NULL DEFAULT '{}',
    is_deleted      BOOLEAN NOT NULL DEFAULT false,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX uq_llm_providers_name ON llm_providers(tenant_id, name) WHERE is_deleted = false;
```

#### agent_templates（host 管理；core 只通过 TemplateResolver 读）

```sql
CREATE TABLE agent_templates (
    id              VARCHAR(40) PRIMARY KEY,                   -- tpl_ULID
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    name            VARCHAR(128) NOT NULL,
    version         VARCHAR(32) NOT NULL,                      -- semver

    identity        JSONB NOT NULL,                            -- dict[Purpose, IdentityFacet]
    capability_refs JSONB NOT NULL,                            -- list[CapabilityRef]
    memory_config   JSONB NOT NULL,
    loop_config     JSONB NOT NULL,
    metadata        JSONB NOT NULL DEFAULT '{}',

    is_deleted      BOOLEAN NOT NULL DEFAULT false,            -- 软删除以保护 pin 该 version 的 running agent
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX uq_templates_name_version ON agent_templates(tenant_id, name, version);
CREATE INDEX idx_templates_name_latest       ON agent_templates(tenant_id, name, version DESC) WHERE is_deleted = false;
```

**关键约束**：template 一旦被某 agent pin（agents 表中存在 template_id+template_version），就**永远不能物理删除**——只能软删除。否则崩溃恢复时 agent 找不到自己的 template 会失败。host 的 template DELETE API 实际只是设 `is_deleted = true`，TemplateResolver 仍可读到。

### 8.9 apply_event 事务模式

`StateStore.apply_event` 把 event 落日志和 state mutation 绑在单一事务中：

```python
class PostgresStateStore(StateStore):
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def apply_event(
        self,
        event: Event,
        state_mutation: Callable[[asyncpg.Connection], Awaitable[None]] | None = None,
    ) -> None:
        """同一事务内：(1) append event log，(2) 执行 state mutation。
        state_mutation=None 时仅落事件（适合纯观测事件如 LLMTokenStreamed）。"""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """INSERT INTO events (id, tenant_id, run_id, sequence,
                       session_id, task_id, agent_id, type, payload, metadata,
                       causation_id, occurred_at)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10::jsonb,$11,$12)""",
                    event.id, event.tenant_id, event.run_id, event.sequence,
                    event.session_id, event.task_id, event.agent_id,
                    event.type, json.dumps(event.payload), json.dumps(event.metadata),
                    event.causation_id, event.timestamp,
                )
                if state_mutation is not None:
                    await state_mutation(conn)
```

**调用约定**（业务代码示例）：

```python
# Bad: 直接改字段——禁止
task.status = "FINISHED"
await task_store.save(task)

# Good: 走 apply_event
async def mark_task_finished(task_id, verdict):
    event = Event(
        type="TaskFinished",
        task_id=task_id,
        payload={"outcome": verdict.task_outcome, "summary": verdict.summary},
        sequence=...,
        ...
    )
    async def mutate(conn):
        await conn.execute(
            """UPDATE tasks SET status='FINISHED', process_report=$1,
               updated_at=now(), finished_at=now() WHERE id=$2""",
            verdict.summary, task_id,
        )
    await state_store.apply_event(event, mutate)
```

**沉淀此模式的设计意图**：
- 事件与状态原子一致——不会出现"状态变了但事件没发"或反之
- V2 切换到 Event Sourcing 平滑——只需把 mutate 抽到 projector 异步消费 event 流，apply_event 内部不再调 mutate

### 8.10 索引与典型查询模式

下表列出 V1 几条热路径查询及其依赖的索引：

| 查询场景 | SQL 模式 | 命中索引 |
|---------|---------|---------|
| 列出活跃 session（admin 面板） | `WHERE status IN ('RUNNING','PAUSED_HITL')` | `idx_sessions_active` |
| TaskQueue 重建（崩溃恢复） | `WHERE session_id=$1 AND status IN ('PENDING','ACTIVE','SUSPENDED')` | `idx_tasks_runnable` |
| 父任务 resume：列出所有子任务 | `WHERE parent_task_id=$1` | `idx_tasks_parent` |
| Agent 列出当前 session 树 | `WHERE session_id=$1` | `idx_agents_session` |
| HITL 待审批列表 | `WHERE status='PENDING' ORDER BY expire_at` | `idx_hitl_pending` |
| HITL 超时扫描器 | `WHERE status='PENDING' AND expire_at < now()` | `idx_hitl_pending` |
| SSE 事件流订阅 | `WHERE session_id=$1 AND occurred_at > $2 ORDER BY occurred_at` | `idx_events_session_occurred` |
| Replay：按 run 取事件 | `WHERE run_id=$1 AND sequence > $2 ORDER BY sequence` | `uq_events_run_sequence` |
| recall_recent（按 type） | `WHERE agent_id=$1 AND is_superseded=false AND type=ANY($2) ORDER BY seq_no DESC LIMIT N` | `idx_mev_recent` |
| recall_topic 自 cursor 拉取 | `WHERE topic=$1 AND seq_no > $2 ORDER BY seq_no` | `uq_mev_topic_seq` |
| 审计：某 capability 最近调用 | `WHERE capability_id=$1 ORDER BY started_at DESC` | `idx_invocations_capability_status` |

### 8.11 迁移、备份与恢复

#### Schema 迁移

使用 **Alembic** 管理（与 SQLAlchemy 2.0 配套）：

```
CtxWeft/host/persistence/postgres/migrations/
├── alembic.ini
├── env.py
└── versions/
    ├── 0001_initial_schema.py       # 创建所有表 + 索引（本节内容）
    ├── 0002_add_partition_events.py # V1.x：events 按月分区
    └── ...
```

**迁移原则**：
- 每个 PR 必须含对应 migration
- DDL 变更不允许直接 ALTER TABLE 在生产；先 migration → review → 部署 → 应用
- 破坏性变更（DROP COLUMN）需要两阶段：先停止使用 → 等部署稳定 → 下一版本再 DROP

#### 备份

- **完整快照**：`pg_dump` 每日，保留 30 天
- **WAL 归档**：开启 archive_mode，支持 PIT 恢复
- **events 表特殊处理**：增长最快，建议独立 partition + 长期归档到 S3（V2）

#### 崩溃恢复

进程崩溃 + 重启后，core 的恢复流程：

1. **State 直接可读**：sessions / tasks / agents 都是 current state，直接 SELECT 拿到最新值
2. **TaskQueue 重建**：扫 `tasks WHERE status IN ('PENDING','ACTIVE','SUSPENDED')`，按 dag_deps 拓扑排序填充内存队列
3. **CapabilityCache 重建**：扫 `agents WHERE status IN ('RUNNING','WAITING','IDLE')`，按每个 agent 的 (template_id, template_version, bound_capability_ids) 重新 resolve（capability 是软状态，安全重建）
4. **LoopGuard 已持久化**：context_tokens / context_message_count 等都在 agents 表，直接读
5. **In-flight LLM/Capability invocations 标记为失败**：扫 `capability_invocations WHERE status='RUNNING'`，标 `CANCELED + 'process_restart'`，对应的 task 走 retry 路径
6. **HITL 等待恢复**：`hitl_approvals WHERE status='PENDING'` 仍有效，等用户操作即可
7. **Event sequence 恢复**：每个 run_id 的下一个 sequence 取 `MAX(sequence) + 1`

第 5 步是关键——崩溃时正在跑的 LLM/tool 调用结果丢失，但因为我们有 task 状态机和 retry 机制，task 会被重新执行；event log 保留崩溃前的事件，便于事后分析。

#### V2 迁移到分布式

| 现状 | V2 改造 |
|------|--------|
| 单 Postgres 实例 | 主从 + read replica 用于 SSE 订阅查询 |
| events 单表 | 按 occurred_at 月分区；冷分区归档 S3 |
| TaskQueue 内存 | Redis Streams 替代，跨 worker 共享 |
| CapabilityCache 单进程 | Redis cache + 失效广播 |
| 单租户 | tenant_id 全表分区 + per-tenant RBAC |

---

## 9. Event 体系

### 9.1 Event 总览

事件是 ctx-weft 的中枢神经。所有 step 输出、所有状态变更、所有外部观察都通过 event 表达。

### 9.2 Event 基类

```python
@dataclass
class Event:
    id: str                              # ULID
    run_id: str                          # 唯一关联到某次 loop run
    session_id: str
    task_id: Optional[str]
    agent_id: Optional[str]
    type: str                            # 见下表
    timestamp: datetime
    sequence: int                        # 全局 run 内严格递增
    payload: dict
    causation_id: Optional[str] = None   # 引起此 event 的上游 event id
    metadata: dict = field(default_factory=dict)
```

### 9.3 完整 Event Type 索引

每个事件标注 reducer 类别，决定 Pure Replay（§7.5.3）行为：

- **S**（state-mutating）：对 LoopState 有状态变更，必须有 reducer
- **M**（measurement）：更新度量字段（context_tokens、token_used、turns_used 等）
- **T**（memory-tracking）：更新 memory 订阅 cursor / 标记 superseded 等
- **O**（observation-only）：仅供时间轴展示，reducer 为 no-op

#### Session / Run / Step 域

| Type | 触发时机 | Reducer | 关键 payload 字段 |
|------|---------|--------|----------------|
| `SessionCreated` | SessionManager.create_session 创建 session 记录后 | **S** | `goal`, `user_prompt`, `root_agent_id`, `token_budget`, `context_limit` |
| `SessionStatusChanged` | 任何 session.status 字段变更 | **S** | `from_status`, `to_status`, `reason` |
| `SessionFinished` | session 进入 SUCCEEDED / FAILED / TIMEOUT / CANCELED | **S** | `final_status`, `token_used`, `runtime_summary` |
| `SessionPausedHitl` | failure_counter 触达 threshold 自动暂停 | **S** | `failure_counter`, `pending_approval_ids` |
| `RunStarted` | LoopEngine.run 启动 | **S** | `run_id`, `initial_step` |
| `RunPaused` | RunHandle.pause() 在 step 边界生效 | **S** | `reason`, `paused_at_step` |
| `RunResumed` | RunHandle.resume() | **S** | `resumed_at_step` |
| `RunCanceled` | RunHandle.cancel() | **S** | `reason` |
| `RunFinished` | LoopEngine.run 结束 | **S** | `final_status`, `total_events`, `total_turns` |
| `StepStarted` | Step.execute 入口 | O | `step_name`, `step_index` |
| `StepCompleted` | Step.execute 返回 | O | `step_name`, `next_step`, `duration_ms` |
| `StepFailed` | Step.execute 抛异常 | **S** | `step_name`, `error_code`, `error_message` |

#### Task 域

| Type | 触发时机 | Reducer | 关键 payload 字段 |
|------|---------|--------|----------------|
| `TaskCreated` | TaskManager 创建 task（含子 task spawn） | **S** | `type`, `parent_task_id`, `creator_agent_id`, `assigned_agent_id`, `user_prompt`, `inputs`, `dag_deps`, `requires_approval`, `priority`, `settings` |
| `TaskStarted` | task 由 PENDING → ACTIVE | **S** | `assigned_agent_id` |
| `TaskSuspended` | control capability 触发挂起（submit_plan/submit_task） | **S** | `reason`, `subtask_ids` |
| `TaskResumed` | 所有子任务终态后 _try_resume_parent | **S** | `from_status`（SUSPENDED→ACTIVE） |
| `TaskFinished` | Observer 裁决 success → FinalizeStep 更新状态 | **S** | `outcome`, `summary`, `outputs` |
| `TaskFailed` | Observer 裁决 failed / Step 抛异常 | **S** | `error_code`, `error_message`, `retry_count` |
| `TaskCanceled` | replan / 显式 cancel | **S** | `reason` |
| `TaskFinalized` | FinalizeStep 完成（内含写 memory + publish blackboard） | O | `outcome`（汇总 task 终态） |

#### Agent 域

| Type | 触发时机 | Reducer | 关键 payload 字段 |
|------|---------|--------|----------------|
| `AgentInstantiated` | LifecycleManager.instantiate_agent 完成（§4.6.5） | **S** | `template_id`, `template_version`, `parent_agent_id`, `spawn_depth`, `bound_capability_ids`, `memory_config`, `loop_config` |
| `AgentSpawned` | spawn 子 agent 成功（_check_spawn_permission 通过） | **S** | `parent_agent_id`, `subtask_id` |
| `AgentStatusChanged` | agent.status 变更（IDLE/RUNNING/WAITING/FINISHED/FAILED） | **S** | `from_status`, `to_status` |
| `AgentWaiting` | agent 切 WAITING（等子 task 完成） | **S** | `waiting_for_task_ids` |
| `AgentFinalized` | sub-agent 完成被 _settle_executor 回收 | **S** | `final_status`, `final_turns_used` |
| `SpawnRejected` | _check_spawn_permission 拒绝（深度/并发/token 不足） | **S** | `reason`（depth_limit/concurrent_limit/token_low）, fallback_to_inline |

#### Context 域

| Type | 触发时机 | Reducer | 关键 payload 字段 |
|------|---------|--------|----------------|
| `ReasonCompleted` | ReasonStep 正常完成装配 | O | `estimated_tokens`, `assembled_token_count`, `blocks_used`, `blocks_dropped` |
| `ContextTokensEstimated` | ReasonStep 入口估算后 | O | `estimated_tokens`, `has_baseline` |
| `ContextTokensMeasured` | ActStep 末尾测量回写 loop_guard | **M** | `measured_tokens`（max prompt_tokens）, `message_count` |
| `ContextAssembled` | ContextAssembler.assemble 返回 | O | `purpose`, `block_count`, `total_tokens` |
| `ContextOverflowed` | BudgetStrategy 兜底失败抛错 | **S** | `total_tokens`, `limit` |

#### LLM 域

| Type | 触发时机 | Reducer | 关键 payload 字段 |
|------|---------|--------|----------------|
| `LLMRequestStarted` | LLMClient.complete 调用入口 | O | `request_id`, `model`, `prompt_tokens_estimate` |
| `LLMTokenStreamed` | 每个 LLM stream chunk | O | `request_id`, `delta`（token 增量文本） |
| `LLMResponseFinished` | LLMClient.complete 流结束 | **M**+**S** | `request_id`, `content`（完整 LLM 输出文本）, `tool_calls`（结构化 tool_call 列表）, `usage`（七字段拆分：prompt/completion/total/cache_read/cache_write/input/reasoning） — **payload 必须完整携带，replay 用** |
| `LLMRetryTriggered` | LLM 网络/格式错误后重试 | O | `request_id`, `attempt`, `error_code` |

> ⚠ `LLMResponseFinished` 是 replay 关键事件——payload 必须含**完整 content 和 tool_calls**，否则 replay 无法重建 ActStep 当时的 turn record。

#### Capability 域

| Type | 触发时机 | Reducer | 关键 payload 字段 |
|------|---------|--------|----------------|
| `CapabilityInvoked` | ActStep 调用 capability invoke 前 | O | `invocation_id`, `capability_id`, `capability_kind`, `arguments` |
| `CapabilityProgress` | provider.invoke 流中每个 progress/stdout/stderr | O | `invocation_id`, `event_kind`, `payload` |
| `CapabilityFinished` | provider.invoke 流正常结束 | **S** | `invocation_id`, `capability_id`, `content`（聚合 stdout）, `metadata`（含 task_suspended 等 control 信号） — **payload 必须完整，replay 用** |
| `CapabilityFailed` | provider.invoke 流 error event | **S** | `invocation_id`, `capability_id`, `error_code`, `error_message` |
| `CapabilityCanceled` | cancel_token 触发 + provider.cancel | **S** | `invocation_id`, `reason` |

#### ActStep / ObserveStep 子事件

| Type | 触发时机 | Reducer | 关键 payload 字段 |
|------|---------|--------|----------------|
| `ActTurnStarted` | ActStep 每轮 turn 开始 | O | `turn`, `agent_id` |
| `ActTurnCompleted` | ActStep 每轮 turn 结束 | O | `turn`, `reason`（normal/tool_calls_processed/suspended/max_turns） |
| `MaxTurnsReached` | ActStep 达 max_turns_per_act | **S** | `agent_id`, `max`, `turns_used` |
| `ObserveCompleted` | ObserveStep 完成（产出 verdict） | **S** | `task_id`, `outcome`（success/failed/needs_user_input）, `summary_length` |

#### Memory 域

| Type | 触发时机 | Reducer | 关键 payload 字段 |
|------|---------|--------|----------------|
| `MemoryIngested` | core 调 MemoryProvider.ingest 后 | **T** | `memory_event_type`（user_prompt/llm_response/...），`source`，`memory_event_id`（provider 返回的 id） |
| `CompactTriggered` | ReasonStep 入口判定需 compact | O | `reason`（token_ratio/message_delta）, `estimated_tokens` |
| `MemoryCompactStarted` | CompactStep 入口 | O | `agent_id` |
| `MemoryCompacted` | CompactStep 成功 | **T** | `events_before`, `events_after`, `summary_tokens`, `summary_event_id`, `fallback`（bool） |
| `MemoryCompactFailedFallback` | Compact LLM 失败，走机械截断 | O | `error` |
| `BlackboardSubscribed` | session 订阅 topic | **T** | `session_id`, `topic`, `intent`, `priority` |

#### HITL 域

| Type | 触发时机 | Reducer | 关键 payload 字段 |
|------|---------|--------|----------------|
| `HitlRequired` | capability 触发 HITL 拦截 | **S** | `approval_id`, `task_id`, `invocation_id`, `capability_id`, `arguments`, `trigger_reason`, `expire_at` |
| `HitlApproved` | 用户 approve | **S** | `approval_id`, `decided_by`, `decided_at` |
| `HitlRejected` | 用户 reject | **S** | `approval_id`, `decided_by`, `reason`, `decided_at` |
| `HitlModified` | 用户 modify 后 approve | **S** | `approval_id`, `decided_by`, `modified_arguments`, `decided_at` |
| `HitlTimeout` | 超时未操作 | **S** | `approval_id`, `expire_at` |

#### Guard 域

| Type | 触发时机 | Reducer | 关键 payload 字段 |
|------|---------|--------|----------------|
| `TokenBudgetWarning` | token_used ≥ token_budget × 90% | **M** | `token_used`, `token_budget`, `ratio` |
| `TokenBudgetExceeded` | token_used ≥ token_budget | **S** | `token_used`, `token_budget` |
| `FailureThresholdHit` | failure_counter ≥ failure_threshold | **S** | `failure_counter`, `failure_threshold` |
| `MaxConcurrentAgentsExceeded` | spawn 时 concurrent_agents 已达上限 | O | `current_count`, `limit`, `attempted_spawn` |

#### Provider 域

| Type | 触发时机 | Reducer | 关键 payload 字段 |
|------|---------|--------|----------------|
| `MCPServerDisconnected` | MCPCapabilityProvider 连接断开 | O | `provider_name`, `last_error` |
| `MCPServerReconnected` | 重连成功 | O | `provider_name`, `attempts` |
| `RemoteSkillSyncCompleted` | RemoteSkillCapabilityProvider 同步完成 | O | `source`, `skills_added`, `skills_updated`, `skills_removed` |
| `RemoteSkillSyncFailed` | 同步失败 | O | `source`, `error` |

#### System / 元事件

| Type | 触发时机 | Reducer | 关键 payload 字段 |
|------|---------|--------|----------------|
| `EventsDropped` | EventBus 订阅者 queue 溢出，丢弃旧事件 | O | `subscriber_id`, `dropped_count`, `first_dropped_id`, `last_dropped_id` |
| `SnapshotCreated` | EventStore 写入 snapshot | O | `snapshot_id`, `last_event_sequence`, `reason` |

### 9.4 Replay 关键事件 payload Schema 详解

下面对**所有 reducer 类别为 S 的事件**给出精确 payload schema（用 TypedDict 表达）。这些是 Pure Replay（§7.5）的硬契约——payload 字段不可在不升版本号的情况下增删。

#### 9.4.1 Session / Run 事件

```python
class SessionCreatedPayload(TypedDict):
    goal: str
    user_prompt: str
    status: str                          # "QUEUED"
    root_agent_id: Optional[str]         # 创建时可能尚未 instantiate
    token_budget: int
    context_limit: int
    config: dict


class SessionStatusChangedPayload(TypedDict):
    from_status: str
    to_status: str
    reason: Optional[str]
    occurred_at: str                     # ISO-8601


class SessionFinishedPayload(TypedDict):
    final_status: str                    # SUCCEEDED/FAILED/TIMEOUT/CANCELED
    token_used: int
    runtime_summary: dict                # 任意结构化统计


class RunStartedPayload(TypedDict):
    run_id: str
    initial_step: str                    # "reason"


class RunPausedPayload(TypedDict):
    reason: str                          # user_request/hitl/single_step/...
    paused_at_step: str                  # 当前 step 名


class RunResumedPayload(TypedDict):
    resumed_at_step: str


class RunCanceledPayload(TypedDict):
    reason: str


class RunFinishedPayload(TypedDict):
    final_status: str
    total_events: int
    total_turns: int


class StepFailedPayload(TypedDict):
    step_name: str
    error_code: str
    error_message: str
```

#### 9.4.2 Task 事件

```python
class TaskCreatedPayload(TypedDict):
    type: str                            # reasoning/tool-call/sub-agent/skill/hitl
    parent_task_id: Optional[str]
    creator_agent_id: str
    assigned_agent_id: Optional[str]     # 创建时可能未分配
    user_prompt: Optional[str]
    inputs: dict
    dag_deps: list[str]
    requires_approval: bool
    priority: int
    settings: dict                       # skill_name/working_dir/use_subagent/...


class TaskStartedPayload(TypedDict):
    assigned_agent_id: str


class TaskSuspendedPayload(TypedDict):
    reason: str                          # spawn_plan / spawn_task / hitl
    subtask_ids: list[str]               # 派生的子 task id 列表（spawn 路径）


class TaskResumedPayload(TypedDict):
    from_status: str                     # "SUSPENDED"
    triggered_by: str                    # "all_subtasks_terminal"


class TaskFinishedPayload(TypedDict):
    outcome: str                         # "success"
    summary: str                         # verdict.summary
    outputs: Optional[Any]               # 多模态 list 或 str


class TaskFailedPayload(TypedDict):
    error_code: str
    error_message: str
    retry_count: int


class TaskCanceledPayload(TypedDict):
    reason: str                          # replan/user_cancel/parent_failed
```

#### 9.4.3 Agent 事件

```python
class AgentInstantiatedPayload(TypedDict):
    template_id: str
    template_version: str
    parent_agent_id: Optional[str]
    spawn_depth: int
    bound_capability_ids: list[str]      # resolve 后的完整列表
    memory_config: dict                  # snapshot
    loop_config: dict                    # snapshot
    context_limit: int                   # 实例化时从 LLMClient 拷贝


class AgentSpawnedPayload(TypedDict):
    parent_agent_id: str
    subtask_id: str


class AgentStatusChangedPayload(TypedDict):
    from_status: str
    to_status: str


class AgentWaitingPayload(TypedDict):
    waiting_for_task_ids: list[str]


class AgentFinalizedPayload(TypedDict):
    final_status: str                    # FINISHED/FAILED
    final_turns_used: int


class SpawnRejectedPayload(TypedDict):
    reason: str                          # depth_limit/concurrent_limit/token_low
    fallback_to_inline: bool
    attempted_subtask_id: Optional[str]
```

#### 9.4.4 LLM / Capability 事件（replay 数据载体）

这两个事件的 payload 是 replay 引擎重建 ActStep 内部 turn record 的依据，**必须携带完整数据**：

```python
class LLMResponseFinishedPayload(TypedDict):
    request_id: str
    content: str                         # 完整 LLM 文本输出
    tool_calls: list[dict]               # 结构化 tool_call 列表：
                                         #   [{id, name, arguments}]
    usage: LLMUsageDict                  # 七字段拆分，见下
    finish_reason: str                   # "stop" / "tool_use" / "length" / ...


class LLMUsageDict(TypedDict):
    prompt_tokens: int        # 总输入（含缓存读/写；跨 provider 归一口径）
    completion_tokens: int    # 全部输出
    total_tokens: int         # prompt + completion
    cache_read_tokens: int    # 缓存命中
    cache_write_tokens: int   # 缓存写入（Anthropic cache_creation；OpenAI 系恒 0）
    input_tokens: int         # 实际未缓存输入（= prompt − read − write）
    reasoning_tokens: int     # 输出中的推理子集


class CapabilityFinishedPayload(TypedDict):
    invocation_id: str
    capability_id: str
    capability_kind: str                 # tool/skill/agent
    content: str                         # 聚合后的输出文本（stdout）
    metadata: dict                       # 含 task_suspended / subtask_ids 等 control 信号
    duration_ms: int


class CapabilityFailedPayload(TypedDict):
    invocation_id: str
    capability_id: str
    error_code: str
    error_message: str
    duration_ms: int


class CapabilityCanceledPayload(TypedDict):
    invocation_id: str
    reason: str
```

#### 9.4.5 Context / Step 事件

```python
class ContextOverflowedPayload(TypedDict):
    total_tokens: int
    limit: int
    purpose: str                         # act/observe/compact
    blocks_present: int


class MaxTurnsReachedPayload(TypedDict):
    agent_id: str
    max: int
    turns_used: int


class ObserveCompletedPayload(TypedDict):
    task_id: str
    outcome: str                         # success/failed/needs_user_input
    summary_length: int
```

#### 9.4.6 HITL 事件

```python
class HitlRequiredPayload(TypedDict):
    approval_id: str
    task_id: str
    invocation_id: Optional[str]
    capability_id: Optional[str]
    arguments: Optional[dict]
    trigger_reason: str                  # explicit/failure_threshold/tool_risk
    expire_at: str                       # ISO-8601


class HitlApprovedPayload(TypedDict):
    approval_id: str
    decided_by: str
    decided_at: str


class HitlRejectedPayload(TypedDict):
    approval_id: str
    decided_by: str
    reason: str
    decided_at: str


class HitlModifiedPayload(TypedDict):
    approval_id: str
    decided_by: str
    modified_arguments: dict
    decided_at: str


class HitlTimeoutPayload(TypedDict):
    approval_id: str
    expire_at: str
```

#### 9.4.7 Guard 事件

```python
class TokenBudgetExceededPayload(TypedDict):
    token_used: int
    token_budget: int


class FailureThresholdHitPayload(TypedDict):
    failure_counter: int
    failure_threshold: int
    last_failed_task_id: Optional[str]
```

### 9.5 Measurement & Memory-tracking 事件 payload

```python
# Measurement
class ContextTokensMeasuredPayload(TypedDict):
    measured_tokens: int                 # 取多 turn 中最大 prompt_tokens
    message_count: int                   # 测量时的消息数（用作下一轮 baseline）


class TokenBudgetWarningPayload(TypedDict):
    token_used: int
    token_budget: int
    ratio: float                         # 0.9 等


# Memory-tracking
class MemoryIngestedPayload(TypedDict):
    memory_event_type: str               # user_prompt/llm_response/tool_invocation/...
    memory_event_id: str                 # provider 返回的 id
    source: str                          # 调用点标识（task_prompt/observer_verdict/act_step_*）
    content_length: Optional[int]


class MemoryCompactedPayload(TypedDict):
    events_before: int
    events_after: int
    summary_tokens: int
    summary_event_id: str                # provider 写入的 COMPACT_SUMMARY 事件 id
    fallback: bool                       # True=机械截断（LLM 失败）


class BlackboardSubscribedPayload(TypedDict):
    session_id: str
    topic: str
    intent: str                          # parent_child/long_term_background/long_term_project_log
    priority: int
```

### 9.6 Observation-only 事件简表

下表事件 reducer 为 no-op，仅参与时间轴展示与实时 streaming（SSE）。payload 字段仅供参考，可在 minor 版本灵活变更（不影响 replay）：

| Type | 关键字段 | 备注 |
|------|---------|------|
| `StepStarted` | step_name, step_index | 时间轴标记 |
| `StepCompleted` | step_name, next_step, duration_ms | 时间轴标记 |
| `ReasonCompleted` | estimated_tokens, assembled_token_count | 装配可观测 |
| `ContextTokensEstimated` | estimated_tokens, has_baseline | 装配可观测 |
| `ContextAssembled` | purpose, block_count, total_tokens | 装配可观测 |
| `LLMRequestStarted` | request_id, model | 时间轴标记 |
| `LLMTokenStreamed` | request_id, delta | 流式 UI 用 |
| `LLMRetryTriggered` | request_id, attempt, error_code | 调试用 |
| `CapabilityInvoked` | invocation_id, capability_id, capability_kind, arguments | 时间轴 + 实时 UI |
| `CapabilityProgress` | invocation_id, event_kind, payload | 流式 UI |
| `ActTurnStarted` / `ActTurnCompleted` | turn, reason | 时间轴 |
| `TaskFinalized` | outcome | 时间轴 |
| `CompactTriggered` | reason, estimated_tokens | 时间轴 |
| `MemoryCompactStarted` | agent_id | 时间轴 |
| `MemoryCompactFailedFallback` | error | 调试 |
| `MCPServerDisconnected` / `Reconnected` | provider_name | 运维监控 |
| `RemoteSkillSyncCompleted` / `Failed` | source, counts | 运维监控 |
| `MaxConcurrentAgentsExceeded` | current_count, limit | 调试 |
| `EventsDropped` | subscriber_id, dropped_count | 背压告警 |
| `SnapshotCreated` | snapshot_id, last_event_sequence | 持久化诊断 |

### 9.7 Event 版本演化策略

Replay 引擎依赖 payload schema 稳定。当 schema 需要演化时：

| 变更类型 | 兼容性 | 策略 |
|---------|------|------|
| **新增可选字段** | ✅ 向前兼容 | 直接加，旧 reducer 会忽略，新 reducer 用默认值兜底 |
| **新增必需字段** | ❌ 破坏 replay | 走 event versioning：发新 type `TaskCreated.v2` 或在 payload 加 `_schema_version`；reducer 注册多版本 |
| **删除字段** | ❌ 破坏 replay | 软删除：保留字段但标记 deprecated；reducer 容忍 None；至少跨两个 release 才物理删 |
| **重命名字段** | ❌ 破坏 replay | 等同于"加新字段 + 删旧字段"，两步走 |
| **改字段类型** | ❌ 破坏 replay | 走 event versioning |

**实践规则**：
- 所有 `S` 类事件 payload 视为对外契约，破坏性变更需要走版本号
- `O` / `M` / `T` 类事件 payload 演化空间较大，但仍建议向前兼容

**Schema 版本号字段**：

```python
@dataclass
class Event:
    ...                                  # 基础字段（§9.2）
    schema_version: int = 1              # payload 版本；reducer 据此分支
```

V1 全部 schema_version=1，V2 引入 v2 时 reducer 注册形如：

```python
REDUCERS["TaskCreated.v1"] = _reduce_task_created_v1
REDUCERS["TaskCreated.v2"] = _reduce_task_created_v2
```

### 9.8 Event Bus

```python
class EventBus(Protocol):
    async def emit(self, event: Event) -> None: ...
    def subscribe(
        self,
        event_type: str | None,
        handler: Callable[[Event], Awaitable[None]],
    ) -> SubscriptionHandle: ...
    async def stream(
        self,
        filter: EventFilter,
    ) -> AsyncIterator[Event]: ...
```

V1 内置 `InProcessEventBus`（基于 asyncio.Queue），V2 切 `RedisStreamsEventBus`。

---

## 10. Streaming 数据流

### 10.1 三处 Streaming

1. **LLM 流式输出**：LLMClient 返回 `AsyncIterator[LLMChunk]`，每个 chunk 即时 emit 为 `LLMTokenStreamed` 事件
2. **Capability 流式输出**：CapabilityProvider.invoke 本身是 `AsyncIterator[CapabilityEvent]`
3. **Events 流式订阅**：RunHandle.events() 返回 `AsyncIterator[Event]`，host 层封装为 SSE

### 10.2 背压 / Buffer

- EventBus 每个订阅者有独立 queue（默认 size=1000）
- 慢消费者超容时**丢弃旧事件**而非阻塞 loop（保证 loop 不被订阅者拖死）
- 丢弃事件时 emit 一个 `EventsDropped` 元事件，提醒消费者数据有缺口

### 10.3 端到端示例（用户视角）

```python
async with CtxWeftRuntime(config) as runtime:
    runtime.providers.register_knowledge(MyKnowledge())
    runtime.providers.register_memory(MyMemory())
    runtime.providers.register_capability(MyTools())

    handle = await runtime.start_session(goal="...", user_prompt="...")

    async for event in handle.events():
        if event.type == "LLMTokenStreamed":
            print(event.payload["delta"], end="")
        elif event.type == "TaskFinished":
            print(f"\n[Task {event.task_id} done]")
```

---

## 11. LLM 适配层

### 11.1 LLMClient 协议

```python
class LLMClient(Protocol):
    async def complete(
        self,
        request: LLMRequest,
        stream: bool = True,
    ) -> AsyncIterator[LLMChunk]: ...

    async def count_tokens(self, text: str) -> int: ...

    @property
    def context_limit(self) -> int: ...

    @property
    def supports_tool_calling(self) -> bool: ...
```

### 11.2 LLMRequest 统一格式

```python
@dataclass
class LLMRequest:
    model: str
    system: str
    messages: list[LLMMessage]
    tools: list[LLMTool]
    max_tokens: int
    temperature: float
    metadata: dict          # trace_id / run_id / 计费标签
```

Adapter（OpenAI / Anthropic / 其他）负责把统一格式转换为各家 API。tool_calling 格式归一化：core 看到的永远是统一的 `tool_use_block`，由 adapter 双向转换。

### 11.3 Cost / Token 统计

每次 LLMChunk 携带 token 增量；LLMClient 在请求结束后 emit `LLMResponseFinished` 事件，包含总 token 数。Guard 订阅此事件更新 session.token_used。

---

## 12. Service Shell（CtxWeft.host）

### 12.1 定位

CtxWeft.host 是 core 的部署外壳，把 core 包装成 HTTP/SSE 服务。host 不引入新概念，只做协议转换。

### 12.2 主要接口（REST）

| Method | Path | 说明 |
|--------|------|------|
| POST | `/api/v1/sessions` | 创建 session |
| GET | `/api/v1/sessions/{id}` | 取 session 详情 |
| GET | `/api/v1/sessions/{id}/runs` | 列出运行历史 |
| POST | `/api/v1/sessions/{id}/cancel` | 取消会话 |
| GET | `/api/v1/runs/{run_id}/events` | 分页事件 |
| GET | `/api/v1/runs/{run_id}/stream` | SSE 事件流 |
| POST | `/api/v1/runs/{run_id}/pause` | 暂停 |
| POST | `/api/v1/runs/{run_id}/resume` | 恢复 |
| POST | `/api/v1/runs/{run_id}/cancel` | 取消 |
| POST | `/api/v1/runs/{run_id}/replay` | 重放到指定 event |
| POST | `/api/v1/hitl/{approval_id}/approve|reject|modify` | HITL 操作 |
| POST | `/api/v1/providers/knowledge` | 注册一个 knowledge provider 配置 |
| POST | `/api/v1/providers/memory` | 注册 memory provider 配置 |
| POST | `/api/v1/providers/capability` | 注册 capability provider 配置 |

### 12.3 持久化适配

host 层负责实例化具体的 StateStore / EventStore 实现。V1 提供：

- `PostgresStateStore` + `PostgresEventStore`
- 配置在 `LoomConfig.persistence`

### 12.4 鉴权

JWT / API Key，挂在 ASGI middleware，与 core 解耦。所有请求带 `tenant_id` 注入 `ProviderContext`。

---

## 13. 包结构与模块边界

### 13.1 顶层包

```
CtxWeft/
├── core/                       ← 纯 runtime，pip install ctx-weft
│   ├── orchestrator/
│   │   ├── session_manager.py
│   │   ├── task_manager.py
│   │   ├── lifecycle_manager.py     # instantiate_agent / spawn / settle
│   │   ├── capability_cache.py       # 实例化时 resolve 的 capability 快照
│   │   └── task_queue.py
│   ├── loop/
│   │   ├── driver.py
│   │   ├── steps/
│   │   │   ├── reason.py
│   │   │   ├── act.py
│   │   │   ├── observe.py
│   │   │   ├── finalize.py
│   │   │   └── suspend.py
│   │   └── guard.py
│   ├── assembler/
│   │   ├── assembler.py
│   │   ├── sources/
│   │   │   ├── identity.py            # 读 template.identity[purpose]
│   │   │   ├── capability.py          # 读 CapabilityCache，purpose 过滤
│   │   │   ├── short_memory.py
│   │   │   ├── blackboard.py
│   │   │   ├── long_memory.py
│   │   │   ├── knowledge.py
│   │   │   └── task_spec.py
│   │   ├── budget.py
│   │   └── composer.py
│   ├── control/
│   │   ├── run_handle.py
│   │   ├── tokens.py            # CancelToken / PauseToken
│   │   └── replay.py
│   ├── events/
│   │   ├── bus.py
│   │   └── types.py
│   ├── llm/
│   │   ├── client.py
│   │   └── types.py
│   ├── auth/
│   │   └── authorizer.py        # AllowListAuthorizer 默认实现
│   └── state/
│       ├── store.py             # StateStore / EventStore protocol
│       └── models.py            # Agent / Session / Task dataclass
│
├── protocols/                  ← 协议层，pip install CtxWeft-protocols
│   ├── knowledge.py
│   ├── memory.py                 # MemoryProvider（统一：ingest + recall_recent/topic/semantic + subscribe + apply_compact）
│   ├── capability.py             # Capability / CapabilityProvider / Purpose（无 identity）
│   ├── template.py               # AgentTemplate / IdentityFacet / TemplateResolver / CapabilityRef
│   └── context.py                # ProviderContext
│
├── host/                       ← Service Shell，pip install ipmastercowork
│   ├── api/
│   ├── template_registry/        # 实现 TemplateResolver；解析 SOUL/ROLE markdown
│   │   ├── resolver.py
│   │   └── markdown_parser.py
│   ├── persistence/
│   │   └── postgres/
│   ├── llm_adapters/
│   │   ├── openai.py
│   │   └── anthropic.py
│   └── cli.py
│
└── providers/                  ← V1 参考实现（详见 §4.7）
    ├── knowledge_file/
    ├── memory_blackboard/        # StructuredBlackboardMemoryProvider（core 默认；纯 Postgres，无 pgvector）
    ├── capability_builtin/       # BuiltinToolsCapabilityProvider（bash/http/file/glob）
    ├── capability_mcp/           # MCPCapabilityProvider（stdio/http transport）
    ├── capability_skill_local/   # LocalSkillCapabilityProvider
    └── capability_skill_remote/  # RemoteSkillCapabilityProvider + GitSyncer/HttpSyncer/OciSyncer
    # 注：外部 memory provider（Mem0/LightRAG/Zep）不在此目录——用户按需自接入
```

### 13.2 依赖方向（严格单向）

```
host ─► core ─► protocols
providers ─► protocols
```

core **不允许** import host / providers；protocols **不允许** import 任何其他 CtxWeft 包。

### 13.3 测试切片

- core 用 mock provider 全覆盖（不依赖任何外部系统）
- protocols 只有契约测试（pytest-asyncio + fakes）
- providers 各自独立测试，且必须通过 protocols 提供的契约测试套件
- host 起 in-memory core + sqlite 跑集成测试

---

## 14. 关键数据契约（精确签名）

> 本节给出 V1 必须冻结的接口签名，作为代码生成 / mock / 契约测试的依据。其余 dataclass 见 §4 / §5。

### 14.1 LoomConfig

```python
@dataclass
class LoomConfig:
    runtime: RuntimeConfig
    persistence: PersistenceConfig
    llm: LLMConfig
    guard: GuardConfig
    logging: LoggingConfig

@dataclass
class RuntimeConfig:
    max_concurrent_runs: int = 16
    step_timeout_sec: int = 120
    default_max_turns: int = 20
    checkpoint_every_n_events: int = 50

@dataclass
class GuardConfig:
    default_token_budget: int = 200_000
    default_context_limit: int = 180_000
    default_failure_threshold: int = 3
```

### 14.2 CtxWeftRuntime 顶层 API

```python
class CtxWeftRuntime:
    def __init__(
        self,
        config: LoomConfig,
        template_resolver: TemplateResolver,   # 由 host 注入；core 通过此读取 AgentTemplate
    ) -> None: ...
    async def __aenter__(self) -> "CtxWeftRuntime": ...
    async def __aexit__(self, *exc) -> None: ...

    @property
    def providers(self) -> ProviderRegistry: ...

    async def start_session(
        self,
        goal: str,
        user_prompt: str,
        agent_template_id: str,                # 由 LifecycleManager 通过 TemplateResolver 解析
        template_version: str | None = None,   # None=最新；指定则 pin 该版本
        config: SessionConfig | None = None,
    ) -> RunHandle:
        """启动 session。内部流程见 §4.6.7 完整端到端流程。"""

    async def resume_session(self, session_id: str) -> RunHandle:
        """从持久化状态恢复 session；CapabilityCache 软状态重建（按 agent.template_id + template_version 重新 resolve）。"""
```

### 14.3 事件类型常量

V1 冻结 §9.3 列出的事件类型。**新增事件必须经过协议升级**，不允许业务代码随便发新类型——保证 replay 与下游消费者的稳定。

---

## 15. V1 范围 / V2 展望

### 15.1 V1 必交付

1. `ctx-weft`：完整的 orchestrator + loop engine + context assembler + control plane + event bus
2. `CtxWeft-protocols`：三协议的抽象定义 + 契约测试套件
3. `ipmastercowork`：FastAPI 服务壳 + Postgres 持久化 + SSE 流
4. 文档：本设计文档 + 用户指南 + provider 开发指南
5. 至少一组参考 provider 实现（够跑 end-to-end demo）

### 15.2 V1 不做的

- 多进程分布式（TaskQueue 分布式化）
- 多语言 SDK
- 多租户严格隔离（API 层支持租户 header，但 core 不做强隔离）
- 实时 multi-agent 协作（多个独立 root agent）
- Auto-dream / 自动整理项目背景

### 15.3 V2 展望

| 方向 | 大致改造点 |
|------|----------|
| 多进程 | EventBus → Redis Streams；TaskQueue 引入分布式锁 |
| 全量 Event Sourcing | `apply_event` 实现从"先 state 后 event"切换到"先 event 后投影" |
| TS SDK | 协议 → JSON Schema；core 关键算法剥离到 WASM / 重写 |
| 实时 multi-agent | SessionManager 支持 root_agent_list；MemoryProvider 加 sibling 互订阅 topic |
| Cost / Plan 优化 | BudgetStrategy 增加学习型 / 成本驱动型实现 |
| Effective Replay | RunHandle.replay 支持 fork-and-rerun（从历史点改参数重跑） |
| 外部 LongMemory 标杆集成 | 提供 Mem0 / LightRAG / Zep 的官方 adapter |

---

## 16. 设计决议（原 TBD）

v0.1 文档列出的 5 个 TBD 在 v0.2 中决议如下：

1. **AgentTemplate 的归属** ✓ 决议：**放在 host**。
   - core 只持有 `template_id` 引用；template 的内容（SOUL/ROLE/SKILL.md 等文件解析）由 host 完成
   - core 与 template 的耦合点仅在 `AgentTemplateIdentityProvider`（一个 CapabilityProvider 实现）——它能根据 template_id 返回对应的 identity capability
   - 这样 core 不需要知道 markdown 解析、frontmatter、版本号语义等细节

2. **PolicyEngine 位置** ✓ 决议：**放在 core**，作为 `Authorizer` 接口。
   - Authorizer 是 core 内的协议，default 实现是 `AllowListAuthorizer`（基于 agent.config.allowed_capabilities）
   - 在 capability invoke 前由 LoopEngine 调用，拒绝时抛 `CapabilityNotAuthorized`
   - V1 实现双阶段授权（act / observe 不同允许清单），等价于 miniAgents 的 PolicyEngine 行为
   - Provider 不参与鉴权决策，保持 provider 简洁

3. **HITL Approval 持久化** ✓ 决议：**单独表 `hitl_approvals`**。
   - 查询模式（"列出 pending"、"按 task_id 查"、"超时扫描"）与 event log 完全不同，专表更合适
   - event log 仍记录 `HitlRequired` / `HitlApproved` 等事件用于审计追溯，但权威状态在 hitl_approvals 表

4. **跨 session 长期记忆** ✓ 决议（v0.14 修正）：**Blackboard 和 LongMemory 是同一概念**，统一在 `MemoryProvider` 协议下。
   - core ship 默认实现 `StructuredBlackboardMemoryProvider`（纯 Postgres）：支持 recall_recent + recall_topic 订阅
   - 外部可替换为 Mem0/LightRAG/Zep/自建 RAG：在默认能力之上提供 recall_semantic 等更强检索
   - core 通过 `ingest()` 把所有重要事件（user_prompt/llm_response/tool_invocation/tool_result/observer_summary/compact_summary）喂给 provider，provider 决定如何内化
   - 详见 §4.3
   - V1 单租户时 topic 命名空间共享；多租户时由 host 在 topic 前缀加 tenant_id 隔离

5. **Provider 配置的热更新** ⚠ 仍开放：
   - V1 推荐：新 session 才用新 provider；运行中 session 不切换 provider 实例
   - 配置类参数（如 RAG 的 top_k）支持 per-call override via ProviderContext
   - V2 考虑：provider 实现一个 `reload(new_config)` 钩子，运行中可热替换

---

## 附录 A：与 miniAgents 的概念映射

| miniAgents 概念 | ctx-weft 概念 | 说明 |
|----------------|---------------|------|
| Reasoner / Actor / Observer | ReasonStep / ActStep / ObserveStep | 拆为 Step（无独立 Plan-step，规划是 Act 内部行为） |
| ReasoningContext | ContextRequest + ContextBlock[] + AssembledPrompt | 三层拆分 |
| MemoryService（所有部分）+ BlackboardService + pgvector memories | **MemoryProvider（统一协议：ingest + recall_recent/topic/semantic）** | Blackboard 与 LongMemory 合一；core 默认 StructuredBlackboard 实现，外部 Mem0/LightRAG 实现 recall_semantic |
| BlackboardService | MemoryProvider.{publish,pull,subscribe}_blackboard | 并入 Memory；升级为跨 session 长期记忆通道 |
| ToolRegistry / SkillRegistry | CapabilityProvider × N | 合并 |
| **SOUL.md / ROLE.md** | **AgentTemplate.identity（dict[Purpose, IdentityFacet]，identity["act"]=SOUL / identity["observe"]=ROLE）** | identity 是 template 一等字段，独立于 Capability 协议 |
| **act_tool_list / observe_tool_list** | **Capability.purposes 字段（["act"] / ["observe"] / ["act","observe"]）** | 双阶段工具区分由 provider 声明 purposes 驱动 |
| **BACKGROUND.md** | **MemoryProvider blackboard topic `project:*:background`** | 归位为长期记忆；进入 messages |
| AgentTemplate (元数据) | host 层管理；core 持 template_id 引用 | host 负责 template 解析与注册 |
| TaskQueue (内存 LIFO) | TaskQueue (核心保留，含 DAG) | 复用思路 |
| SessionManager / TaskManager / LifecycleManager | 同名，在 `CtxWeft.core.orchestrator/` | 复用思路，去耦合 |
| AgentLoop | LoopEngine (Step Driver) | 重写 |
| Control Tools (submit_plan/submit_task/...) | 特殊 Capability + Step 路由；purposes 明确 | 协议化 |
| Event Bus | EventBus 协议 + InProcess 实现 | 抽象化 |
| HitlApproval | HitlWaitStep + 单独 hitl_approvals 表 | step 化；持久化分表 |
| PolicyEngine | `Authorizer` 接口（core 内）+ purpose 过滤 | 双层过滤：purpose 决定可见性，Authorizer 决定权限 |

---

> *ctx-weft Design Doc · v0.1 · 2026-05-16 · 待与作者评审*
