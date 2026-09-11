# Tasks: reliability-wp4

> 完成 = H1/H2 发布单元（WP2+WP3+WP4）仓内齐备，按方案 §3 宣布解决。
> 硬边界：不动提交门/provisional、不动 reducer 语义；h3 判定保持复现（WP5/6 未做）。

## 1. 类型与双 store 持久化

- [x] 1.1 `RunSnapshot` 加 `last_commit_position: int | None = None` + `projection_version: int = 1`（尾部默认，旧构造零破坏）；in_memory save/load 直传；SQL `event_snapshots` 加两列 + `open_sqlite_event_store` pragma 探测式幂等 ALTER；旧行读出 None/1 → 触发忽略重建；快照字段往返单测（含旧行回落）
- [x] 1.2 SQL 幂等 ALTER 兼容冒烟：旧 schema 快照库（无新列）经 open 路径升级后可写可读（沿用 WP2 的迁移工具冒烟手法）

## 2. 快照创建三步切面

- [x] 2.1 `SnapshotWriter` 改造：触发信号（every_n 计数）不变，写时 `C = committed_head` → `read_range(0..C)` 全量折 → 存 blob + `last_commit_position=C` + 当前 `projection_version`；**删除**「rebuild_view 无上界 + 触发事件 ID 当游标」旧路径；无 OrderedEventStore 能力的 store（自定义宿主）回落旧行为并告警（兼容口）
- [x] 2.2 新建 `tests/unit/test_snapshot_consistent_cut.py`：延迟提交不丢（E-T04：B 先提交触发快照、A 后提交，再快照/恢复见 [a,b]）、取 head 后并发提交不混入（E-T05：blob ≤ C、新事件 delta 应用一次）、写快照含 projection_version

## 3. 恢复路径切换与等价

- [x] 3.1 `rebuild_view` 改造（D3 伪码）：快照有 position 且版本匹配且 ≤ head → position 增量；否则忽略快照全量回放；`> head`（引用未来）判损坏走全量；grep 收口确认 `read_after` 无生产调用点，docstring 标 legacy
- [x] 3.2 两路等价测试（E5/D6）：双轮交错夹具（延迟提交+快照+再提交+再快照），全量 vs 快照+增量逐字段 diff（session/task/agent/HITL/outputs，不只 task 键集）
- [x] 3.3 legacy/损坏降级（E-T06）：无 position 旧快照、版本不匹配、blob 损坏三形态 → 忽略+全量重建+后续新快照带 position；既有 `test_snapshot_recovery.py` 全量回归（兼容路径），补两枚等价迁移用例

## 4. 迁移端到端与判定翻转

- [x] 4.1 新建 `tests/integration/test_event_position_migration.py`（E-T13）：旧 schema 库 → `migrate_event_positions.py --execute` → 新 Runtime 起动 → 事件可读、快照按 position 写/恢复、业务字段等价
- [x] 4.2 翻转 WP0 夹具 `test_snapshot_commit_interleaving`：两条恢复路径都见 [a,b]，快照 cursor 断言为 position（保留旧契约对照注释）
- [x] 4.3 验证脚本翻转：探针 `late_commit` 按接口演进改接 gate+切面（交错不删、断言指向 fixed）→ `--expect fixed` H2 转 True；无桩 h2 defect_reproduced 转 false / fixed=true 并按新契约断言；**h3 判定保持复现**、h1/h4 保持修复
- [x] 4.4 全量回归 `uv run pytest tests/unit tests/integration -q -W ignore`：仅 2 既知预存失败，零新增

## 5. 收尾

- [x] 5.1 ARCHITECTURE.md §11 快照段改写（三步切面、两路等价、legacy 降级、`read_after` 标 legacy）；README 事件系统节快照句更新
- [x] 5.2 `openspec validate` 通过；总纲任务组 4 勾选 + 记分板更新（**H1/H2 解决**声明落此）；提交拆分：类型+store / 切面+恢复 / 迁移+翻转 / docs+chore
