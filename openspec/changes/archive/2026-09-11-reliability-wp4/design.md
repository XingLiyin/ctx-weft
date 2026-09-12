# Design: reliability-wp4

## Context

无桩 h2 实测：B 触发快照（游标=evt_0004）后 A 才提交 → 全量 [a,b] vs 快照恢复 [b]。原料已齐：WP2 的 position/committed_head/read_range、WP3 的「观察者只见已确认流」。现状关键代码：

- `providers/events/snapshot.py::SnapshotWriter`：订阅非 provisional、按 every_n 计数触发；写时 `rebuild_view`（无上界）+ 游标取触发事件 ID。
- `core/control/reducers.py::rebuild_view:308`：`load_latest_snapshot` → `read_after(snapshot.last_event_id)` → `apply_events`。
- `RunSnapshot`（protocols/events.py）：id/run_id/session_id/last_event_id/last_event_sequence/state_blob/…，无提交位置概念。
- SQL `event_snapshots` 表与 in_memory dict 两套 save/load。

## Goals / Non-Goals

**Goals:** 三步一致切面算法；两路恢复等价（E5）；legacy/损坏快照忽略重建；双 store 字段持久化（SQL 幂等 ALTER）；迁移端到端；三处判定翻转（夹具/无桩 h2/探针 H2）。**本 change 落地 = 方案 §3 的 H1/H2 发布单元（WP2+WP3+WP4）仓内齐备。**

**Non-Goals:** 不动提交门/provisional（WP3 已定）；不动 reducer/apply 语义；不承诺快照失败零损失（日志是真相）；PostgreSQL 真库（WP8）。

## Decisions

### D1：快照内容 = head 截断后的**全量折**（P1）

`SnapshotWriter` 写快照时不再 `rebuild_view`（无上界）也不维护增量 view，而是：`C = committed_head` → `read_range(0..C)` → `apply_events` → 存 blob+`last_commit_position=C`。

- 备选（弃）：增量维护 writer 内存 view（只 apply 新事件）——快照 writer 与恢复路径两套 apply 状态机，等价性要额外证明；全量折单一 apply 语义，E5 天然成立。
- 代价：每次写快照 O(日志前缀)。触发点低频（every_n 默认 0=不接；接的宿主自定频率；RunFinished 边界），且「日志增长但快照后 delta 固定时恢复读取行数不随总日志线性增长」的目标只约束**恢复读**，不约束快照写（方案 §10.1 明示）。

### D2：触发语义——计数信号不变，边界自取（P1 收尾）

every_n 计数与触发事件类型沿用现状（信号）；写快照那一刻**自取** `committed_head` 作 C——触发事件只是"现在做一次"的提醒，不是边界（方案 §4.8 原文）。WP3 之后 writer 收到的都是已确认事件，C ≥ 触发事件 position 恒成立，不会漏折触发事件本身。

### D3：RunSnapshot 版本化与恢复切换（P2/P3）

`RunSnapshot` 加 `last_commit_position: int | None = None`、`projection_version: int = 1`（dataclass 尾部默认值，旧构造零破坏）。`rebuild_view` 改为：

```
snap = load_latest_snapshot(sid)
head = committed_head(sid)
if snap and snap.projection_version == _PROJECTION_VERSION and snap.last_commit_position is not None:
    if snap.last_commit_position > head: 快照引用未来位置（数据异常）→ 忽略走全量
    view = deserialize(snap.state_blob)
    delta = read_range(sid, after=snap.last_commit_position, through=head)
    return apply_events(delta, view)
view = apply_events(read_range(sid, through=head), 空)   # legacy/损坏/无快照
```

`read_after(id)` 保留为 legacy API：`rebuild_view` 不再调用；docstring 标注废弃指向（删除留 WP8 审计后）。

### D4：双 store 持久化与 SQL 幂等 ALTER

- in_memory：字段随 dataclass 直传。
- SQL：`event_snapshots` 加 `last_commit_position INTEGER NULL`、`projection_version INTEGER DEFAULT 1` 两列；`open_sqlite_event_store` 建表段复用 WP2 的 pragma 探测式 ALTER；读侧旧行回落 None/1 → 触发 D3 的忽略重建。
- 迁移工具不动（WP2 已交付 `--execute`）；本 change 新增的是**端到端迁移验收测试**（旧 schema 库 → 迁移 → 新 Runtime 起动 → 快照/恢复 position 路径全通）。

### D5：判定翻转的成对清单（D7 风格）

| 载体 | 旧断言 | 新断言 |
|---|---|---|
| WP0 夹具 `test_snapshot_commit_interleaving` | 快照恢复只见 [b] | 两条恢复路径都见 [a,b]；快照 cursor 为 position |
| 探针 `late_commit`（H2） | snapshot_replay=[b] | 接口演进：接 WP3 gate + WP4 切面后两条路径 [a,b]（交错不删、断言指向 fixed） |
| 无桩 h2 | defect_reproduced=true | 两条路径等价 [a,b]，defect=false/fixed=true |
| `test_snapshot_recovery.py` 既有用例 | 快照+read_after 路径 | 兼容运行（构造新字段路径 + legacy 快照忽略重建的等价用例补两枚） |

### D6：E5 等价性怎么测才不假

对照两路恢复时归一化随机 ID/时间戳，比较面**至少** session/task/agent/HITL/outputs 字段（不只 tasks 键集合——方案 §9.1 明令）；夹具构造「延迟提交 + 快照 + 再提交 + 再快照」双轮交错，两路结果逐字段 diff。

## Risks / Trade-offs

- [快照写放大] → 低频触发 + 写耗时随验收记录（基准表）；宿主可 every_n=0 关闭。
- [旧快照全部失效的首启全量回放] → 一次性代价；与崩溃恢复同量级。
- [快照引用未来位置（迁移/回滚错配）] → D3 显式判 `> head` 走忽略重建，不猜。
- [read_after 残留调用点漏改] → grep 收口 + 测试断言 rebuild_view 路径不再触碰它。

## Migration Plan

无数据迁移新增（WP2 工具复用）；旧快照行自动降级重建；回滚 = revert（新列留存无害）。生产宿主切日志格式的门=本 change 落地（WP2–WP4 联合验收仓内达成）+ WP8 全量矩阵。

## Open Questions

（无——P1/P2/P3 在探索中定夺，方案 §4.8/§4.9 固定其余细节。）
