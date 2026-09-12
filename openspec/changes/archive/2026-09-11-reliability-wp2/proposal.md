# Proposal: reliability-wp2

> 上游方案：`docs/plans/2026-09-11-agent-core-reliability-plan.md` **WP2（有序日志与原子批次）**，§4.3/§4.4/§8-WP2。
> 前置：reliability-wp0-wp1 已完成（WP0 夹具钉住 H1/H2/H3 现象；探针与无桩验证 `verify_no_stubs_e2e.py` 均已复现）。

## Why

H1/H2 的存储层根因：EventStore 没有「提交位置」概念——SQL 实现逐条 append 各自独立事务（`sql/store.py` append），读取按事件 ID（ULID，创建时铸造的时间序）排序过滤；而提交顺序可以不同于 ID 顺序（并发未提交窗口，无桩验证 H2 已实证：B 触发快照后 A 才落库，A 的事件永久落在游标之外）。没有原子的批次提交，就没有可声明的提交事实（H1）；没有稳定的提交位置，就没有正确的快照边界（H2）。

本 change 给 EventStore 打地基：**提交位置（position）+ 原子批次（append_batch）+ 幂等重试（batch_id）**。它是 WP3（提交门/通知分离，解决 H1）与 WP4（快照一致切面，解决 H2）的必要前置。

## What Changes

- **新增协议**（`protocols/events.py`，名称按方案 §4.3 固定，不另造第二套）：`StoredEvent`（event + position）、`CommitReceipt`（batch_id + records）、`OrderedEventStore`（`append_batch` / `read_range(after_position, through_position)` / `committed_head`）、`EventConflictError`。
- **内存实现**：一把提交锁保护批次写入、幂等索引与会话位置分配。
- **SQL 实现**：会话 head 行 + 批次表，同一事务内分配位置、插入整批、记录 receipt；约束至少含 `(session_id, position)` 唯一、`batch_id` 唯一、`event.id` 唯一。SQLite 用可串行化 head 更新的事务方式，PostgreSQL 用 head 行锁；禁止无锁 `MAX(position)+1`。
- **append 兼容**：`append(event)` 改为单事件 `append_batch` 实现（batch_id 确定性取 event.id）——persister 等既有调用方零改动。
- **幂等语义**：相同 batch_id + 相同内容（envelope/payload，忽略存储分配的 position）→ 返回原 receipt；相同 batch_id + 不同内容 → `EventConflictError`。
- **迁移工具** `scripts/migrate_event_positions.py`：默认 dry-run，报告会话数/事件数/不完整事件；旧数据按 `(session_id, event.id)` 排序分配 position——**不据此声称还原了历史实际提交顺序**（方案 §4.9）。
- **conformance 测试**参数化跑内存与 SQLite：部分写入回滚、同 batch 重试、ID 内容冲突、同会话两连接争用（数据库并发，不以单协程测试代替）、不同会话互不阻塞。

## 不做什么（发布单元纪律）

- **不装 CommitGate、不分离 required/观察者通知**（WP3）——本 change 落地后运行时行为**零变化**（persister 仍走单事件 append 路径，只是底下换了实现）。
- **不动快照算法与恢复读取**（WP4）——`read_after(id)` 保留 legacy 语义，新版快照恢复仍不启用。
- **不宣称 H1/H2 已解决**：方案 §3 明令 WP2–WP4 三者完成前不得宣布；WP0 夹具（`test_runtime_storage_failure` / `test_snapshot_commit_interleaving`）继续钉住缺陷现状，翻转归属仍是 WP3/WP4。
- 不改 `verify_agent_architecture.py` / `verify_no_stubs_e2e.py` 的判定（H1/H2 仍应复现）。

## Capabilities

### New Capabilities

- `event-log`: 事件日志的有序提交契约——同会话提交位置唯一单调、批次原子性、batch_id 幂等与冲突检测、append 兼容、跨会话隔离、并发争用安全、按位置读取、legacy 迁移确定性。

### Modified Capabilities

（无——`capability-gateway` 不受影响。）

## Impact

- **代码**：`src/ctx_weft/protocols/events.py`（新类型与协议）、`providers/events/store/in_memory/store.py`、`providers/events/store/sql/{store,models}.py`（实现 + 表结构）、新增 `scripts/migrate_event_positions.py`。
- **测试**：新建 `tests/unit/test_ordered_event_store_conformance.py`（参数化内存/SQLite）；既有 `test_event_store_conformance.py` 必须全绿（append 兼容）。
- **风险**：SQL 表结构演进（新表 + 既有表加列）需要建表幂等；SQLite 双连接争用的 busy-timeout 处理；行为零变化的承诺由全量回归背书。
