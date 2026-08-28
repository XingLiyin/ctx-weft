# 协议层划界——event 体系入 protocols

> 状态：设计已批准（2026-08-27），§2 划界经用户裁定改为三层（2026-08-28），待实施
> 性质：**纯重构**，与多模态特性正交。配套特性设计见
> `2026-08-27-dual-blob-store-design.md`，该特性依赖本次搬迁先落地。

---

## 1. 问题

`EventStore` 是 host **必须**实现的契约——`core/state/event_store.py` 的模块 docstring
第一句就是「host 必须实现 append + read_by_session」。但它住在 `core/state/` 下，而
同样是 host-facing 契约的 `MemoryProvider` / `LLMClient` / `CapabilityProvider` 全在
`protocols/` 下。

这个不一致有可观测的后果：

- `EventStore` 本身**既没有**从 `ctx_weft` 顶层导出、**也没有**从 `protocols` 导出。
  host 要按它编程只能 `from ctx_weft.core.state.event_store import EventStore`——
  从一个名叫 `core` 的包里 import 自己要实现的接口。
- 顶层只导出了 `InMemoryEventStore`（一个**实现**），契约反而藏着。
- 新增 `EventBlobStore`（见配套特性设计）时无处可放：跟 `EventStore` 同处则继续
  留在 core，放 `protocols/` 则与它服务的契约分居。

判据一旦写清楚，归属就不再含糊——见 §2 的三层划界。

## 2. 划界

判据是**三层**（用户裁定 2026-08-28）：

> **契约进 `protocols/`，实现进 `providers/`，`core/` 只留编排。**

原先只分「host-facing 契约 vs core 自用」两层，把内置实现也归给 core。那不对称：
`providers/` 本就是「协议的具体实现」所在（`memory_blackboard` / `memory_sql` /
`llm` / `capability_*` 都是），唯独 event 体系的两个实现住在 `core/` 下，是历史惯性。

| 符号 | 现居 | 判据 | 去向 |
|---|---|---|---|
| `Event` / `EventFilter` | `core/events/types.py` | host 要构造、要持久化 | **protocols** |
| `EventType` / `EVENT_TYPES` | 同上 | V1 冻结清单，host 按它分派 | **protocols** |
| `TRANSIENT_EVENT_TYPES` | 同上 | host 实现 `append` 时据此跳过瞬态事件 | **protocols** |
| `EventBus` / `SubscriptionHandle` | `core/events/bus.py` | host 可替换（README 明说要换 Redis Streams） | **protocols** |
| `EventStore` / `RunSnapshot` | `core/state/event_store.py` | host 必须实现 | **protocols** |
| `TASK_STATUS_BY_EVENT` | `core/events/types.py` | 只被 `core/control/reducers.py` 用，依赖 core 的 `TaskStatus` | **留 core** |
| `InProcessEventBus` | `core/events/bus.py` | 实现 | **providers/events/** |
| `InMemoryEventStore` | `core/state/event_store.py` | 实现 | **providers/events/** |
| `InMemoryEventStore`（简化版） | `core/control/replay.py` | 死代码 | **删除** |

两个实现一并搬（不只搬 store）：它们是同一类东西，且**配套**——
`InMemoryEventStore(event_bus=...)` 会自动订阅 bus。只搬一个会让布局自相矛盾。

`core/control/replay.py:54` 那个同名简化版（只有 `append` + `read_by_session`，`list`
存储）**除 `core/control/__init__.py` 的一行 re-export 外零使用者**（实测 src 与 tests
均无 import）。它不是「需要合并的重复」，是死代码——留着只会制造「同名不同实现、都对外
可见」的陷阱：`from ctx_weft.core.control import InMemoryEventStore` 拿到的是个缺快照
方法的对象。删掉类与那行导出（用户裁定 2026-08-28；分支未上线，`core.control` 也不在
顶层导出面）。

### 2.1 可行性：无反向依赖

`core/events/types.py` 只 import stdlib（`dataclasses` / `datetime` / `enum` /
`typing`），故 `Event` 体系挪进 protocols **不会**把 core 拖成 protocols 的依赖。
这是本次搬迁成立的前提，实施前应重新核实（若届时 types.py 已引入 core 依赖，
先解耦再搬）。

`EventStore` 的方法签名只用到 `Event` / `RunSnapshot`，两者同批搬迁，自洽。

### 2.2 实现的落点：`providers/events/`

与既有的 `providers/llm/` 同构（领域目录 + 内部按变体分文件）：

```
providers/events/
  __init__.py   导出 InProcessEventBus / InMemoryEventStore
  bus.py        InProcessEventBus + _Subscriber + _matches
  store.py      InMemoryEventStore + _TERMINAL_STATUSES
```

将来 host 要 Redis Streams 的 bus 或 Postgres 的 store，加文件即可，不必再动 core。

`CtxWeftRuntime` 改为 `from ctx_weft.providers.events import ...` 取默认实现；顶层
`ctx_weft.InMemoryEventStore` 的导出**保留**（host 在用），只换来源。

### 2.3 落点：`protocols/events.py` 单文件

约 400 行，与 `protocols/memory.py`（554 行）同量级，符合仓里「一个领域一个文件、
协议与其数据类型同处」的既有风格（`memory.py` 同时装 `MemoryProvider` 与
`MemoryEvent` / `MemoryRecord`，是同一取向）。

## 3. `BLOB_REF_PREFIX` 移到 `protocols/context.py`

2026-08-27 早些时候，`BLOB_REF_PREFIX` 随 `BlobStore` 一起从 `protocols/filesystem.py`
搬进了 `protocols/memory.py`。本次划界推翻其中一半：

`EventBlobStore` 将与 `MemoryBlobStore` 共用同一个 ref 前缀——内容寻址的 sha 口径
两边必须逐字节一致，双写才能得到同一个 ref（配套特性设计 §4）。若前缀留在
`memory.py`，`events.py` 就要为一个字符串常量 import 同层的 `memory.py`，在
protocols 内部制造无谓耦合。

**它属于 `context.py`**：`blob:` 是 `ImagePart.source_type == "ref"` 时 `data` 字段的
前缀，而 `ImagePart` 就定义在 `context.py`。它是**内容形态**的一部分，不是 memory 的，
也不是 events 的。两个 blob 协议各自从 `context.py` 取。

## 4. 挪后的 protocols 全貌与依赖方向

```
protocols/
  context.py     ContentPart / TextPart / ImagePart / ProviderContext
                 BLOB_REF_PREFIX                     ← 不依赖任何同层模块
  memory.py      MemoryProvider + MemoryEvent/Record
                 MemoryBlobStore / NullMemoryBlobStore
  events.py      Event / EventType / EventFilter
                 EventBus / EventStore / RunSnapshot
                 EventBlobStore / NullEventBlobStore  ← 配套特性新增
  llm.py         LLMClient / LLMMessage / LLMRequest …
  capability.py  capability 三件套
  filesystem.py  FS_PROVIDER_NAME / FsTool / SpillSink   ← 回归纯粹，47 行
  knowledge.py / template.py / memory_compat.py
```

依赖方向单向：`context.py` 不依赖任何同层模块，其余各自依赖它、彼此不互相依赖。无环。

`context.py` 对 `core.content` 的惰性绑定（`normalize_content_parts`）是既有的、
刻意的例外，本次不动——理由见该函数 docstring（层序 + 热路径）。

## 5. 兼容策略：破坏面归零

既有 import 点 130+（`core.events.types` 60 处、`core.events` 29 处、
`core.events.bus` 38 处、`core.state.event_store` 9 处；src 37 / tests 99）。

**一个都不改。** 原模块收缩成 re-export：

```python
# core/events/types.py
from ctx_weft.protocols.events import (
    EVENT_TYPES, TRANSIENT_EVENT_TYPES, Event, EventFilter, EventType,
)
# TASK_STATUS_BY_EVENT 仍在本模块定义（core 侧投影逻辑，见 §2）
```

`core/events/__init__.py`、`core/events/bus.py`、`core/state/event_store.py` 同法：
协议改为从 `protocols.events` re-export，**实现改为从 `providers.events` re-export**
（§2.2 把它们搬走了）。两个原模块因此都退化成纯兼容层，自身不再定义任何东西。

顶层 `ctx_weft.InMemoryEventStore` 同样只换来源、不换对象——host 的
`from ctx_weft import InMemoryEventStore` 拿到的仍是同一个类。

顶层 `ctx_weft/__init__.py` 补充导出 `EventStore`（契约此前根本没导出，是 §1 列的
问题之一）。

### 5.1 验收标准

本次是**纯搬迁 + re-export**，不改任何行为。全量测试必须与基线**完全一致**：
`3 failed / 5 skipped`（三个既存失败：`test_compact_flow_e2e` /
`test_golden_conformance` / `test_observe_outcomes`，均与 event 体系无关）。

任何新出现的红都是真回归，不得以「重构难免」为由放行。

## 6. `BlobStore` → `MemoryBlobStore` 改名

两个 blob 协议并存后，`BlobStore` 这个名字不再自明——读者无从判断它指哪一侧。改名
使两侧对称：

| 旧 | 新 |
|---|---|
| `BlobStore` | `MemoryBlobStore` |
| `NullBlobStore` | `NullMemoryBlobStore` |
| `ProviderRegistry.register_blob_store` | `register_memory_blob_store` |
| `ProviderRegistry.get_blob_store` | `get_memory_blob_store` |

后两条本会是破坏性 API 变更，但 `git log master -S register_blob_store -- src/` 零命中
——这两个方法是 `feat/multimodal` 分支新增、尚未进 master 的 API，**改名零破坏**。

### 6.1 改名不波及历史文档

`docs/` 下 94 处 `BlobStore` 绝大多数是既往 spec / plan 对**当时决定**的记述。
**这些一律不改**——历史文档记录的是彼时的事实，改写它们会让「裁定 D4/D5 当时叫什么」
这类可追溯性失效。

只改：活代码、README（描述当前状态）、以及协议自身的 docstring。

## 7. 实施顺序

本设计是配套特性的**前置**：`EventBlobStore` 无处安放，直到 `protocols/events.py`
存在。两者分成两次提交，各自可独立验收。

1. 本次搬迁（验收：测试与基线完全一致）
2. `2026-08-27-dual-blob-store-design.md` 的特性实施

## 8. 不做什么

- **不**动 `TASK_STATUS_BY_EVENT` 的归属（它是 core 的投影逻辑，依赖 core 的 `TaskStatus`）。
- **不**改两个内置实现的任何行为——搬家 + 换 import 来源，代码逐行不动。
- **不**改任何既有 import 语句（re-export 兜住）。
- **不**改 `context.py` 对 `core.content` 的惰性绑定。
