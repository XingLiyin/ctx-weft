# Proposal: reliability-wp4

> 上游方案：`docs/plans/2026-09-11-agent-core-reliability-plan.md` **WP4（快照边界、完整回放与迁移）**，§4.8/§4.9/§8-WP4。
> 前置：reliability-wp2（position/append_batch/read_range/committed_head）、reliability-wp3（提交门——观察者只见已确认流）均已归档。
> 总纲：reliability-remaining-defects 任务组 4；本 change 完成后按方案 §3 **宣布 H1/H2 解决**（WP2+WP3+WP4 三者齐备）。

## Why

H2（快照恢复丢事件）仍复现（无桩验证：全量回放见 task a、b，快照增量恢复只见 b）。根因：快照游标取触发事件 ID（snapshot.py:87-96）、恢复增量按 `read_after(id)` 过滤（reducers.py:320）——事件 ID 是铸造序（ULID），提交顺序可与之不同（并发未提交窗口），延迟提交的旧 ID 永久落在游标之外。

WP2/WP3 已备齐全部原料：position（存储分配的提交位置）、committed_head、read_range、以及「观察者只见已确认提交流」的语义。WP4 是纯组装：把快照边界从「触发事件 ID」换成「已确认提交位置 C 的一致切面」，并把恢复增量从 ID 过滤换成 position 区间。

## What Changes

- **快照创建三步算法**（SnapshotWriter 改造）：`C = committed_head(session)` → `read_range(0..C)` 全量折 view → 保存 `snapshot(view, last_commit_position=C, projection_version=V)`。快照触发事件只是「请求做快照」，不再作为边界；**不取无上界的最新 view 再把较早触发位置写成游标**（方案明令禁止的形态）。低频后台全量折（every_n 或 RunFinished/SessionFinished 触发点沿用），单一 apply 语义让 E5 两路等价天然成立。
- **RunSnapshot 版本化**：新增 `last_commit_position: int | None = None` 与 `projection_version: int = 1`；旧字段（last_event_id/sequence）保留只读兼容。
- **恢复路径切换**（`rebuild_view`，唯一消费快照的恢复入口）：快照有 position 且版本匹配 → `read_range(S.cursor+1 .. committed_head)` 增量；快照损坏 / 版本不匹配 / legacy 无 position → **忽略该快照**，`read_range(0..head)` 全量回放重建（失败的快照是性能降级不是数据丢失）。`read_after(id)` 降为纯 legacy API（无调用点，标注废弃）。
- **双 store 快照字段持久化**：in_memory 直传；SQL `event_snapshots` 表加两列（`last_commit_position`/`projection_version`），幂等 ALTER 兼容路径（与 WP2 同款）；旧快照行读出为 None/1 → 走忽略+重建。
- **迁移实装与联合验收**：`scripts/migrate_event_positions.py --execute` 的端到端校验（迁移旧库 → 新 Runtime 起动 → 快照/恢复按 position）；新建 `tests/integration/test_event_position_migration.py`（方案 E-T13）。**WP2–WP4 联合验收通过后才允许生产宿主切日志格式**（本 change 落地即达成联合验收的仓内部分；PostgreSQL 真库归 WP8）。
- **翻转**：WP0 夹具 `test_snapshot_commit_interleaving`（两条恢复路径都见 a、b）；无桩验证 h2 判定翻转（defect_reproduced → false）；探针 H2 翻转（接口演进：`late_commit` 场景改验 position 路径，故障交错不删、断言不放宽）。

## 不做什么

- 不改提交门/provisional 语义（WP3 已定）；不动 OperationStore（WP5）。
- 不承诺快照创建失败时数据不丢（快照本就是性能优化，日志是真相——方案 §4.8）。
- 不做 PostgreSQL 真库验证（WP8 独立 integration 配置）。
- `apply_events`/reducer 语义零改动（快照只是恢复加速器）。

## Capabilities

### New Capabilities

- `snapshot-recovery`: 快照恢复一致切面契约——committed_head 截断、全量与快照+增量两路等价、不可用快照降级（承自总纲已评审稿，3 条 requirement）。

### Modified Capabilities

（无——`event-log` 的存储契约与 `event-commit` 的提交契约不变；WP4 只消费它们。）

## Impact

- **代码**：`providers/events/snapshot.py`（三步算法）、`core/control/reducers.py::rebuild_view`（恢复切换）、`protocols/events.py::RunSnapshot`（两字段）、两个 store 的 save/load_snapshot（字段持久化 + SQL 幂等 ALTER）。
- **测试**：新建 `tests/unit/test_snapshot_consistent_cut.py` + `tests/integration/test_event_position_migration.py`；翻转夹具与两个验证脚本；既有 `test_snapshot_recovery.py` 全量回归（旧快照兼容路径）。
- **风险**：快照写放大（每次全量折）——低频触发下可接受，基准数据随验收记录；旧快照忽略重建的首启代价——一次性。
