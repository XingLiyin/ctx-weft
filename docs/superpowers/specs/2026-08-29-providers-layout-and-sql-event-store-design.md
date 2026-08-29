# providers 目录重组 + SQL EventStore + blob 归属收口

> 状态：设计已批准（2026-08-29），待实施
> 相关：`2026-08-27-protocols-layer-event-contracts-design.md` —— 已实施。本设计是那次
> 「契约 protocols / 实现 providers / 编排 core」三层划界在 **providers 内部**的延续。
> 相关：`2026-08-27-dual-blob-store-design.md` —— 本设计**推翻其 §4 中 memory 侧的自动
> 解析**（见 §5.3），其余各节不受影响。

---

## 1. 问题

三件互相咬合的事，一起改比分三次改省。

**① `providers/` 是平铺的，同一领域的实现散在同级目录里。**
`memory_blackboard` 与 `memory_sql` 是同一个 `MemoryProvider` 协议的两个实现，却看不出
同源；`blob_fs` 与将来可能出现的 `blob_s3` 同理。`memory_blackboard` 这个名字还是**过期
的**——包 docstring 说它有 `StructuredBlackboardMemoryProvider`，而那个类在代码里根本
不存在，只有 `InMemoryMemoryProvider`。

**② `EventStore` 只有内存实现，且协议的一半是死的。**
`InMemoryEventStore` 自我声明「线程不安全，仅供开发/测试/单进程 demo」。任何真实宿主都
必须自己写一个持久化实现——并且必须**顺带自己写两个订阅者**：一个把事件灌进 store
（因为 `InMemoryEventStore` 把订阅塞在自己的 `__init__` 里，换实现就没了），一个写
`RunSnapshot`（因为仓内没有任何东西会写快照）。后者的缺席让 `save_snapshot` /
`load_latest_snapshot` / `read_after` 三个协议方法在仓内**全是死代码**，`rebuild_view`
的「快照 + 增量」优化对开箱即用的宿主完全不生效。

**③ blob 字节存错了地方。**
`SqlMemoryProvider` 同时实现 `MemoryBlobStore`，把图片字节存进 `memory_blobs` 表。而
`FsBlobStore` 已经是一个同时满足两个 blob 契约的通用实现。两份内容寻址 put/get 实现、
两套测试，并且——把 5 MiB 的图往 RDBMS 里塞本身就与常规实践相悖（备份体积、复制带宽、
Postgres TOAST）。

同时存在一个**现存的洞**：把 `FsBlobStore` 注册成 memory blob store 时，**没有任何办法
正确 GC 它**。`FsBlobStore.collect(live_refs)` 要求调用方喂入活引用集合，而
`MemoryProvider` 协议没有任何接口能列出活引用（`_declared_refs` 是私有的）。
`2026-08-27-dual-blob-store-design.md` §9 还要求共用同一个 `FsBlobStore` 实例时必须同时
喂两侧的活引用——memory 侧那一半今天拿不到。

---

## 2. 目标

1. `providers/` 按**领域 → 协议 → 变体**组织，同层只放同类东西。
2. 补齐 `EventStore` 的 SQL 实现，并把两个订阅者（灌库、写快照）从「每个宿主各写一遍」
   变成仓内提供。
3. 字节离开 RDBMS；引用边留在 RDBMS；补上让宿主能正确 GC 的读接口。

## 3. 不做什么

- 不动 `protocols/` 里的任何协议定义（`MemoryBlobStore` / `EventBlobStore` / `EventStore`
  / `EventBus` 的方法签名一个不改）。
- 不留兼容 shim（理由见 §7）。
- 不移植参考实现里的 `last_activity_times`——协议里没有，是宿主自己的列表排序需求。
- 不默认接上 `SnapshotWriter`（见 §6.6）。
- 不碰 capability / template / knowledge / llm 四个 provider 包。

---

## 4. 目录布局

```
providers/
  _sqlalchemy.py                                  ← 新增（见 §6.1）
  memory/
    in_memory/    __init__.py  provider.py        ← memory_blackboard/in_memory.py
    sql/          __init__.py  models.py  provider.py   ← memory_sql/
  events/
    bus/
      in_process/ __init__.py  bus.py             ← events/bus.py
    store/
      in_memory/  __init__.py  store.py           ← events/store.py
      sql/        __init__.py  models.py  store.py      ← 新增（见 §6.2）
    persister.py                                  ← 新增（见 §6.4）
    snapshot.py                                   ← 新增（见 §6.5）
  blob/
    fs/           __init__.py  store.py           ← blob_fs/
```

**为什么 `events/` 比 `memory/` 多一层。** `events/` 这个域下面装着**两个不同的协议**：
`EventBus`（传输/扇出，一个实现）与 `EventStore`（持久化，两个实现）。不分层就会把
`in_process`（一个 bus）和 `in_memory` / `sql`（两个 store）摆成平级三兄弟，读起来像同一
个东西的三个变体。`memory/` 下面只有 `MemoryProvider` 一个协议，`in_memory` 与 `sql`
本就是它的两个变体，不需要这一层。`blob/fs` 同理——`FsBlobStore` 一个类实现两个协议，
但它是**一个**东西。

**命名收口三条：**
- `memory_blackboard` → `memory/in_memory`。顺手删掉包 docstring 里那句假话。
- `in_memory.py` → `provider.py`，与 `sql/provider.py` 对齐；`events/store/*/store.py` 同理。
- 包名描述的维度要一致：`in_process` 说的是「投递发生在本进程内」，`in_memory` 说的是
  「数据存在内存里」，分层之后这两个词各自待在自己的协议下，不再被并排比较。

**`__init__.py` 的导出边界。** `providers/memory/__init__.py` 与
`providers/events/__init__.py` **只 re-export 无可选依赖的名字**
（`InMemoryMemoryProvider` / `InProcessEventBus` / `InMemoryEventStore` /
`EventPersister` / `SnapshotWriter` / `attach_persistence`）。`sql` 子包**不 re-export、
也不做 PEP 562 惰性 `__getattr__`**。

理由：`providers/llm` 用惰性是因为 adapter 类名本身就是公开 API 的一部分（宿主写
`from ctx_weft.providers.llm import AnthropicAdapter`）。这里相反——让路径本身说明依赖
边界更诚实：`from ctx_weft.providers.memory.sql import SqlMemoryProvider` 一眼看出要
`[sql]` extras。惰性 `__getattr__` 会让 `providers.memory.SqlMemoryProvider` 这个名字
看起来存在、访问时才炸。这条也保住了 `memory_sql` 现有的不变量：**没有任何上层包 eager
import 它**，`import ctx_weft.providers` 在缺 sqlalchemy 时照常工作。

---

## 5. blob 归属：字节离开 RDBMS

### 5.1 裁定 D4 的理由只覆盖引用边，没覆盖字节

`2026-08-27-dual-blob-store-design.md` 承接的裁定 D4 是「blob 并入 memory，因为存取与
回收应与 ingest/fold 同事务」。这条论证在 `provider.py::_ingest_in_tx` 里的落点是：

```python
for ref in collect_blob_refs(event):
    db.add(MemoryBlobRefModel(event_id=event_id, sha=ref[len(BLOB_REF_PREFIX):]))
```

这段是**无条件执行的**——它跟字节存在哪儿毫无关系。真正需要与 ingest 同事务的是
`memory_blob_refs` 这张**引用边表**，不是 `memory_blobs` 这张**字节表**。字节是可以搬走
的那一半，D4 从来没论证过它必须留下。

### 5.2 删什么、留什么、补什么

**删（`providers/memory/sql/`）：**
- `models.py` 的 `MemoryBlobModel`（`memory_blobs` 表）
- `provider.py` 的 `put` / `get` / `collect_blobs`、对 `MemoryBlobStore` 的基类继承、
  `_DEFAULT_BLOB_GRACE` 常量与 `blob_grace_period` 构造参数
- `open_sqlite_memory` 的 `blob_grace_period` 形参

**留：**
- `MemoryBlobRefModel` 与 `_ingest_in_tx` 里那段引用边写入（§5.1）
- `protocols/memory.py` 的 `MemoryBlobStore` / `NullMemoryBlobStore` 定义原样不动

**补（`SqlMemoryProvider` 新增一个读方法）：**

```python
async def live_blob_refs(self) -> set[str]:
    """当前仍被活记录引用的全部 blob ref（``blob:<sha>``）。

    喂给 ``FsBlobStore.collect(live_refs)`` 做 mark-sweep 的 mark 输入。
    **跨全部租户**——只看单租户会删掉别的租户仍在引用的同 sha 字节（内容寻址天然共享
    一行，判据必须看见全部引用者）。判据与原 ``collect_blobs`` 的 JOIN 完全相同。
    """
```

实现就是原 `collect_blobs` 里那条子查询：
`memory_blob_refs JOIN memory_events ON id WHERE is_superseded = 0`，把 sha 加上
`BLOB_REF_PREFIX` 前缀返回。

这个方法补的是 §1③ 那个现存的洞，**与删不删字节表无关**——即便保留 SQL 字节存储，共用
`FsBlobStore` 的宿主也需要它（`dual-blob-store` §9 的要求）。

### 5.3 解析规则对称化

`ProviderRegistry.get_memory_blob_store()` 现在是三级：

```
显式注册 > memory provider 自身（isinstance + can_externalize） > NullMemoryBlobStore
```

中间那一级的唯一服务对象就是 `SqlMemoryProvider`。删掉字节存储后它无对象可服务，**退回
两级**，与 `get_event_blob_store()` 完全对称：

```
显式注册 > NullMemoryBlobStore
```

副产品：当前分支为了论证「为什么 memory 侧自动回落而 event 侧不自动回落」写了相当篇幅
的注释（`runtime.py` 两个 getter 的 docstring）。不对称本身消失，比继续解释它更好。

### 5.4 连带要改的文档口径

- `protocols/memory.py` 里 `MemoryBlobStore` 的 docstring 有一段论证「保持独立 ABC 而不
  并入 `MemoryProvider`，因为自动解析判据 `isinstance(mem, MemoryBlobStore) and
  mem.can_externalize` 会恒真、失去分辨力」。自动解析没了，这段论证失效。**结论不变**
  （仍然独立），理由换成「blob 能力与 memory 能力正交，字节放哪由宿主决定」。同时删掉
  「仓内实现见 `SqlMemoryProvider`」，改指 `providers/blob/fs`。
- `docs/host-migration-to-sql-memory.md` 整篇按新接线重写（它现在教宿主「注册
  `SqlMemoryProvider` 即自动获得 blob 能力」）。
- `providers/blob/fs/store.py` 里两处引用 `SqlMemoryProvider.collect_blobs` 作为宽限期
  论据的注释，改指本设计。

### 5.5 破坏性变更与时机

对宿主而言这是破坏性的：接 Postgres 的宿主要额外 provision 一个文件系统（或换成自己的
对象存储实现）。**但没有迁移负担**——`providers/memory_sql/` 整个包是 `feat/multimodal`
分支新加的（git 状态是 A 不是 M），从未随任何版本发布过，不存在「已经有字节躺在
`memory_blobs` 表里」的宿主。

---

## 6. events：新增件

### 6.1 共享 SQL 基建 → `providers/_sqlalchemy.py`

从 `memory/sql/models.py` 与 `provider.py` 抽出两样东西：

- `UtcDateTime`（时区保真的 `TypeDecorator`）——SQLite 没有时区类型，SQLAlchemy 的
  SQLite 方言会把 aware datetime 的 tzinfo 直接丢掉。这个坑 events 侧一模一样。
- `make_session_factory(url, **kw) -> (engine, factory)`

放私有共享模块，`providers/_encoding.py` / `_tooldecl.py` / `_script_runner.py` 已有先例。
**不进 `providers/__init__.py`**，故 sqlalchemy 仍不会被 eager import。

**`Base` 各自独立**（`memory/sql` 一个、`events/store/sql` 一个）。共用会让只想建 events
表的宿主被迫连 memory 表一起建，而两个包的可选依赖边界本就是分开的。单文件 SQLite 部署
照样可以共享同一个 `engine` / `factory`，跑两次 `create_all` 即可：

```python
engine, factory = make_session_factory("sqlite+aiosqlite:///app.db")
async with engine.begin() as conn:
    await conn.run_sync(memory_sql.Base.metadata.create_all)
    await conn.run_sync(events_sql.Base.metadata.create_all)
memory = SqlMemoryProvider(factory)
store  = SqlEventStore(factory)
```

### 6.2 `SqlEventStore`（`events/store/sql/`）

七个协议方法全实现。参考 `IpMasterCoworkPy` 的 `PostgresEventStore`，但改三处：

**① 补 `schema_version` 列。** 参考实现的 `EventModel` 没有这一列，`_row_to_event` 也不
还原。`Event.schema_version` 是给 reducer 分支用的（`protocols/events.py`），丢了就是往返
有损——今天全是 1，所以不会立刻炸，但第一次 bump 版本时会静默地把新事件读成旧版本。

**② 补 `RunSnapshot.run_id` 列。** 参考实现的 `load_latest_snapshot` 硬编码
`run_id=""`，同样是往返有损。

**③ `list_active_session_ids` 不照抄**——见 §6.3。

`append` **不过滤 `TRANSIENT_EVENT_TYPES`**，与 §6.4 之后的 `InMemoryEventStore` 同口径：
过滤是订阅策略，归 `EventPersister`。两个 store 在这一点上必须一致，否则一致性测试
（§8 第 1 项）无法用同一套用例跑。

保留参考实现的 `keep_snapshots=3` 剪枝：定期写快照会让旧快照无界累积，而恢复只取最新
一张。子查询带 `LIMIT`，SQLite / Postgres 均支持。

配 `open_sqlite_event_store(db_path)` 便利入口，与 `open_sqlite_memory` 同形。

### 6.3 `list_active_session_ids` —— 本次移植唯一的设计工作

两侧现有判据**不等价**：

| | `InMemoryEventStore` | 参考实现（纯 SQL） |
|---|---|---|
| `SessionCreated` / `SessionResumed` | 计入 active | 计入 opened |
| `SessionFinished` | 移出 active | 计入 finished |
| `SessionStatusChanged` 且 `new_status ∈ {SUCCEEDED, FAILED, CANCELED, INTERRUPTED}` | **移出 active** | **完全忽略** |
| 判据 | 四条规则的增量状态机 | `max(opened.id) > max(finished.id)` |

照抄参考实现会让两个 store 对「哪些 session 需要崩溃恢复」给出不同答案——经
`SessionStatusChanged` 终结的会话在 SQL 侧会被永远报成 active，宿主每次启动都去恢复一个
已经结束的会话。

终态判据藏在 `payload` JSON 里，纯 SQL 提取要方言分叉（SQLite `json_extract` vs Postgres
`->>`）。**做法：一条查询捞出全部四类生命周期事件（`type IN (...)`，按 `id` 即 ULID 升序），
在 Python 里跑与 `InMemoryEventStore` 逐字相同的那台状态机。**

- 方言无关。
- 「与内存版一致」是**结构性**的（同一台状态机），不是靠对齐维护的。
- 行数 = 会话数 × 每会话几条生命周期事件，且只在启动时调一次。真的大到不能接受时，正确
  的下一步是加一张 session 状态投影表，而不是把判据塞回 SQL 表达式里。

⚠️ 注意「最新一条生命周期事件」**不能**直接定胜负：`SessionStatusChanged` 携带非终态
（如 `RUNNING`）时不改变活跃性，需要继续往前看。必须重放整个序列。

### 6.4 `EventPersister`（`events/persister.py`）

把 `InMemoryEventStore.__init__` 里那句 `event_bus.subscribe(None, self.append)` 抽出来，
对**任意** `EventStore` 通用；`detach()` 一并搬过来。这正是参考实现里 `EventPersister`
存在的原因——换掉内存 store 就得自己补一个。

**行为变化（必须写进升级须知）：`TRANSIENT_EVENT_TYPES` 过滤跟着搬到 persister，
`InMemoryEventStore.append()` 不再自己丢弃瞬态事件。**

- 理由：它本就是**订阅策略**而非存储策略。
- 副作用是好的：`append` 变成「让存什么就存什么」，EventStore 一致性测试（§8）才能直接
  测 append/read 往返，不会被 store 悄悄吃掉测试事件。
- 仓内零影响：已确认 `src/` 里除那句自订阅外**没有任何地方调 `append`**。
- 对直接调 `append` 的宿主是行为变化（事件表会开始堆 delta）。

`InProcessEventBus.emit` **已经**捕获 handler 异常（`logger.exception("EventBus handler
raised for subscriber %s")`），故 persister 自己那层 try/except 不是为了防止掀掉 loop，
只是为了把 event id / type 写进日志。这一点要写在 docstring 里，免得后来者以为它是
唯一防线而不敢动。

### 6.5 `SnapshotWriter` + `attach_persistence`（`events/snapshot.py`）

照参考实现移植：订阅 bus；`SessionFinished` 写一张；`RunFinished` 且距上次累计 ≥ N
（默认 50）写一张并清零；瞬态事件不计数。`_write` 走 `core.control.reducers` 的
`rebuild_view` + `serialize_view`。

- 用 `rebuild_view`（快照 + 增量）而非全量回放是**必须**的：bus handler 在 `emit()` 里
  **内联**执行，任何 O(全部事件) 的读取都会阻塞 loop 主路径。这条要在 docstring 里写死。
- `providers` → `core` 的依赖方向是允许的（`providers/memory/sql/provider.py` 已经 import
  `core.content` / `core.utils`）；被禁止的只有 `protocols` → `core`。

**顺序陷阱与消除办法。** `SnapshotWriter` 必须在 `EventPersister` **之后**订阅，否则
`rebuild_view` 看不到当前这条事件，而 `last_event_id=event.id` 会与 view 对不上。参考
实现只在注释里提了一句。这里不靠注释：

```python
def attach_persistence(event_bus, event_store, *, snapshot_every_n: int = 0):
    """按正确顺序接线 EventPersister（+ snapshot_every_n > 0 时的 SnapshotWriter）。

    返回可 detach 的句柄。顺序错误由此不可达。
    """
```

### 6.6 runtime 接线

`CtxWeftRuntime` 从 `InMemoryEventStore(event_bus=self.event_bus)` 改成
`InMemoryEventStore()` + `EventPersister(store, self.event_bus)`。

**`SnapshotWriter` 默认不接。** 宿主要就 `attach_persistence(..., snapshot_every_n=50)`
一行。理由：接上会给所有宿主多一个订阅者和周期性 `rebuild_view`，而现有行为零变化是更
安全的默认；快照的收益（重启回放 O(全部事件) → O(增量)）对内存 store 的开发场景也不明显。

---

## 7. 破坏性变更汇总（写进 README 升级须知）

| # | 变更 | 影响面 | 迁移 |
|---|---|---|---|
| 1 | `providers.memory_blackboard` → `providers.memory.in_memory` | 已知宿主 1 行 | 改 import |
| 2 | `providers.memory_sql` → `providers.memory.sql` | 未发布 | 改 import |
| 3 | `providers.blob_fs` → `providers.blob.fs` | 未发布 | 改 import |
| 4 | `providers.events.{bus,store}` → `providers.events.bus.in_process` / `.store.in_memory` | 未发布 | 改 import |
| 5 | `SqlMemoryProvider` 不再是 `MemoryBlobStore` | 未发布 | 注册 `FsBlobStore` |
| 6 | `get_memory_blob_store()` 不再回落到 memory provider | 未发布 | 同上 |
| 7 | `InMemoryEventStore.append()` 不再过滤瞬态事件 | 直接调 `append` 的宿主 | 改用 `EventPersister` |
| 8 | `InMemoryEventStore(event_bus=...)` 不再自订阅 | 直接构造它的宿主 | 改用 `attach_persistence` |

**不留兼容 shim。** 1–4 是纯路径变更，已知宿主（`IpMasterCoworkPy`）只断一行；其余全部
未发布。留一层永久转发不值这个价。`core/state/event_store.py` 与 `core/events/*` 的
**现有** shim 保留（它们服务的是上一次三层划界），只需把内部指向改到新路径。

---

## 8. 测试

**新增 `tests/unit/test_event_store_conformance.py`**（仿 `test_memory_conformance.py`）：
同一套用例跑 `InMemoryEventStore` 与 `SqlEventStore`。这是 §6.3 那个分歧的直接产物——
没有它，两个 store 对 `list_active_session_ids` 的口径分歧只会在生产里以「重启后某些会话
不弹恢复」的形式暴露。必须覆盖：

1. append / read_by_session 往返，**逐字段**（含 `schema_version`、`causation_id`、
   `tenant_id`、时区保真的 `timestamp`）
2. `read_after` 的 ULID 序与边界（`after_event_id` 不存在时的行为）
3. `read_session_events_of_types` 过滤
4. 快照往返（含 `run_id`）、`load_latest_snapshot` 取最新、无快照返 None
5. 生命周期状态机四条规则各一例 + 一例「`SessionStatusChanged` 非终态不改变活跃性」
6. 跨 session 隔离

**其余测试变动：**

- 97 个测试文件的 `memory_blackboard` → `memory.in_memory`，纯 sed
- `test_sql_blob_store.py`（506 行）砍掉字节存取与回收部分，改写成 `live_blob_refs` 的测试
- `test_l05_demotion_blob_lifecycle.py` 改用 `SqlMemoryProvider` + `FsBlobStore` 组合——
  顺带从「测 SQL 自己存自己收」变成**测真实的生产组合**
- 新增 `live_blob_refs()` × `FsBlobStore.collect(live_refs)` 的联动 GC 测试：图入库 → 记录
  被 fold → 活引用归零 → 过宽限期被回收 → `rehydrate_content` 降级成占位
- `EventPersister` / `SnapshotWriter` / `attach_persistence` 各自的单测，含顺序断言
  （snapshot 的 `last_event_id` 必须等于触发它的那条事件）

---

## 9. 风险

按判断的严重度排序：

1. **`list_active_session_ids` 口径（中）** —— 影响崩溃恢复，错了很难发现（表现为「有些
   会话重启后不恢复」或「已结束的会话反复被恢复」）。靠 §8 的一致性测试兜住。
2. **`append` 不再过滤瞬态（低-中）** —— 仓内零影响，但对直接调 `append` 的宿主是**无声
   的**行为变化：事件表开始堆 delta，且这些 delta 会被 `read_by_session` / `reduce_events`
   全量回放。升级须知里明写。
3. **97 文件 sed（低）** —— 机械但 diff 极大，容易掩盖真实改动。**目录迁移必须是独立的
   一个 commit**，与功能改动分开。
4. **`SnapshotWriter` 在 `emit` 内联跑 `rebuild_view`（低）** —— 默认不接，接上的宿主自己
   承担；docstring 写清这是内联执行而非后台任务。
5. **`docs/host-migration-to-sql-memory.md` 整篇失效（低）** —— 是写给宿主的迁移指南，
   必须跟着改，否则会指导宿主走一条已经不存在的路。
