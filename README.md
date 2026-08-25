# ctx-weft

**Protocol-based Agent Runtime Core** — 用于构建可控、可观测 AI Agent 应用的 Python SDK。

ctx-weft 只做一件事：把外部系统（知识库、记忆系统、能力系统、LLM、模板）通过协议接入，驱动 LLM 完成任务。它不包含任何数据库代码、HTTP 服务或具体 LLM 实现——这些由上层应用（host）提供。

> 本文是**使用参考**。想了解循环引擎、任务编排、崩溃恢复等**内部实现逻辑**，见 [ARCHITECTURE.md](./ARCHITECTURE.md)。

---

## 目录

- [安装](#安装)
- [核心概念](#核心概念)
- [快速上手](#快速上手)
- [三大协议](#三大协议)
  - [MemoryProvider](#memoryprovider)
  - [CapabilityProvider](#capabilityprovider)
  - [KnowledgeProvider](#knowledgeprovider)
- [AgentTemplate 与 AgentCapabilityProvider](#agenttemplate-与-agentcapabilityprovider)
- [LLM 接入](#llm-接入)
- [CtxWeftRuntime API](#ctxweftruntime-api)
- [事件系统](#事件系统)
- [内置 Provider](#内置-provider)
- [控制面](#控制面)
- [自定义 Provider](#自定义-provider)
- [在测试中使用](#在测试中使用)
- [限制与约束](#限制与约束)

---

## 安装

```bash
# 核心（零额外依赖）
pip install ctx-weft

# 含内置工具（bash / http / file）
pip install "ctx-weft[builtin]"

# 含 MCP bridge
pip install "ctx-weft[mcp]"

# 含远程 skill 同步
pip install "ctx-weft[skills]"

# 开发环境（pytest + ruff + mypy）
pip install "ctx-weft[dev]"
```

Python ≥ 3.11 required（用到 `StrEnum` / `datetime.UTC` / `asyncio.timeout`）。

顶层包导出：

```python
from ctx_weft import (
    CtxWeftRuntime, ProviderRegistry, RunHandle, SessionStartParams, InMemoryEventStore,
    TaskSettings, NormalTaskSettings, CompactTaskSettings, MetadataFillerTaskSettings,
)
# 测试辅助单独放在 ctx_weft.testing（不污染生产 API）：
from ctx_weft.testing import MockLLMAdapter, MockResponse, ToolCall
```

---

## 核心概念

```
AgentTemplate        定义 Agent 的身份（SOUL/ROLE）和能力引用
       ↓
CtxWeftRuntime        顶层 API，连接所有组件
  ├── ProviderRegistry        注册 Memory / Capability / Knowledge / LLM
  ├── AgentCapabilityProvider 读取 AgentTemplate（由上层实现或用内置 LocalAgentTemplateProvider）
  └── LLMClient               LLM 调用接口（由上层实现）
       ↓
Loop Engine          reason → act → observe → finalize
       ↓
EventBus             事件总线，所有状态变更的唯一出口
```

这些**协议**是对外的接入面。外部系统只需实现其中一个协议，ctx-weft 自动用上（LLM 详见
[LLM 接入](#llm-接入) 一节）：

| 协议 | 用途 | Context 落位 | 数量 |
|------|------|-------------|------|
| `MemoryProvider` | 记忆（短期对话 / 长期 / blackboard） | messages | **唯一，必需** |
| `CapabilityProvider` | Agent 可调用的动作（tool / skill / sub-agent） | 首条 user message 前缀 + LLM tools | 多个 |
| `KnowledgeProvider` | 动态检索的参考资料（RAG / Wiki） | messages | 多个 |
| `LLMClient` / `LLMClientResolver` | LLM 调用 adapter / 多账号 provider | — | **唯一，必需** |

全部协议（含数据类型）均从 `ctx_weft.protocols` 导出。

> 引擎如何把这些协议拼成 prompt、如何驱动 Step、如何编排子任务，见 [ARCHITECTURE.md](./ARCHITECTURE.md)。

---

## 快速上手

最小可运行示例（使用内置 `MockLLMAdapter`，不需要真实 LLM）：

```python
import asyncio
from ctx_weft import CtxWeftRuntime, ProviderRegistry
from ctx_weft.testing import MockLLMAdapter, MockResponse
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider
from ctx_weft.protocols import (
    AgentCapability, AgentCapabilityProvider, AgentTemplate,
    CapabilityProviderInfo, IdentityFacet, LoopConfig, MemoryConfig,
)


# 1. 实现一个最简 AgentCapabilityProvider——模板进入 core 的唯一通道
#    （发现与加载同源，spec 2026-07-22）。真实项目通常直接用内置
#    ctx_weft.providers.agent_template_local.LocalAgentTemplateProvider 扫描模板目录。
class DictAgentTemplateProvider(AgentCapabilityProvider):
    name = "agent"

    def __init__(self, templates: dict[str, AgentTemplate]):
        self._t = templates

    async def list(self, ctx) -> list[AgentCapability]:
        return [
            AgentCapability(id=f"{self.name}:{t.id}", name=t.id, template_name=t.id)
            for t in self._t.values()
        ]

    async def get_template(self, template_id, version, ctx) -> AgentTemplate | None:
        return self._t.get(template_id)

    async def describe(self, ctx) -> CapabilityProviderInfo:
        return CapabilityProviderInfo(name=self.name, capability_count=len(self._t))


# 2. 定义 AgentTemplate（Agent 的身份蓝图）
template = AgentTemplate(
    id="my_agent", name="My Agent", version="1.0.0",
    identity={
        "act": IdentityFacet(text="You are a helpful assistant."),
        "observe": IdentityFacet(text="Evaluate if the task is complete."),
    },
    capability_refs=[],                 # 暂不绑定外部工具
    memory_config=MemoryConfig(),
    loop_config=LoopConfig(),
)

# 3. 组装 Runtime
providers = ProviderRegistry()
providers.register_memory(InMemoryMemoryProvider())
providers.register_capability(DictAgentTemplateProvider({"my_agent": template}))

runtime = CtxWeftRuntime(
    llm=MockLLMAdapter(responses=[MockResponse(text="The answer is 42.")]),
    providers=providers,
)

# 4. 运行一个任务：template_id 须为规范形式 provider:name——本 provider 注册的
#    前缀是 "agent"（DictAgentTemplateProvider.name），故 "my_agent" → "agent:my_agent"。
async def main():
    handle, state = await runtime.run_single_task(
        template_id="agent:my_agent",
        user_prompt="What is the meaning of life?",
    )
    print(state.verdict.summary)        # "The answer is 42."

asyncio.run(main())
```

---

## 三大协议

### MemoryProvider

**唯一的 memory 抽象**——承载短期对话窗口、长期记忆、blackboard 父子通信。

```python
from ctx_weft.protocols import (
    MemoryProvider, MemoryEvent, MemoryEventType,
    MemoryScope, MemoryRecord, ProviderContext,
)
```

核心接口（全部 `async`）：

```python
class MemoryProvider(Protocol):
    name: str

    # 写入（core 在每个关键节点自动调用）
    async def ingest(self, event: MemoryEvent, ctx: ProviderContext) -> str: ...

    # 读取（三种模式）
    async def recall_recent(self, scope, types, limit, ctx) -> list[MemoryRecord]: ...
    async def recall_topic(self, topic, since, ctx) -> tuple[list[MemoryRecord], int]: ...
    async def recall_semantic(self, query, scope, top_k, ctx) -> list[MemoryRecord]: ...

    # topic 订阅（跨 session 长期上下文）
    async def subscribe_topic(self, session_id, topic, intent, ctx) -> str: ...
    async def list_subscriptions(self, session_id, ctx) -> list[Subscription]: ...

    # 压缩（超出 context 时触发）
    async def apply_compact(self, scope, summary, keep_last, ctx) -> CompactResult: ...

    # 工具
    async def count_recent(self, scope, types, ctx) -> int: ...
    async def describe(self, ctx) -> MemoryProviderInfo: ...
```

**MemoryEventType** — core 自动 ingest 的事件类型：`USER_PROMPT` / `LLM_RESPONSE` /
`TOOL_INVOCATION` / `TOOL_RESULT` / `OBSERVER_SUMMARY` / `COMPACT_SUMMARY` / `BLACKBOARD_PUBLISH`
（每种类型由 core 在何时写入，见 [ARCHITECTURE.md §记忆写入时机](./ARCHITECTURE.md#记忆写入时机)）。

**注册方式**：

```python
providers.register_memory(InMemoryMemoryProvider())   # 唯一槽位，重复注册即覆盖
```

### CapabilityProvider

**Agent 的可调用动作**——tool、skill、sub-agent 三类。

```python
from ctx_weft.protocols.capability import (
    Capability, ToolCapability, SkillCapability, AgentCapability,
    CapabilityEvent, ToolCapabilityProvider,
)
```

`CapabilityProvider` 基类只声明三个方法；`ToolCapabilityProvider` 额外要求 `invoke()` / `cancel()`：

```python
class CapabilityProvider(ABC):
    name: str
    async def list(self, ctx) -> list[Capability]: ...
    async def retrieve(self, ctx) -> list[Capability]: ...   # 默认回落到 list()
    async def describe(self, ctx) -> CapabilityProviderInfo: ...

class ToolCapabilityProvider(CapabilityProvider):
    def invoke(self, capability_id, arguments, ctx) -> AsyncIterator[CapabilityEvent]: ...
    async def cancel(self, invocation_id, ctx) -> None: ...
```

**Capability** 数据结构：

```python
@dataclass
class ToolCapability(Capability):
    id: str            # 全局唯一，如 "builtin:bash_exec" / "mcp:fs:read_file"
    name: str          # LLM tool_call 时用的短名
    kind: str = "tool"
    purposes: list[Purpose]   # ["act"] / ["observe"] / ["act", "compact"]
    description: str = ""
    input_schema: dict = {}   # JSON Schema
    side_effects: bool = False
```

**CapabilityEvent** — invoke 流式返回：`kind ∈ {"progress","stdout","stderr","result","error"}`；
最终结果放 `payload.content`，错误放 `payload.code` + `payload.message`。

**Purpose 过滤** — core 在每个阶段只向 LLM 暴露对应 purpose 的 capability：

| Purpose | 对应 Step | 典型工具 |
|---------|-----------|---------|
| `"act"` | ActStep | bash_exec / http_request / submit_task |
| `"observe"` | ObserveStep | submit_task_assessment / request_human_input |
| `"compact"` | CompactStep | read_file / glob |
| `"metadata_filler"` | MetadataFillerStep | update_task_metadata |

**注册方式**（可注册多个）：

```python
providers.register_capability(BuiltinToolsCapabilityProvider())
providers.register_capability(MCPCapabilityProvider(config))
providers.deregister_capability("builtin")   # 按 provider.name 注销
```

### KnowledgeProvider

**动态检索的参考资料**——RAG、Wiki、API 文档。只读，不接受写入。

```python
from ctx_weft.protocols import KnowledgeProvider, KnowledgeQuery, KnowledgeDoc

class KnowledgeProvider(Protocol):
    name: str
    async def retrieve(self, query: KnowledgeQuery, ctx) -> AsyncIterator[KnowledgeDoc]: ...
    async def describe(self, ctx) -> KnowledgeProviderInfo: ...
```

**KnowledgeQuery**：`text` / `intent`（"policy" / "reference" / "api_spec" / ...）/ `top_k=5` / `filters`。

**注册方式**（可注册多个，priority 升序，数字小=先查询）：

```python
providers.register_knowledge(MyRAGProvider(), priority=0)
providers.register_knowledge(WikiProvider(), priority=10)
```

---

## AgentTemplate 与 AgentCapabilityProvider

### AgentTemplate

Agent 实例化的蓝图，由上层（host 应用或用户代码）构建：

```python
from ctx_weft.protocols import (
    AgentTemplate, IdentityFacet, CapabilityRef, MemoryConfig, LoopConfig,
)

template = AgentTemplate(
    id="research_agent", name="Research Agent", version="2.1.0",  # semver

    # Identity：按 purpose 分面（act / observe / compact / metadata_filler）
    identity={
        "act":     IdentityFacet(text="You are a research agent. Be thorough and cite sources."),
        "observe": IdentityFacet(text="Evaluate whether the research task is complete."),
    },

    # 外部 capability 引用，mode: optional / required / forbidden
    capability_refs=[
        CapabilityRef(capability_id="builtin:http_request", mode="optional"),
        CapabilityRef(capability_id="control:submit_task",  mode="required"),
    ],

    memory_config=MemoryConfig(
        short_window_size=20,       # 最近多少条历史进 prompt
        summary_threshold=20,       # 超过多少条触发 compact
        use_long_term=True,
        subscribed_blackboard_topics=[],
    ),

    loop_config=LoopConfig(
        max_turns_per_act=10,       # 单个 ActStep 最多多少轮 LLM 调用
        max_turns_per_observe=5,
        max_turns_per_agent=20,
        timeout_per_step_sec=120,
        failure_threshold=3,
        max_spawn_depth=4,          # 子 agent 最大嵌套深度
        compact_token_ratio=0.8,    # token 占 context_limit 比例超过则压缩
        compact_message_delta=20,   # 距上次 compact 累积多少条再触发
        compact_keep_last=6,        # compact 后保留最近多少条
    ),
)
```

### AgentCapabilityProvider

模板进入 core 的**唯一通道**是 `AgentCapabilityProvider`（spec 2026-07-22 方案 B）：
发现（`list`）与加载（`get_template`）同源，`register_capability` 直接注册即可，无需
额外适配器。

推荐直接使用内置的目录扫描实现——根目录下每个含 `SOUL.md` 的子目录即一个模板：

```python
from pathlib import Path
from ctx_weft.providers.agent_template_local import LocalAgentTemplateProvider

providers.register_capability(LocalAgentTemplateProvider(Path("resources/templates")))
runtime = CtxWeftRuntime(providers=providers)
```

也可以自己实现协议（比如从数据库/远端注册表读取）：

```python
from ctx_weft.protocols import AgentCapability, AgentCapabilityProvider, AgentTemplate, CapabilityProviderInfo

class MyAgentTemplateProvider(AgentCapabilityProvider):
    name = "agent"

    def __init__(self, store: dict[str, AgentTemplate]):
        self._store = store

    async def list(self, ctx) -> list[AgentCapability]:
        return [
            AgentCapability(id=f"{self.name}:{t.id}", name=t.id, template_name=t.id,
                            description=t.metadata.get("description", ""), version=t.version)
            for t in self._store.values()
        ]

    async def get_template(self, template_id, version, ctx) -> AgentTemplate | None:
        return self._store.get(template_id)

    async def describe(self, ctx) -> CapabilityProviderInfo:
        return CapabilityProviderInfo(name=self.name, capability_count=len(self._store))
```

> `AgentCapabilityProvider` 缺省 `retrieve()` 返回 `[]`：全目录只经 `list()` 暴露给
> required-ref 精确查找，永不自动召回——sub-agent 只经模板声明的 `subagents` 绑定。

---

## LLM 接入

ctx-weft 只定义 `LLMClient` 协议，不包含任何真实 LLM SDK。协议与全部数据类型都在
`ctx_weft.protocols`（与 Memory / Capability / Knowledge 同层）：

```python
from ctx_weft.protocols import (
    # 写一个 LLM adapter 需要的全部类型：
    LLMClient,                         # 要实现的协议
    LLMRequest, LLMMessage, LLMTool,   # 读：complete() 入参
    LLMChunk, ToolCall, LLMUsage,      # 产：complete() yield 的流式 chunk
    LLMCallError,                      # 错误/重试契约
    # 仅多账号 provider 需要：
    LLMClientResolver,
)
```

有两种接法。

### 方式 A：直接传一个 LLMClient（`llm=`）

适合脚本/测试、单一模型：

```python
providers = ProviderRegistry()
providers.register_capability(my_agent_capability_provider)
runtime = CtxWeftRuntime(providers=providers, llm=my_adapter)
```

`LLMClient` 协议：

```python
class LLMClient(Protocol):
    @property
    def context_limit(self) -> int: ...
    @property
    def output_reserve(self) -> int: ...   # 输入侧输出预留（→ reserved_output_tokens）
    @property
    def supports_tool_calling(self) -> bool: ...
    def complete(self, request: LLMRequest, stream: bool = True) -> AsyncIterator[LLMChunk]: ...
    async def count_tokens(self, text: str) -> int: ...
```

`LLMChunk.kind ∈ {"token","reasoning","tool_call","tool_call_partial","usage","done"}`。
自定义 Adapter 见 [自定义 Provider](#自定义-provider)。

### 方式 B：多账号 LLMProvider（推荐用于服务端）

`LLMProvider` 实现 `LLMClientResolver`，管理多账号 + 多模型，支持持久化与环境变量自举：

```python
from ctx_weft.providers.llm import LLMProvider, LLMAccount, ModelConfig
# 真实 adapter（需 [llm] extra）/ 测试 adapter 也都从同一入口取：
# from ctx_weft.providers.llm import AnthropicAdapter, OpenAIAdapter, MockLLMAdapter

provider = LLMProvider(store)          # store 实现 LLMAccountStoreProtocol(save/delete/list_all)
provider.register_account(LLMAccount(
    name="claude", style="anthropic",  # 仅支持 "anthropic" / "openai"
    api_key="sk-...", base_url="",      # base_url 留空走各家默认
    models=[ModelConfig(name="claude-sonnet-4-6", context_limit=200_000)],  # output_reserve 缺省=按窗口尺寸
    default_model="claude-sonnet-4-6", timeout_sec=120,
))
provider.load_from_store()             # 从持久化恢复
runtime.providers.register_llm_provider(provider)
```

账号/模型管理：`register_account` / `delete_account` / `get_account` / `list_accounts` /
`is_registered` / `add_model` / `remove_model` / `set_default_model` / `get_client(account, model)`。

> SDK core 不读环境变量。从环境变量自举一个默认账号（`bootstrap_from_env`）由上层应用
> 在子类中实现（host 的 `LLMProvider` 子类读自己约定前缀的环境变量）。

> 内置 `AnthropicAdapter` / `OpenAIAdapter` 由 `LLMProvider` 按 `style` 自动构造。
> `run_single_task` / `SessionStartParams` 里的 `llm_account` / `llm_model` 会透传给 `get_client()`。

---

## CtxWeftRuntime API

`CtxWeftRuntime` 是使用 ctx-weft 的唯一入口。

### 构造

```python
# 模板进入 core 的唯一通道是 AgentCapabilityProvider——构造前先注册（发现与加载同源）；
# registry 里一个 AgentCapabilityProvider 都没有会在构造期抛 ValueError（fail-fast）。
providers = providers or ProviderRegistry()
providers.register_capability(LocalAgentTemplateProvider(Path("resources/templates")))

runtime = CtxWeftRuntime(
    providers=providers,              # 必需含至少一个 AgentCapabilityProvider
    llm=my_llm_adapter,               # 可选，LLM 兜底（未注册 llm provider 时用）
    hitl_manager=None,                # 可选，默认自动创建
    event_store=None,                 # 可选，默认 InMemoryEventStore（自动订阅 event_bus）
)
```

构造时 **ControlCapabilityProvider**（内置控制工具）与 **SkillExecutorCapabilityProvider**
会**自动注册**，无需手动添加。常用属性：`runtime.event_bus` / `runtime.event_store` /
`runtime.hitl_manager` / `runtime.providers`。

运行/控制入口共 **5 种**：

| 方法 | 用途 | 返回 |
|------|------|------|
| `run_single_task(...)` | 单任务端到端，**等待完成** | `(RunHandle, LoopState)` |
| `start_session(params)` | 新建会话，多 agent 编排，**异步后台跑** | `RunHandle` |
| `start_session(params)`（带 `session_id`） | 恢复已有会话续跑 | `RunHandle` |
| `recover()` / `recover_session(id)` | 进程重启后的崩溃恢复 | `int` / `None` |
| `interrupt_session(id)` | 协作式取消正在跑的 session | `bool` |

### run_single_task() — 单任务直跑

跑一个任务，**await 直到完成**再返回。适合脚本、CLI、单测：

```python
handle, state = await runtime.run_single_task(
    template_id="agent:my_agent",    # 必需；规范形式 provider:name（见下方 AgentCapabilityProvider）
    user_prompt="总结这份文档：...",   # 必需
    session_id=None,                 # 可选，不传则自动生成 ses_xxx
    tenant_id="default",
    llm_account=None, llm_model=None,
)
print(state.task.status)                     # "FINISHED" / "FAILED" / "CANCELED"
print(state.verdict.task_outcome)            # "success" / "failed"
print(state.verdict.summary)                 # Observer 总结
print(state.transcript[-1].assistant_text)   # 最后一轮 LLM 回复
```

### start_session() — 新建会话（完整模式）

支持多 agent 树、`submit_task` 派生子任务、suspend/resume、compact。入参封装在
**`SessionStartParams`**，用 `.create()` 构造；`start_session` **只接受这一个参数**：

```python
from ctx_weft import SessionStartParams

params = SessionStartParams.create(
    template_id="agent:planner_agent",  # 必需；规范形式 provider:name
    user_prompt="研究并撰写量子计算报告", # 必需
    session_id=None,                    # None=新建；传 id=恢复（见下）
    initial_task=None,                  # 可选，dict → 反序列化为 TaskSettings（见下）
    tenant_id="default",
    llm_account=None, llm_model="claude-sonnet-4-6",
    token_budget=200_000,
)
handle = await runtime.start_session(params)   # 立即返回，任务在后台 drain
final_state = await handle.wait_for_finish(timeout=300.0)
```

**恢复已有会话**：给 `session_id` 传已存在的 id 即进入 resume，runtime 从事件存储重建
`root_agent_id`，把新 `user_prompt` 作为后续输入续跑：

```python
params = SessionStartParams.create(
    template_id="agent:planner_agent",
    user_prompt="补充一节关于纠错码的内容",
    session_id="ses_01HXXXX",          # 复用已有 session
)
handle = await runtime.start_session(params)
```

**TaskSettings**（`initial_task` dict 按 `_type` 反序列化，默认 `NormalTaskSettings`）：

```python
initial_task = {
    "_type": "NormalTaskSettings",   # 缺省即此类型
    "skill_name": "",                # 绑定某个 skill 作为本任务上下文
    "use_subagent": False,           # True → 用子 agent 执行
    "subagent_template": "",         # 子 agent 模板 id
    "inherit_memory": True,          # 子任务是否继承父 agent 近期记忆
    "purpose": "act",
}
```
`CompactTaskSettings` / `MetadataFillerTaskSettings` 仍保留为类型（反序列化兼容），但已不再作为独立 task 调度：
compact 由 ReasonStep 命中阈值后内联直调 CompactStep；metadata_filler 由后台协程直跑在 root task 上。

### 崩溃恢复

进程重启后救活之前无终态的 session，**在所有 provider 注册完成、接收新请求之前**调用：

```python
count = await runtime.recover()
# recover() 直接查 EventStore（无需 host 投影表）据事件决策、且**无回调、不 drain**:
#   · 有未解决 pending HITL 的 session → 只 rebuild 内存 HitlManager,保持 PAUSED_HITL,等应答;
#   · 其余（崩溃前在跑）→ core emit SessionStatusChanged(INTERRUPTED),host 既有事件订阅者
#     （投影/SSE）按事件自行反映,等用户 /resume。
# 故 load_sessions_from_db 须排在 recover() 之后,从更新后的投影把状态灌回内存缓存。返回处理的 session 数
```

恢复流程的内部细节见 [ARCHITECTURE.md §会话生命周期与崩溃恢复](./ARCHITECTURE.md#会话生命周期与崩溃恢复)。

### RunHandle

```python
@dataclass
class RunHandle:
    run_id: str
    session_id: str
    task_id: str
    agent_id: str
    template_id: str
    event_bus: EventBus

    async def events(self) -> AsyncIterator[Event]: ...        # 流式订阅本 run 全部事件
    async def wait_for_finish(self, timeout: float = 300.0) -> LoopState | None: ...
```

---

## 事件系统

所有状态变更都通过事件总线发布，append-only。

### Event 结构

```python
@dataclass
class Event:
    id: str               # evt_ULID
    run_id: str | None
    sequence: int         # 在同一 run_id 内单调递增
    session_id: str
    type: str             # 见 EVENT_TYPES（冻结清单）
    timestamp: datetime
    tenant_id: str = "default"
    task_id: str | None
    agent_id: str | None
    payload: dict         # 按 type 不同而异
    metadata: dict
    causation_id: str | None
    schema_version: int
```

### 常用事件类型

事件类型在 `ctx_weft.core.events.types.EVENT_TYPES` 中**冻结**（业务代码不得发未登记类型）：

- **Run/Step**：`RunStarted` `RunFinished` `RunCanceled` `RunPaused` `RunResumed` `StepStarted` `StepCompleted` `StepFailed`
- **Session**：`SessionCreated` `SessionStatusChanged` `SessionFinished` `SessionPausedHitl`
- **LLM**：`LLMRequestStarted` `LLMTokenStreamed` `LLMResponseFinished`
- **Capability**：`CapabilityStarted` `CapabilityProgress` `CapabilityFinished`
- **Task**：`TaskStarted` `TaskFinished` `TaskFailed` `TaskSuspended` `TaskCanceled`
- **Memory**：`MemoryIngested` `MemoryCompactionStarted` `MemoryCompacted`

`RunFinished.payload` 含 `final_status` / `will_retry` / `total_events` / `total_turns` / `error` / `error_type`。

### 订阅事件

```python
from ctx_weft.core.events.types import EventFilter

# 用 handle 订阅本 run
async for ev in handle.events():
    if ev.type == "LLMTokenStreamed":
        print(ev.payload.get("delta", ""), end="", flush=True)
    elif ev.type == "RunFinished":
        break

# 用 event_bus 自定义过滤（session / run / task / types）
async for ev in runtime.event_bus.stream(EventFilter(session_id="ses_xxx")):
    ...
```

---

## 内置 Provider

### InMemoryMemoryProvider

零依赖，用于开发/测试：

```python
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider
providers.register_memory(InMemoryMemoryProvider())
# recall_recent ✓ / recall_topic ✓ / recall_semantic ✗（返空）；线程不安全，仅单进程/测试
```

### BuiltinToolsCapabilityProvider

需要 `pip install "ctx-weft[builtin]"`：

```python
from pathlib import Path
from ctx_weft.providers.capability_builtin import (
    BuiltinToolsCapabilityProvider, BuiltinToolsConfig,
)

providers.register_capability(BuiltinToolsCapabilityProvider(
    BuiltinToolsConfig(allowed_dirs=[Path("/workspace")])   # 文件操作限定目录；空列表=不限制
))
```

> `BuiltinToolsConfig` **只有 `allowed_dirs` 一个字段**。bash 黑名单、命令超时、HTTP SSRF
> 防护、响应大小上限等都是 provider 内置常量，不通过 config 开关。

提供的 capability：

| ID | Purpose | 描述 |
|----|---------|------|
| `builtin:bash_exec` | act | 执行 shell（黑名单含 rm/sudo/curl 等；30s 超时） |
| `builtin:http_request` | act | HTTP 请求（仅 http/https；内网地址 SSRF 拦截；剥离敏感 header） |
| `builtin:read_file` | act, compact | 读文件（≤500KB） |
| `builtin:write_file` | act | 写文件（自动建父目录） |
| `builtin:edit_file` | act | 原地替换（old_string→new_string；要求唯一匹配或 replace_all） |
| `builtin:glob` | act, compact | 列出匹配文件（按文件名） |
| `builtin:grep` | act, compact | 按内容正则搜索（ripgrep 优先，回退纯 Python；files/content 两种输出模式） |

### MCPCapabilityProvider

桥接任意 MCP server，需要 `pip install "ctx-weft[mcp]"`：

```python
from ctx_weft.providers.capability_mcp import MCPCapabilityProvider, MCPServerConfig

# stdio transport（启动子进程）
providers.register_capability(MCPCapabilityProvider(MCPServerConfig(
    name="filesystem", transport="stdio",
    command=["npx", "-y", "@modelcontextprotocol/server-filesystem", "/workspace"],
    env={}, default_purposes=["act"], timeout_per_call_sec=60, connect_timeout_sec=10,
)))

# http / streamable_http transport
providers.register_capability(MCPCapabilityProvider(MCPServerConfig(
    name="my_service", transport="http",
    url="http://localhost:3000", headers={"Authorization": "Bearer xxx"},
    capability_purpose_override={"some_tool": ["observe"]},  # 单工具 purpose 覆盖
)))
# provider.name = "mcp:<name>"，cap.id = "mcp:<name>:<tool>"
```

### LocalSkillCapabilityProvider

从本地目录加载 SKILL.md：

```python
from pathlib import Path
from ctx_weft.providers.capability_skill_local import LocalSkillCapabilityProvider
providers.register_capability(LocalSkillCapabilityProvider(Path("./skills")))
# 目录结构：skills_dir/<skill_name>/SKILL.md（+ 可选 references/、scripts/）
# capability id 前缀 local_skill:
```

### RemoteSkillCapabilityProvider

从 Git 仓库同步 SKILL.md，需要 `pip install "ctx-weft[skills]"`：

```python
from pathlib import Path
from ctx_weft.providers.capability_skill_remote import (
    RemoteSkillCapabilityProvider, GitSkillSyncer,
)

syncer = GitSkillSyncer(repo_url="https://github.com/org/skills",
                        cache_dir=Path("./skills_cache"), branch="main")
providers.register_capability(RemoteSkillCapabilityProvider(
    source_name="github",          # 必填：决定 provider.name 与 cap.id 前缀
    cache_dir=Path("./skills_cache"),
    syncer=syncer,
))
# name = "remote_skill_github"，cap.id = "remote_skill_github:<skill>"
# 首次 list()/load_definition() 触发 sync；reset_sync() 强制下次重新同步
```

### 内置控制 Capability（ControlCapabilityProvider）

由 `CtxWeftRuntime` 自动注册，无需手动添加。向 LLM 暴露：

| capability id | purpose | 描述 |
|--------------|---------|------|
| `control:submit_task` | act | 派生子 task，当前 agent 挂起等待 |
| `control:submit_plan` | act | 一次提交多个并行/串行子 task |
| `control:replan` | act | 修订当前计划 |
| `control:submit_task_assessment` | observe | Observer 报告 task 结果 |
| `control:update_task_metadata` | metadata_filler | 回填 title / description / session goal |
| `control:request_human_input` | act, observe | 请求人工介入（触发 HITL） |

---

## 控制面

### 取消正在运行的 session

```python
ok = runtime.interrupt_session("ses_01HXXXX")
# True = 找到并已发取消信号；False = 已结束 / 未注册（run_single_task 不注册 cancel token）
# 被取消时发 RunCanceled + TaskCanceled，任务状态置 CANCELED
```

### HITL 人工审批

当 `request_human_input` 等触发人工介入时，发 `SessionPausedHitl` 事件并暂停。上层通过
`runtime.hitl_manager` 应答：

```python
runtime.hitl_manager.approve(request_id, response="同意，继续")
runtime.hitl_manager.reject(request_id, reason="不允许该操作")
```

### 鉴权（Authorizer）

给敏感 capability 挂鉴权器，在 invoke 前拦截：

```python
from ctx_weft.core.auth import (
    Authorizer, AllowAllAuthorizer, AllowListAuthorizer, HumanConfirmationAuthorizer,
)

# 方式 ①：注册 capability 时传入
providers.register_capability(
    BuiltinToolsCapabilityProvider(),
    tool_authorizers={
        "builtin:bash_exec": HumanConfirmationAuthorizer(hitl_manager=runtime.hitl_manager),
    },
)

# 方式 ②：事后单独设置（key 为 provider_name 或完整 capability_id）
providers.set_capability_authorizer(
    "builtin:bash_exec",
    HumanConfirmationAuthorizer(hitl_manager=runtime.hitl_manager),
)
```

---

## 自定义 Provider

### 自定义 MemoryProvider

需实现全部抽象方法（`ingest` / `recall_recent` / `recall_topic` / `recall_semantic` /
`subscribe_topic` / `list_subscriptions` / `apply_compact` / `count_recent` / `describe`）：

```python
from ctx_weft.protocols import (
    MemoryProvider, MemoryEvent, MemoryEventType, MemoryScope, MemoryRecord,
    MemoryProviderInfo, CompactResult, Subscription, ProviderContext,
)

class MyMemoryProvider(MemoryProvider):
    name = "my_memory"

    async def ingest(self, event, ctx) -> str: ...
    async def recall_recent(self, scope, types, limit, ctx) -> list[MemoryRecord]: ...
    async def recall_topic(self, topic, since, ctx) -> tuple[list[MemoryRecord], int]: ...
    async def recall_semantic(self, query, scope, top_k, ctx) -> list[MemoryRecord]: ...
    async def subscribe_topic(self, session_id, topic, intent, ctx) -> str: ...
    async def list_subscriptions(self, session_id, ctx) -> list[Subscription]: ...
    async def apply_compact(self, scope, summary, keep_last, ctx) -> CompactResult: ...
    async def count_recent(self, scope, types, ctx) -> int: ...
    async def describe(self, ctx) -> MemoryProviderInfo:
        return MemoryProviderInfo(name=self.name, supports_semantic=True, supports_topic=True)
```

### 自定义 ToolCapabilityProvider

```python
from collections.abc import AsyncIterator
from ctx_weft.protocols.capability import (
    ToolCapabilityProvider, Capability, ToolCapability,
    CapabilityEvent, CapabilityProviderInfo,
)
from ctx_weft.protocols import ProviderContext

class DatabaseProvider(ToolCapabilityProvider):
    name = "database"

    async def list(self, ctx) -> list[Capability]:
        return [ToolCapability(
            id="database:query", name="database_query", purposes=["act"],
            description="Run a read-only SQL query.",
            input_schema={"type": "object",
                          "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
            side_effects=False,
        )]

    def invoke(self, capability_id, arguments, ctx) -> AsyncIterator[CapabilityEvent]:
        return self._run(arguments)

    async def _run(self, args) -> AsyncIterator[CapabilityEvent]:
        try:
            rows = await self._db.execute(args["sql"])
            yield CapabilityEvent(kind="result",
                                  payload={"content": str(rows), "metadata": {"rows": len(rows)}})
        except Exception as e:
            yield CapabilityEvent(kind="error", payload={"code": "QUERY_FAILED", "message": str(e)})

    async def cancel(self, invocation_id, ctx) -> None: ...
    async def describe(self, ctx) -> CapabilityProviderInfo:
        return CapabilityProviderInfo(name=self.name, capability_count=1)
```

> 还可继承 `SkillCapabilityProvider`（`load_definition` / `list_files` / `load_resource` /
> `exec_script`）或 `AgentCapabilityProvider`（发现与加载同源：`list()` 返回 `AgentCapability`，
> `get_template()` 按 cap.id 前缀路由加载对应 `AgentTemplate`——模板进入 core 的唯一通道）。

### 自定义 KnowledgeProvider

```python
from collections.abc import AsyncIterator
from ctx_weft.protocols import (
    KnowledgeProvider, KnowledgeQuery, KnowledgeDoc, KnowledgeProviderInfo, ProviderContext,
)

class VectorSearchProvider(KnowledgeProvider):
    name = "vector_search"

    async def retrieve(self, query, ctx) -> AsyncIterator[KnowledgeDoc]:
        for r in await self._index.search(query.text, top_k=query.top_k):
            yield KnowledgeDoc(id=r.id, content=r.text, score=r.score, source=self.name)

    async def describe(self, ctx) -> KnowledgeProviderInfo:
        return KnowledgeProviderInfo(name=self.name)
```

### 自定义 LLMAdapter

```python
from collections.abc import AsyncIterator
from ctx_weft.protocols import LLMClient, LLMChunk, LLMRequest, LLMUsage

class MyLLMAdapter(LLMClient):
    @property
    def context_limit(self) -> int: return 128_000
    @property
    def output_reserve(self) -> int: return 4096
    @property
    def supports_tool_calling(self) -> bool: return True

    def complete(self, request, stream=True) -> AsyncIterator[LLMChunk]:
        return self._stream(request)

    async def _stream(self, request) -> AsyncIterator[LLMChunk]:
        async for c in my_api.stream(request.system, request.messages):
            yield LLMChunk(kind="token", text=c.text)
        yield LLMChunk(kind="usage", usage=LLMUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150))
        yield LLMChunk(kind="done", finish_reason="stop")

    async def count_tokens(self, text: str) -> int:
        return len(text) // 4
```

---

## 在测试中使用

### MockLLMAdapter

```python
from ctx_weft.testing import MockLLMAdapter, MockResponse, ToolCall

# 纯文本，按队列依次返回
llm = MockLLMAdapter(responses=[MockResponse(text="pong")])

# 返回 tool call，下一条是工具结果后的回复
llm = MockLLMAdapter(responses=[
    MockResponse(text="", tool_calls=[ToolCall(id="c1", name="bash_exec",
                                               arguments={"command": "ls"})]),
    MockResponse(text="done"),
])

# 断言收到的请求
_, state = await runtime.run_single_task(template_id="agent:echo", user_prompt="ping")
assert llm.last_request.system.startswith("You are")
```

### 完整集成测试示例

```python
import pytest
from ctx_weft import CtxWeftRuntime, ProviderRegistry
from ctx_weft.testing import MockLLMAdapter, MockResponse
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider
from ctx_weft.protocols import (
    AgentCapability, AgentCapabilityProvider, AgentTemplate,
    CapabilityProviderInfo, IdentityFacet, LoopConfig, MemoryConfig,
)

@pytest.fixture
def echo_template():
    return AgentTemplate(
        id="echo", name="Echo", version="1.0.0",
        identity={"act": IdentityFacet(text="You are a helpful echo agent.")},
        capability_refs=[], memory_config=MemoryConfig(), loop_config=LoopConfig(),
    )

@pytest.fixture
def runtime(echo_template):
    class Provider(AgentCapabilityProvider):
        name = "agent"
        async def list(self, ctx) -> list[AgentCapability]: return []
        async def get_template(self, tid, version, ctx): return echo_template
        async def describe(self, ctx): return CapabilityProviderInfo(name=self.name)
    providers = ProviderRegistry()
    providers.register_memory(InMemoryMemoryProvider())
    providers.register_capability(Provider())
    return CtxWeftRuntime(
        llm=MockLLMAdapter([MockResponse(text="pong")]),
        providers=providers,
    )

@pytest.mark.asyncio
async def test_single_task(runtime):
    handle, state = await runtime.run_single_task(template_id="agent:echo", user_prompt="ping")
    assert state.verdict.task_outcome == "success"
    assert state.transcript[-1].assistant_text == "pong"
```

---

## 升级须知（多模态 Phase 3a）

- **破坏性变更：多模态会话默认失败，除非显式声明视觉能力。** 升级到本版本后，
  任何携带图片（`ImagePart`）的会话会以 `VisionNotSupportedError`
  （`error_code=VISION_NOT_SUPPORTED`）失败，**除非**在传给
  `register_llm_provider` / `llm=` 的 `ModelConfig`（或等价的 LLM client 对象）上
  显式设置 `supports_vision=True`。这是刻意的严格默认：未声明视觉能力的模型一律
  视为不支持图片，防止图片 block 被静默发给 text-only 模型导致 provider 400。
  纯文本会话不受影响，行为逐字节不变。
  若你的宿主此前把图片喂给了任意模型（不管它是否真的支持视觉），升级后需要
  逐个 model 显式标注 `supports_vision=True` 才能继续工作。

## 限制与约束

- **单进程**：EventBus 是进程内实现，不跨进程。多进程需替换为 Redis Streams 等外部总线。
- **MemoryProvider 单实例**：同一个 `ProviderRegistry` 只能注册一个 MemoryProvider。
- **LLM 单解析器**：`register_llm_provider` 是唯一槽位；或用 `llm=` 兜底，二者皆无则运行时报错。
- **core 不做 I/O**：`ctx_weft` 内部无文件读写、无网络请求（providers 是外部的）。
- **默认单租户**：V1 默认 `tenant_id="default"`，多租户需在上层处理。
- **Python ≥ 3.11**。

---

> 更深入的内部实现（循环引擎、Step 流水线、上下文装配、任务编排、崩溃恢复）见 [ARCHITECTURE.md](./ARCHITECTURE.md)。
