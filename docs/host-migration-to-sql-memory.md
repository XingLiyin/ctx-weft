# 宿主迁移清单：切到 ctx-weft 自带的 `SqlMemoryProvider`

日期：2026-08-27
适用版本：ctx-weft `feat/multimodal` @ Phase 3c（commit `65dd606` 之后）
相关设计：[多模态整体设计 §14](superpowers/specs/2026-08-20-multimodal-design.md)

> **这份文档是给人执行的**，不是给 agent 读的。按顺序做，每一步都有验收判据。
> 用户裁定 D4 是「宿主后续用这个 memory，平滑切换」——**平滑指存量行零迁移**，
> 但**不指零 DDL**：第 2 步里有一条**破坏性 DDL 必须先做**，只加列绕不开。

---

## 0. 这次切换会得到什么 / 失去什么

**得到**：

- **多租户隔离**。宿主现有的 `postgres.py` 参考实现**完全没有 tenant**：
  `load_view` / `recall_topic` / `list_subscriptions` 都不看 `ctx.tenant_id`，
  PUBLICATION 覆盖按 topic 扫全表。这四个洞在 ctx-weft 侧已实证并修复
  （见设计 §14.2），其中一个是**写侧**的——「B 一发布就把 A 的黑板行标成
  superseded，而 B 自己根本读不到那行」，即**能销毁自己读不到的数据**。
- **多模态能真的存住**。参考实现的 `_ingest_in_tx` 里是 `json.dumps(event.content)`，
  而 `ContentPart` 是普通 dataclass —— **一旦 content 带图就 `TypeError`**（今日不炸
  只因 content 全是 `str`）。新 provider 有判别列 + jsonable 往返。
- **blob 字节与引用表**，以及延迟回收（`collect_blobs`）。

**失去 / 需要留意**：

- `FilesystemBlobStore` 已被移除（裁定 D5）。若宿主此前显式
  `register_blob_store(FilesystemToolsProvider(...))`，那行代码会失效。
- `ProviderRegistry.get_blob_store()` 现在会**自动解析到 memory provider**——
  注册 `SqlMemoryProvider` 即等于打开了图片外部化（详见第 4 步，这是行为变更）。
- 文末 legacy 方法（`recall_recent` / `recall_recent_by_agent` / `count_recent` /
  `supersede`）**新 provider 不实现**——它们已被 P4b-2 移出协议。宿主若有直调点，
  必须先改成协议面的 `load_view` / `fold`（见第 6 步）。

---

## 1. 依赖

```bash
pip install "ctx-weft[sql]"        # sqlalchemy>=2.0 + aiosqlite>=0.19
```

`aiosqlite` 只有 SQLite 后端需要；接 postgres 的宿主自带 `asyncpg`/`psycopg` 即可。

**这是可选依赖，且没有任何上层包 eager import 它**——缺 sqlalchemy 时
`import ctx_weft` / `import ctx_weft.providers` 照常工作（已实测，见台账 Task C2），
只有显式 `import ctx_weft.providers.memory_sql` 才会如实报 `ImportError`。

---

## 2. DDL（**先做这一步，且第 2.1 条必须最先做**）

### 2.1 ⚠️ 必须先 DROP 旧的三列唯一索引

宿主现有：

```sql
-- 旧：3 列，不含 tenant
ix_subscriptions_session_task_topic  ON memory_subscriptions (session_id, task_id, topic)
```

新 provider 的订阅幂等键**必须含 tenant**（隔离契约第 4 条：否则同 `session_id` 的
另一个租户会撞进幂等分支、拿到别人的订阅对象与游标）。若不 DROP 旧索引，
**两个租户的同 `(session, task, topic)` 订阅会撞 `IntegrityError`**——
这不是「只加 nullable 列」能覆盖的，是本次迁移唯一一条破坏性 DDL。

```sql
-- postgres：先确认它是索引还是约束
--   \d memory_subscriptions      （psql）
-- 若是 UNIQUE INDEX：
DROP INDEX IF EXISTS ix_subscriptions_session_task_topic;
-- 若是 UNIQUE CONSTRAINT（名字相同也可能是约束）：
ALTER TABLE memory_subscriptions DROP CONSTRAINT IF EXISTS ix_subscriptions_session_task_topic;
```

**验收**：`ix_subscriptions_session_task_topic` 在系统表里查不到。

### 2.2 加列（全部 nullable，存量行零迁移）

```sql
ALTER TABLE memory_events        ADD COLUMN tenant         VARCHAR(64) NULL;
ALTER TABLE memory_events        ADD COLUMN content_format VARCHAR(16) NULL;
ALTER TABLE memory_subscriptions ADD COLUMN tenant         VARCHAR(64) NULL;
```

**不要给它们加 NOT NULL 或 DEFAULT**。`NULL` 在两个列上各有确定语义：

- `tenant IS NULL` ≡ `'default'` 租户。读侧一律 `COALESCE(tenant,'default')`，
  与 core 侧的 `normalize_tenant(t) = t or "default"` 是同一条规则的 SQL 写法，
  **存量行不迁移即落默认分区**。
- `content_format IS NULL` **唯一地表示「存量行」**（见 2.4）。

### 2.3 建索引与两张新表

```sql
CREATE UNIQUE INDEX ix_subscriptions_tenant_session_task_topic
    ON memory_subscriptions (tenant, session_id, task_id, topic);

CREATE INDEX ix_memory_tenant_task  ON memory_events (tenant, session_id, layer, task_id);
CREATE INDEX ix_memory_tenant_agent ON memory_events (tenant, session_id, layer, agent_id);
CREATE INDEX ix_memory_tenant_topic ON memory_events (tenant, topic, topic_seq_no);

-- 图片等二进制字节的唯一持有者。没有 tenant 列——内容寻址本就跨租户去重。
CREATE TABLE memory_blobs (
    sha         VARCHAR(64)  PRIMARY KEY,
    media_type  VARCHAR(64)  NOT NULL DEFAULT '',
    data        BYTEA        NOT NULL,          -- SQLite: BLOB
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- 事件 → blob 的引用边。刻意无外键（见设计 §14.4）。
CREATE TABLE memory_blob_refs (
    event_id VARCHAR(64) NOT NULL,
    sha      VARCHAR(64) NOT NULL,
    PRIMARY KEY (event_id, sha)
);
CREATE INDEX ix_memory_blob_refs_sha ON memory_blob_refs (sha);
```

`ix_memory_task` / `ix_memory_agent` 两个既有索引**保持原样，名字与列都不要动**——
新 provider 的模型定义与宿主逐字一致。

**验收**：`SqlMemoryProvider` 的 `models.py` 与库里的实际 schema 对得上。
SQLite 单机部署可以直接用 `open_sqlite_memory(path)`，它会 `create_all`。

### 2.4 建议：给存量行回填 `content_format`

**不回填也能跑**，但存量行会继续走一条**启发式**：读侧对 `content_format IS NULL`
的行「试 `json.loads`，解出 list 就当结构化内容」。后果：**一条正文恰好是
`"[1, 2]"` 的存量纯文本消息会被误读成 parts**。这是继承自宿主参考实现的既有歧义
（新行已无此问题——新行一律写非空判别列，`"text"` 或 `"parts"`，三态而非两态）。

彻底治法是迁移时显式回填一次：

```sql
-- 只有当你确信存量 content 全是纯文本时（Phase 3c 之前的宿主正是如此）：
UPDATE memory_events SET content_format = 'text' WHERE content_format IS NULL;
```

若宿主存量里**确实**有 pre-E2 的 `json.dumps(list-of-dict)` 行，先用
`WHERE content LIKE '[%'` 挑出来人工核对，再分别回填 `'parts'` / `'text'`。

**验收**：`SELECT count(*) FROM memory_events WHERE content_format IS NULL` 为 0
（或为「你确认要留给启发式的那部分」）。

---

## 3. 接线：换 provider

```python
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from ctx_weft.providers.memory_sql import SqlMemoryProvider

engine  = create_async_engine(dsn)                      # 宿主自己的 engine
factory = async_sessionmaker(engine, expire_on_commit=False)
memory  = SqlMemoryProvider(factory)                    # blob_grace_period 可选

runtime.providers.register_memory(memory)
```

宿主接 postgres 时**自带 engine 与 migration**，直接构造 `SqlMemoryProvider(factory)`
即可，不必走 `open_sqlite_memory`（那是测试与单机部署用的便利函数，会 `create_all`）。

**验收**：跑一遍宿主自己的 memory 冒烟用例；ctx-weft 侧的协议一致性套
`tests/unit/test_memory_conformance.py` 是同一批断言（80 条 × provider）。

---

## 4. ⚠️ 行为变更：`get_blob_store()` 现在会自动解析到 memory

`ProviderRegistry.get_blob_store()` 的优先级是
**显式注册 > memory provider（若 `isinstance(BlobStore)` 且 `can_externalize`）> `NullBlobStore`**。

所以**注册 `SqlMemoryProvider` 这一个动作，同时打开了图片外部化**：入口收到的
inline base64 图片会被 `normalize_content` 换成 `blob:<sha>` ref 存进 memory，
出网前由 gateway 还原。这是裁定 D4 的本意，但对存量宿主是「换 provider 顺带
打开了新行为」，需要知情：

- 事件 payload / memory 行里从此**不再有 base64**，只有短 ref 标记；
- 图片字节全部落在 `memory_blobs` 表——**事件库任何情况下都重建不出一张图**，
  备份策略要把这张表算进去；
- 不想要这个行为，可以显式 `register_blob_store(NullBlobStore())` 覆盖
  （显式注册优先级最高）；纯内存 provider 按裁定 D6 不实现 `BlobStore`，回落
  `NullBlobStore`，行为逐字节不变。

**验收**：接线后 `runtime.providers.get_blob_store()` 返回的是那个
`SqlMemoryProvider` 实例本身（不是 `NullBlobStore`）。

---

## 5. ⚠️ `collect_blobs` 必须由宿主自己定时调

`collect_blobs(now=None) -> int` **不在 `MemoryProvider` 协议里**，是
`SqlMemoryProvider` 的自有方法，**ctx-weft 里没有任何调用点**——这是刻意的：
回收时机是运维决策，core 不该在任何写路径上触发删字节。

```python
# 例：每小时一次的后台任务
deleted = await memory.collect_blobs()
```

- **幂等**，可随时重跑，可并发重入（多删一次也只是删不到）。
- **两条判据同时成立才删**：① 没有任何 `is_superseded = 0` 的引用者；
  ② `created_at` 已过宽限期（默认 **24 小时**，`SqlMemoryProvider(...,
  blob_grace_period=timedelta(hours=N))` 可调）。
- **宽限期是正确性要求，不是优化**：`put` 与 `ingest` 之间存在时序窗口
  （进程内是毫秒级，但中间可能隔着 HITL park——那能等人数小时）。窗口里的 blob
  没有任何引用边，没有宽限期就会被清扫误删，图**永久丢失**。
- **不调用的后果是「blob 只涨不删」**（泄漏磁盘），**不会**产生悬空 ref。
  即：忘了配定时任务不会坏数据，只会费磁盘。

**验收**：定时任务上线后观察 `SELECT count(*) FROM memory_blobs` 不再单调上涨。

---

## 6. 宿主自己的 event model 做多模态改造时的五个坑

宿主的参考实现 `providers/memory/postgres.py`（462 行）落后于当前协议七处，
下面五条是真会咬人的（另两处是 legacy 方法与 `recall_topic` 语义，见第 7 步）：

1. **`json.dumps(event.content)` 对 dataclass 直接 `TypeError`。** 必须走
   `ctx_weft.core.content.content_to_jsonable` / `content_from_jsonable`。
2. **读侧不要靠猜。** 参考实现「试 `json.loads`，是 list 就当结构化内容」会把正文
   恰为 JSON 数组的用户消息误判。新行**一律写非空判别列**（`"text"` / `"parts"`），
   `NULL` 因此唯一表示存量行——**两态改三态**是这次的关键设计。
3. **SQLite 会静默丢 tzinfo。** 裸 `DateTime(timezone=True)` 在 SQLite 上回读得到
   naive datetime，排序照常工作，但**等值比较恒 False**。新 provider 包了一层
   `UtcDateTime` TypeDecorator（在 postgres 上是恒等变换）。宿主若共用模型文件，
   直接用它。
4. **tenant 必须进列 + 索引/唯一键**，不能只在应用层过滤。写读两侧用**同一条**
   归一规则（`COALESCE(tenant,'default')`）。
5. **`MemoryEventModel` 与宿主的 `SessionModel` / `TaskModel` / `EventModel`
   共用同一个 `Base`。** 拆开意味着两个注册表、`create_all` 各建各的；宿主的
   `observability/session_export.py` 与 `session_import.py` 都直接 import 了
   `MemoryEventModel`，会被波及。**这是本次迁移最大的一块工程摩擦，且不在
   ctx-weft 仓内**——建议单列一个任务，并先做只读核对（列清楚哪些文件会被波及）
   再动手。

---

## 7. 迁移前必须先改掉的宿主调用点

- **legacy 方法**：`recall_recent` / `recall_recent_by_agent` / `count_recent` /
  `supersede` **新 provider 不实现**（P4b-2 已移出协议）。宿主有直调点的话，
  改成 `load_view(address, scope, ctx, kinds=...)` 与
  `fold(supersede_ids, replacements, ctx)`。
  顺带一提：这些方法里的 tenant 比较**本来就是空转**（等式两边都用读侧 ctx 的
  tenant，约掉了），所以它们**没有**任何隔离——不改而是继续用，等于把已修好的洞
  留一扇后门。
- **`recall_topic` 不比 `session_id`**（新旧两个实现一致、协议未定）。这意味着
  **同租户内跨 session 的同名 topic 是共享的**。宿主若自定过 `long_term_*` 之类的
  固定 topic 名，切换前请确认这正是你要的语义（详见设计 §14.7 的 L25）。

---

## 8. 验收清单（照着勾）

- [ ] `pip install "ctx-weft[sql]"` 完成
- [ ] `ix_subscriptions_session_task_topic`（3 列）**已 DROP**
- [ ] `memory_events.tenant` / `memory_events.content_format` /
      `memory_subscriptions.tenant` 三列已加，且**均为 nullable、无 DEFAULT**
- [ ] `ix_subscriptions_tenant_session_task_topic`（4 列 unique）已建
- [ ] `ix_memory_tenant_task` / `_agent` / `_topic` 已建
- [ ] `memory_blobs` / `memory_blob_refs` 两表已建（含 `ix_memory_blob_refs_sha`）
- [ ] 存量行 `content_format` 已回填（或已确认不回填的风险）
- [ ] `register_memory(SqlMemoryProvider(factory))` 已接
- [ ] 已知晓 `get_blob_store()` 自动解析 = 外部化自动开启，且备份包含 `memory_blobs`
- [ ] `collect_blobs()` 的定时任务已配
- [ ] 宿主对 legacy 四方法的调用点已清零
- [ ] 两个租户用**同一个 `session_id`** 跑一遍冒烟：A 读不到 B 的行，
      B 发布不会把 A 的行标 superseded，两边订阅各自独立

---

## 9. 回滚

DDL 全是加列/加索引/加表（除 2.1 那条 DROP），回滚方向：

1. 代码切回旧 provider；
2. 重建旧的 3 列唯一索引——**但先确认此时库里没有「同 (session,task,topic)
   不同 tenant」的订阅行**，有的话重建会失败，需要先决定保留哪一条；
3. 新增的三列与两张表可以留着不管（旧 provider 不读它们），也可以 DROP。

**不可回滚的一件事**：切换期间新写入的图片，字节只在 `memory_blobs` 表里，
事件行里只有 ref。回滚到旧 provider 后那些图取不回来（旧 provider 不认识 ref）。
