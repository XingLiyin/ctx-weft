# Design: reliability-wp2

## Context

上游方案 §4.3（新增最小接口，名称已固定）/§4.4（Store 实现与事务）/§4.9（迁移）。现状根因（验证报告 + 无桩验证已坐实）：

- SQL store `append`（`sql/store.py:54`）每条一个事务；读取按 `EventModel.id`（ULID 时间序）排序——`read_after` 以 ID 为游标。
- 事件 ID 在创建时铸造（`core/utils/ids.py`），并发未提交窗口（TaskManager begin/commit_round 包裹的 bus provisional）使「后提交的旧 ID」真实可达（无桩 H2：快照恢复丢 task a）。
- 内存实现靠 dict 插入序，同样没有显式提交位置。

关键约束：**本 change 落地后运行时行为零变化**（persister 走 append() → 单事件批次，commit 语义等价）；WP3 才把发射侧切到 CommitGate+批次，WP4 才把恢复切到 position 游标。这是「能力新增、暂不启用」的铺路形态。

## Goals / Non-Goals

**Goals:** OrderedEventStore 协议 + 内存/SQL 双实现 + conformance（数据库级并发）+ 迁移工具 dry-run；append 兼容零变化。

**Non-Goals:** 不改 persister/bus/runtime 接线（WP3）；不动快照与 `read_after`（WP4）；不翻转 WP0 夹具与两个验证脚本的 H1/H2 判定；不做 PostgreSQL 真库集成（连接形态就位，验证归 WP8 的独立 integration 配置，不在本包静默宣称已支持）。

## Decisions

### D1：协议与实现的关系——扩展而非替换

`OrderedEventStore` 是 `EventStore` 的**扩展协议**（新增三个方法），不是平行接口：内存/SQL 两个既有 store 类直接加方法，`read_after(id)` 等 legacy 读取原样保留。方案 §4.3「名称在此固定，不另造第二套」按字面执行——类型放 `protocols/events.py`，与 Event/EventStore 同处。

- 备选：独立的新 store 类族（旧新并存）——两套实现双维护、迁移期状态分裂，弃。

### D2：SQL 事务形态——head 行 + 同事务批次

每会话一行 head（`session_id, next_position`）。`append_batch` 在**同一事务**内：`UPDATE head SET next_position = next_position + N`（持锁）→ 拿到起始 position → 整批 INSERT → INSERT 批次表 receipt → COMMIT。

- SQLite：`BEGIN IMMEDIATE`（写锁先取，串行化 head 更新；aiosqlite 下即 connection 级 `PRAGMA busy_timeout` + 立即写事务）。
- PostgreSQL（连接形态就位）：head 行 `SELECT ... FOR UPDATE`。
- 明令禁止 `MAX(position)+1` 无锁读改写——conformance 的双连接争用用例专测这一点。
- 唯一约束三重：`(session_id, position)`、`batch_id`、`event.id`（event.id 全局唯一已有则沿用；无则在批表/事件表补）。

### D3：幂等索引与冲突判定——receipt 全文比对

两实现都在提交路径查 batch_id：命中 → 逐字段比对原批事件（envelope+payload，**剔除 position**）→ 全同返回原 receipt；任异抛 `EventConflictError`。比对材料存批次表（SQL：批表存 event id 列表，事件行本身即全文；内存：`{batch_id: (receipt, events)}`）。不为幂等比对引入规范化序列化——逐字段 dict 比对足够且不引入 JSON 序列化歧义。

### D4：append 兼容的等价性证明方式

`append(event)` → `append_batch(session, batch_id=event.id, [event])`。幂等键=event.id 与既有「同 id 重复 append」的行为天然对齐（既有 store 的 append 重复 id 如何处理？实施第一步先钉 conformance：当前行为是什么就保留什么，不趁机改语义）。`read_by_session` / `read_session_events_of_types` 改按 position 排序（同会话内与 ID 序的差异在「后提交旧 ID」下才出现——当前运行时逐条同步提交，二者一致；WP3 启用批次后才可能分叉）。

### D5：内存实现——一把 asyncio.Lock

单锁保护「head 分配 + 幂等查询 + 批量写入」短临界区；跨会话不互相阻塞的隔离性由「锁粒度=全局」暂时妥协（进程内单 asyncio 环境下争用本来就低），conformance 的跨会话用例只断言正确性不断言并行度。SQL 实现不做此妥协（方案明令跨会话互不阻塞——head 按会话分行天然满足）。

### D6：表结构演进与建表幂等

SQL models 新增 head 表与批次表；events 表加 `position` 列（nullable，迁移回填）。`create_all` 幂等建表对新增表无感；对既有库的加列需要显式 `ALTER TABLE` 兼容路径（SQLite 支持 ADD COLUMN）——放进 `open_sqlite_event_store` 的建表段，与迁移工具共用回填函数。

### D7：迁移工具——只做 dry-run 与显式执行两层

`scripts/migrate_event_positions.py --db <path> [--execute]`：无 `--execute` 只输出 JSON 报告（会话数/事件数/不完整事件）；有则按 `(session_id, event.id)` 排序回填 position 与 head。**报告与文档都明写**：分配的是确定性顺序，不是历史提交顺序（旧契约没记它）。不删旧列、不双写。

### D8：conformance 的参数化与数据库级并发

`test_ordered_event_store_conformance.py` 用 fixture 工厂参数化内存 / SQLite（tmp_path 文件）；「双连接争用」用例开**两个独立 session factory**（两个连接池），`asyncio.gather` 并发提交同会话批次，断言 position 无重复。PostgreSQL 不在本包跑（WP8 接独立 integration 配置），代码里连接形态（`make_session_factory` 的 pg URL + head 行锁路径）就位。

## Risks / Trade-offs

- [SQLite 并发下的 busy/锁错误被误判为冲突] → busy_timeout 显式设置 + 冲撞用例重跑三次稳定后再断言；OperationalError 与 EventConflictError 分类明确。
- [append 改道后既有 conformance 语义漂移] → 实施第一步先为「当前 append 重复 id 行为」钉基线用例，改道后逐一对照。
- [批表与幂等比对的存储成本] → receipt 只存 event id 列表 + 计数，全文比对走事件行；不做哈希摘要（冲突概率换实现复杂度不划算，方案也只要求可判定）。
- [「行为零变化」被无意破坏] → 全量回归（unit 2841+ / integration 107+ 基线）+ 两个验证脚本 H1/H2 判定不变作为门禁。

## Migration Plan

新表/加列向前兼容（旧代码不读新列）；迁移工具独立可重跑；回滚 = 还原代码（新列留存无害）。生产宿主切日志格式是 WP2–WP4 联合验收（WP4）之后的事，本包不动任何 writer。

## Open Questions

（无——接口名称、事务形态、幂等语义、迁移口径均由方案 §4.3/§4.4/§4.9 固定，本设计只做落地取舍。）
