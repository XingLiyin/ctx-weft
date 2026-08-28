# 双 blob store——事件流全 ref 化

> 状态：设计已批准（2026-08-27），**前提刷新于 2026-08-28**，待实施
> 前置：`2026-08-27-protocols-layer-event-contracts-design.md` —— **已实施**
> （`protocols/events.py` 现已存在，commit `8788f02..84a9308`）
> 上级：`2026-08-20-multimodal-design.md`

> ⚠️ **§3 的「两边 sha 口径必须逐字节一致」与 §5「入口双写」已于 2026-08-28 被推翻**，
> 见 `docs/superpowers/plans/2026-08-28-blob-store-decoupling.md`。§5.1 当初以成本为由
> 否决的「各自 put + 跨界搬运」正是现行方案：跨界只发生在恢复路径这一个交界处，由
> event 侧发起（`hydrate_event_content`），memory 的写路径不再替 event 代劳。
> 其余各节（§4 注册面、§7 严格门控、§8 条件可见、§9 生命周期归 host）仍然有效。

## 0. 本设计写于 8-27，这些前提在实施前已经变了

写完之后仓里发生了两件事，本节逐条订正，正文已按此更新：

| 当时 | 现在 | 影响 |
|---|---|---|
| `BlobStore` / `NullBlobStore` | `MemoryBlobStore` / `NullMemoryBlobStore` | 正文已用新名 |
| `ProviderRegistry.register_blob_store` / `get_blob_store` | `register_memory_blob_store` / `get_memory_blob_store` | §4 已更新 |
| `BLOB_REF_PREFIX` 在 `protocols/memory.py` | 在 **`protocols/context.py`** | §3 的「共用前缀」来源随之改 |
| `protocols/events.py` 不存在 | 已存在（含 `Event` / `EventBus` / `EventStore`），import 块已有 `abstractmethod` | `EventBlobStore` 直接追加即可，需补 `from abc import ABC` |
| `MemoryEvent` 无 `blob_refs` 字段 | **有**（L0.5 引用缺陷修复引入，见 `2026-08-27-l05-demotion-drops-blob-reference.md`） | 见 §6.1 |
| §8 说 `describe()` 返回空工具集 | **事实错误**——`describe()` 返回 `CapabilityProviderInfo`（provider 元信息），暴露工具的是 **`list(ctx)`** | §8 已订正 |

---

## 1. 问题

多模态外部化（Phase 3b/3c）只给了 memory 一侧 blob 存储。事件流侧的现状是三种口径
并存，且都不理想：

| 事件 | 现状 | 问题 |
|---|---|---|
| `SESSION_CREATED` / `SESSION_RESUMED` | `content_to_jsonable_refs_only`（本日新增） | 无 blob 时图降级成占位，重启丢图 |
| `TASK_CREATED` / `TASK_REQUEUED` | `content_to_jsonable` | 无 memory blob 时 **base64 字节进事件行**（5 MiB 图 ≈ 6.7 MB） |
| `HITL_ANSWERED` 等 | `content_to_jsonable` | 同上 |

根因是事件流侧**没有自己的 blob 存储**，只能在「落字节」与「丢图」之间二选一：
memory blob 可用时事件里恰好是 ref（搭了便车），不可用时就只剩这两条坏路。

更深一层：blob 的引用边只锚在 `memory_events` 的活记录上（`collect_blobs` 的
JOIN 条件），event store 完全不构成引用。所以即便事件里存着 ref，对应记录被 fold
之后字节仍会被回收——事件流重放拿到的是悬空 ref。

## 2. 目标

**事件库恒不含字节，且所有图片都以 ref 形式可回读。** host 可以让 event 与 memory
共用一个 blob 实现，也可以分开——core 不需要知道。

## 3. 协议：`EventBlobStore`

落在 `protocols/events.py`，与 `EventStore` 同处（判据见前置设计 §2）。

```python
class EventBlobStore(ABC):
    @property
    def can_externalize(self) -> bool: return True
    async def put(self, data: bytes, media_type: str, ctx: ProviderContext) -> str: ...
    async def get(self, ref: str, ctx: ProviderContext) -> tuple[bytes, str] | None: ...

class NullEventBlobStore(EventBlobStore):
    can_externalize = False
```

与 `MemoryBlobStore` **同形但类型无关**（用户裁定 2026-08-27）。不做成子类型、也不
共用一个 ABC，理由是两侧语义会各自演进——最明显的是回收锚点不同：memory 侧是记录
`is_superseded`，event 侧是事件保留策略。今天同形不代表明天同形。

host 共用时一个类同时实现两者：

```python
class MyBlobStore(MemoryBlobStore, EventBlobStore):
    ...  # 一份实现，注册两次
```

`put` 的内容寻址与幂等要求、`get` 对不存在的 ref 恒返回 `None` 不抛——两条约束与
`MemoryBlobStore` 逐字相同，在 docstring 各写一份（协议独立，不靠 import 共享措辞）。

**共用的 ref 前缀取自 `protocols/context.py::BLOB_REF_PREFIX`**（协议层划界已把它移到
那里，正是为了让两个 blob 协议各自取用而不互相 import）。两边的 sha 口径必须逐字节
一致，双写才能得到同一个 ref。

`protocols/events.py` 现有的 import 块已含 `from abc import abstractmethod`，
新增 `EventBlobStore` 需补 `ABC`。

## 4. 注册面：显式，不自动解析

```python
ProviderRegistry.register_event_blob_store(store)
ProviderRegistry.get_event_blob_store()   # 显式注册 > NullEventBlobStore
```

**刻意不像 `get_memory_blob_store()` 那样自动回落到 memory provider。** 自动解析会让
「共用」成为隐式默认，而本设计的出发点正是让两者可分。host 要共用就把同一个实例
注册两次——意图写在接线代码里，而不是藏在解析规则里。

`get_memory_blob_store()` 的三级回落（显式 > memory provider > `NullMemoryBlobStore`）保持不变。

## 5. 入口双写

`normalize_content` 增加 `event_blob_store` 参数，在**同一个循环内**双写：

```python
raw = base64.b64decode(part.data, validate=True)
ref = await memory_blob_store.put(raw, media_type, ctx)
await event_blob_store.put(raw, media_type, ctx)     # 内容寻址 → 同一个 ref
out.append(dataclasses.replace(part, data=ref, source_type="ref", byte_size=len(raw)))
```

**必须同循环**：`put` 之后 raw bytes 就不再持有，分两趟要么重新 b64decode（热路径上
不可接受，同 `image_byte_size` 拒绝解码的理由），要么从 store 取回（一次无谓 IO）。

⚠️ **双写只在 memory 侧可外部化时发生。** `normalize_content` 开头那句
`if not memory_blob_store.can_externalize: return content` 保持不变，整个函数（含
event 侧的 put）随之短路——否则会去调 `NullMemoryBlobStore.put` 触发
`NotImplementedError`。所以「memory 无 blob、event 有 blob」这一组合下，事件的 ref 化
**不由入口负责**，而由 §6 的 `content_to_event_jsonable` 在发射点独立完成。两条路径
互补，覆盖全部四种组合。

内容寻址保证两边 sha 相同，故**只有一个 ref**，两边都取得到。读侧因此**全部不用改**：

```
memory 路径  → memory_blob.get(ref)   （装配 / rehydrate / L0.5 / get_image）
事件流路径   → event_blob.get(ref)    （TASK_CREATED / HITL / 重放）
```

两条读路径各取各的 store，永不交叉。host 分不分开实现，core 都不感知。

共用同一实例时第二次 `put` 幂等命中已有行，零额外成本（仅刷新 `created_at`，即
「最后一次有人声称要用它」——语义正确）。

### 5.1 为什么不是「各自 put + 跨界搬运」

备选方案是入口只 put 进 memory、事件发射点另行 put 进 event store，从事件恢复的 ref
在 ingest 进 memory 前从 event store 取字节再 put 进 memory store。语义更"每个 store
自洽"，但要在恢复路径（`_persist_user_prompt`）上加一次 async 取+存，且那条路径此前
完全不碰 blob。双写把复杂度留在唯一的 put 点上，恢复路径一行不改。

## 6. 事件外部化

新函数取代 `content_to_jsonable_refs_only`（后者是本日为 session 事件临时加的过渡
实现，本设计实施后连同 `tests/unit/test_session_prompt_refs_only.py` 里针对它的 6 个
纯函数用例一并删除；该文件的 3 个事件重放用例改挂新函数后保留——它们钉的是「session
事件重放出 parts 与 ref」这个不随实现变的性质）：

```python
async def content_to_event_jsonable(content, *, event_blob_store, ctx):
    """str/None → 原样；ref part → 原样；base64/url part → put 进 event blob → ref。"""
```

- **ref part 原样**：入口双写已保证 event store 持有这份字节，不必重复 put。
- **base64 part 外部化**：memory blob 不可用时入口不外部化，content 里仍是 inline
  base64。此时只要 event blob 可用，事件侧仍能独立完成 ref 化——**这是本设计让
  「所有 base64 变引用」在 memory 无 blob 时也成立的关键**。

调用点（全部已在 async 上下文，`EventBus` / `EventStore` 一行不动）：

| 调用点 | 事件 |
|---|---|
| `SessionManager.create_session` | `SESSION_CREATED` |
| `SessionManager.resume_session` | `SESSION_RESUMED` |
| `TaskManager._task_payload` 的调用处 | `TASK_CREATED` |
| `TaskManager.reopen_task` | `TASK_REQUEUED`（`user_prompt` + `original_user_prompt`） |
| `HitlManager` 的应答发射 | `HITL_ANSWERED` / `APPROVED` / `MODIFIED` / `REJECTED` |

`_task_payload(task)` 本身是同步函数、返回整个 payload dict。改法是把 `user_prompt`
的外部化提到 `await self._emit(...)` 那一行之前完成，再把结果传进去——**不**把
`_task_payload` 改成 async（它还负责十余个与内容无关的字段，async 化会让所有调用方
被迫等待一次 IO）。

`serialize_view` / `deserialize_view` 保持同步、继续用 `content_to_jsonable`：投影
快照读的 view 本就是从事件还原来的 ref，无 base64 可外部化，不需要 `put`。

### 6.1 与 `MemoryEvent.blob_refs` 无交集

L0.5 引用缺陷修复（`2026-08-27-l05-demotion-drops-blob-reference.md`）给 `MemoryEvent` /
`MemoryRecord` 加了 `blob_refs` 字段，作为 GC 的 mark 输入。**它与本设计不相干**：

- `blob_refs` 是 **memory 侧**记录的字段，服务于 `SqlMemoryProvider` 建引用边；
- 本设计动的是**事件 payload** 里的 `content`（`TASK_CREATED` 等），那是
  `content_to_jsonable` 的产物，不含 `blob_refs`。

两者都叫「blob ref」但落在完全不同的载体上，实施时不要混淆——**事件外部化不读也不写
`MemoryEvent.blob_refs`**。

## 7. 严格默认：携图必须有 EventBlobStore

新错误：

```python
class BlobStoreRequiredError(CtxWeftError):
    code = "BLOB_STORE_REQUIRED"
```

在 `validate_content` 内判定，位置紧接 `supports_vision` 门控之后：

```
格式校验（media_type / base64 / 尺寸）
  → 视觉能力门控（supports_vision，严格默认 False）
  → event blob 门控（can_externalize，严格默认拒绝）   ← 新
```

**入口即拒**，与前两道同处，理由一致：畸形/不被支持的内容不该流到下游才炸。三个入口
（`start_session` / `run_single_task` / HITL 应答）共用 `_validate_and_normalize_content`，
故只需在这一处接上。

纯文本会话完全不受影响——`validate_content` 对 `str` / 无图内容在门控之前就已返回。

### 7.1 破坏性变更

比 `supports_vision` 那次影响更大：**任何携图会话在未注册 `EventBlobStore` 时直接失败**。

- 波及 **7 个测试文件**（实测：含 `ImagePart` 且经三个入口之一的），实施时需接一个
  `EventBlobStore` 桩：

  ```
  tests/integration/test_media_fold_replay_e2e.py
  tests/integration/test_multimodal_end_to_end.py
  tests/unit/test_content_validation.py
  tests/unit/test_hitl_multimodal_validation.py
  tests/unit/test_memory_conformance.py
  tests/unit/test_multimodal_entry.py
  tests/unit/test_normalize_content.py
  ```

  桩放 `tests/unit/conftest.py` 作共用 fixture，避免逐个文件重复；其中
  `test_content_validation.py` 还需**新增**反向用例（无 event blob + 含图 → 拒绝）。
- README 加迁移说明，与 Phase 3a 的 `supports_vision` 那条并列。

选择这条硬口径而非「无 blob 时降级成占位」，是用户裁定（2026-08-27）：口径统一——
事件库恒不含字节、恒可回读，没有例外分支。

## 8. `MediaCapabilityProvider` 条件可见

目标：没有可用 blob 时，`media:get_image` 不出现在模型的工具集里。

**做法是 `list(ctx)` 在 blob 不可用时返回空列表，而不是在 `Runtime.__init__` 里
条件注册**（用户裁定 2026-08-27 选 B）。

⚠️ **本设计初稿写的是「`describe()` 返回空工具集」，那是事实错误**：
`MediaCapabilityProvider.describe()`（`core/media/capability.py:377`）返回的是
`CapabilityProviderInfo`（name / capability_count / supports_streaming…），是 **provider
元信息**；真正把工具暴露给模型的是 `list(ctx) -> list[ToolCapability]`
（同文件 `:374`）。要改的是后者。`describe()` 的 `capability_count` 宜一并跟着变
（可用时 1、不可用时 0），保持自洽。

理由：注册发生在 `Runtime.__init__`，而 host 完全可能先构造 `Runtime` 再
`register_memory()`。`__init__` 时刻判定 blob 不可用 → 工具永远缺席，即使后来接上了
memory。这正是现有注释刻意规避的东西：

> memory / blob store 由它在调用时经 registry 解析，**故此处不要求它们已注册**

以及 `get_memory_blob_store()` docstring 里记着的同款坑（「缓存会让先 get 后 register
的接线顺序静默地拿不到 memory」）。

判据用 **memory blob store**（`get_memory_blob_store().can_externalize`），不是 event
侧——`media:get_image` 取的是 L0.5 占位里的 ref，而 L0.5 是 memory 侧的机制
（`_media_enabled` 用的也是这个判据，保持一致）。

对模型的可观测效果与「不注册」完全相同；代价是注册表里多一个返回空工具集的 provider。

## 9. 事件侧 blob 的生命周期归 host

两个 store 分开之后，**存储、引用、清理三件事各自独立**：

| | memory 侧 | event 侧 |
|---|---|---|
| 字节 | `memory_blobs`（或 host 实现） | host 实现的 `EventBlobStore` |
| 引用边 | `memory_blob_refs`，`ingest` 时按内容里的 ref 建 | host 定义（按事件保留策略） |
| 清理 | `collect_blobs`：无活引用 + 过宽限期 | host 定义 |

core 不在两者之间建立任何关联。`SqlMemoryProvider.collect_blobs` 的 JOIN 只扫
`memory_events`——这**不是缺陷**，它本就不该管事件流的 ref；反过来 host 的
`EventBlobStore` 也不必关心 memory 记录是否 supersede。

由此，「事件流重放能否重建出图片」完全取决于 host 让 event blob 活多久：

- **分开实现**：两套互不可见，各按各的策略回收。想让事件流永远可重建，就让 event
  blob 的回收与事件保留策略对齐（例如永不回收，或按事件 TTL）。
- **共用一个实例**：该实现要**同时**看两侧的引用才能安全回收。仅套用 memory 侧的
  `collect_blobs` 判据会删掉事件流仍需要的字节——这是共用实现自身的责任。

`EventBlobStore` 的 docstring 必须写明这条，尤其是共用实现的那条陷阱：host 很容易
想当然地复用 memory 侧的回收逻辑。

core 的保证到此为止：**事件库恒不含字节、结构与 ref 不丢**。字节能否取回是 host 的
存储策略问题，core 不规定、也不应规定。

## 10. 测试

| 层 | 覆盖 |
|---|---|
| 协议 | `EventBlobStore` 桩的 put 幂等 / get 返 None 不抛 / `can_externalize` 探询 |
| 双写 | 同一份字节两边得到同一个 ref；共用实例时第二次 put 幂等；memory 不可用时不 put |
| 事件外部化 | ref 原样；base64 → ref；无 event blob + 含图 → 入口即拒；纯文本零影响 |
| 五个发射点 | 各自 payload 里恒无 base64（断言字节串不出现在序列化结果中） |
| 重放 | `rebuild_view` 还原出 parts 与 ref；**存量裸 str payload 仍原样重放**（零数据迁移） |
| 分开实现 | 两个不同实例时，memory 路径与事件路径各取各的 store 都能取到 |
| 条件可见 | blob 不可用 → `describe()` 空工具集；可用 → 含 `media:get_image` |
| 回归 | 纯文本全链路逐字节不变 |

## 11. 不做什么

- **不**让 `get_event_blob_store()` 自动回落到 memory provider（§4）。
- **不**改 `serialize_view` / `deserialize_view` 的同步性（§6）。
- **不**把 `_task_payload` 改成 async（§6）。
- **不**解决悬空 ref（§9）。
- **不**改 memory 侧 blob 的任何既有行为（回收、宽限期、租户取向）。
