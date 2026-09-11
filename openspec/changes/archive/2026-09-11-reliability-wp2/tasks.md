# Tasks: reliability-wp2

> 发布单元纪律：本 change 完成 ≠ H1/H2 解决（WP3/WP4 未做）。WP0 夹具与两个验证脚本
> （verify_agent_architecture.py / verify_no_stubs_e2e.py）的 H1/H2 判定必须保持复现。

## 1. 协议与类型（specs/event-log 的契约面）

- [x] 1.1 `protocols/events.py` 新增 `StoredEvent` / `CommitReceipt` / `OrderedEventStore`（`append_batch` / `read_range` / `committed_head`）与 `EventConflictError`（名称照抄方案 §4.3，docstring 注明「实施期间不另造等价第二套接口」），验证 `tests/unit/test_protocols_imports.py` 与新增协议形状单测通过
- [x] 1.2 先钉基线：为「当前 append 对重复 event.id 的行为」补 conformance 用例（现状是什么就断言什么），记录输出作为改道前后对照

## 2. 内存实现

- [x] 2.1 `in_memory/store.py`：`asyncio.Lock` 短临界区保护 head 分配 + 幂等索引（`{batch_id: (receipt, events)}`）+ 批量写入；`append` 改道单事件 `append_batch`（batch_id=event.id）；`read_by_session`/`read_session_events_of_types` 按 position 排序，验证既有 `test_event_store_conformance.py`（内存参数）全绿

## 3. SQL 实现

- [x] 3.2 models：新增会话 head 表（session_id, next_position）与批次表（batch_id, session_id, event_ids, created_at）；events 表加 nullable `position` 列；`open_sqlite_event_store` 建表段补幂等 `ALTER TABLE` 兼容路径
- [x] 3.3 `append_batch`：同事务内 head 加锁分配（SQLite `BEGIN IMMEDIATE` / PG head 行锁形态就位）→ 整批 INSERT → receipt 落批表；三重唯一约束 `(session_id, position)` / `batch_id` / `event.id`；禁 `MAX+1`；`append` 改道；`read_by_session` 等按 position 排序，验证既有 conformance（SQL 参数）全绿
- [x] 3.4 幂等与冲突：同 batch_id 同内容（忽略 position）返回原 receipt；异内容抛 `EventConflictError`——SQL 与内存两参数各一组用例

## 4. conformance（参数化内存 + SQLite）

- [x] 4.1 新建 `tests/unit/test_ordered_event_store_conformance.py` 覆盖 spec 全部 8 条 requirement 的场景：位置递增与 committed_head / 批中第 k 条失败整批回滚 / 确认丢失原样重试 / 同 batch_id 异内容冲突 / append 与批次共存 / 两会话并发互不阻塞 / **双连接同会话争用**（两个独立 session factory，asyncio.gather，position 无重复）/ read_range 边界（after=2, through=4 → 3,4），全部通过
- [x] 4.2 `read_after(id)` legacy 路径回归：既有快照恢复测试（test_snapshot_recovery.py）与本 change 行为零变化承诺一并验证

## 5. 迁移工具

- [x] 5.1 新建 `scripts/migrate_event_positions.py`：默认 dry-run 输出 JSON 报告（会话数/事件数/不完整事件），`--execute` 才写入；按 `(session_id, event.id)` 排序回填 position 与 head；报告与 docstring 明写「确定性顺序 ≠ 历史提交顺序」，单测覆盖 dry-run 默认与执行后 position 连续性

## 6. 门禁与收尾

- [x] 6.1 全量回归 `uv run pytest tests/unit tests/integration -q -W ignore`：与基线一致（unit 2841 过/1 预存、integration 107 过/1 预存 + 本包新增），零新增失败
- [x] 6.2 验证脚本判定不变：`verify_agent_architecture.py --expect baseline` H1/H2 仍 true；`verify_no_stubs_e2e.py h1/h2` 的 defect_reproduced 仍 true（H1/H2 未解决的诚实证据）
- [x] 6.3 文档：ARCHITECTURE.md §11 补一段「position/批次（OrderedEventStore，WP3 起启用）」；README 事件节一句带过；`openspec validate "reliability-wp2"` 通过
