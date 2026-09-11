# Design: reliability-remaining-defects

## Context

上游方案 §4.5–§4.10（H1/H2 的提交门、provisional、快照切面、迁移）、§5（H3 的身份/账本/策略/处置）、§6（H5 限制）、§7（H6 拆分）已把绝大多数决策写死——本设计不做二次发明，只记录落地取舍与本地事实（夹具翻转点、验证脚本门禁、附带发现的归属）。前置：reliability-wp2（OrderedEventStore 地基）必须先行落地，WP3/WP4 依赖其 append_batch/position。

## Goals / Non-Goals

**Goals:** 按 WP3→WP4→WP5→WP6→WP7（→WP8 验收→WP9 可选）渐进修复 H1/H2/H3/H5（H6 仅在获得收益证据时做）；每 WP 独立提交可回滚；夹具与验证脚本的翻转作为各 WP 的验收门禁。

**Non-Goals:** 不重做方案已定的决策；不在 WP9 无证据时强拆 Runtime；PostgreSQL 真库验证归 WP8 独立配置；不为 H3 承诺对外部系统的 exactly-once（方案明令不据此声称）。

## Decisions

### D1：实施顺序与发布单元（沿方案 §3 硬约束）

WP3（提交门）→ WP4（快照切面）合为 H1/H2 发布单元，两者齐前不宣称解决；WP5（身份+账本）→ WP6（策略+处置）合为 H3 单元；WP7 独立；WP8 全量验收矩阵；WP9 单独否决权。单 change 多 commit：每 WP 一个（或一对「测试+实现」）commit，tasks.md 按 WP 分组勾选。

### D2：CommitGate 接线形态（WP3）

新建 `core/events/commit_gate.py`（编排层，Provider 不反向依赖 Runtime）：构造期替换 `attach_persistence` 的 persister 接线（保留 `runtime.event_bus` 公开入口与 `attach_persistence` 兼容路径）；required consumer 名单显式登记（ALM/SessionRegistry 的 subscribe(provisional=True) 调用点改为经 gate 注册）；`EventsDropped` 真正实现（TRANSIENT 集合内的元事件，payload 带被丢事件的 position 区间——依赖 WP2 的 position 作补读游标）。**有意修改** `test_persister_swallows_store_errors`：旧契约测试改为 required/best_effort 双形态（方案 WP3 明令）。

### D3：provisional 批次与 round 归属（WP3）

commit_provisional 改走 append_batch（round 级 batch_id）；COMMITTING 状态机按方案 §4.6（失败不 pop、新发射等待、不持 Store 锁）；嵌套派生事件的 round_id 经执行上下文传播（不只查 task_id）。WP0 夹具 `test_runtime_storage_failure` 翻转：drop-table 后 required 模式 → 会话隔离 + wait_for_finish 抛 PersistenceUnavailableError + 无 committed 通知。

### D4：快照切面（WP4）

SnapshotWriter 改「head 截断 + read_range + apply」三步；`RunSnapshot` 增 `last_commit_position`/`projection_version`（WP2 的 receipt 供值）；恢复路径 rebuild_view 优先 position 游标，`read_after(id)` 降为 legacy 只读。夹具 `test_snapshot_commit_interleaving` 与无桩验证 h2 翻转（两条恢复路径都见 a、b）。迁移实装（`--execute`）与「快照失效重建」在同一 WP 验收（方案 §4.9 步骤 3）。

### D5：operation_id 与账本落点（WP5）

身份五元组按方案 §5.2；`_ingest_assistant_turn` 返回 `PersistedAssistantTurn(record_id, tool_calls)` 是 ActStep 的接口变更（所有执行入口带 record_id+ordinal）；`ProviderContext.operation_id` 新增、invocation_id/extra 兼容保留。账本 SQL 表与 WP2 的迁移工具同仓（`scripts/migrate_event_positions.py` 不动，新表独立建）。silent/dispatch 控制工具不入对话但**必须有账本身份**（方案明令）。

### D6：Reconcile 匹配改判据（WP6）

完成匹配从「task view 内所有 tool_call_id 集合」改为「operation_id 关联的 TOOL_RESULT」——这是对旧 dangling 语义的行为变更：存量无身份的 dangling 默认进 unknown（不随机 ID 自动执行副作用工具）。夹具 `test_tool_outcome_unknown` 与无桩验证 h3 翻转（manual 下副作用保持 1 次、task INTERRUPTED + TOOL_OUTCOME_UNKNOWN）。

### D7：ExecutionLimits 的注入点（WP7）

`core/control/execution_budget.py` 新模块 + RuntimeConfig 字段；StepDriver 每步边界 checkpoint（≤1s 周期，崩溃漏记如实文档）；三个旧字段的 DeprecationWarning 在模板 loader 与 Runtime 校验各一处去重点。**不激活旧默认**（方案 §6 明令）。

### D8：验收与门禁（WP8）

方案 §9 矩阵按 E/O/L/X 前缀落为测试文件；性能基准 `scripts/benchmark_runtime_commit.py`（固定 MockLLM/延迟，机器可读 JSON，15% p95 预算为待验证值）；两个验证脚本的四个判定全部翻转（h1/h2/h3 的 defect_reproduced → false 且新契约断言通过）是本 change 整体的最终门禁。

### D9：WP9 独立否决（H6）

只在 WP3–WP8 稳定后评估：两个示例 ContextPolicy 宿主 + 轨迹等价实验；无收益即整体撤销，不影响前序修复价值（方案 §7.2）。

## Risks / Trade-offs

- [required 默认值对现有宿主是行为变更] → 迁移说明 + 构造期显式失败（不静默）；测试宿主（本仓全部 fixture）随 WP3 同批切默认。
- [WP5 接口变更面大（ActStep/ProviderContext/Registry）] → 分两步：先身份与账本（不动 Reconcile），后策略与处置；每步全量回归。
- [快照切换期的旧格式混存] → projection_version 不匹配忽略快照全量重放（spec 已定），迁移工具统一收口。
- [多 WP 单 change 的 apply 周期长] → tasks 按 WP 分组、每 WP 独立 commit；如需中途交付，已完成的 WP 组勾选状态即进度真相。

## Migration Plan

WP2 迁移工具先行（dry-run → execute）；WP3 切默认前先灰度 best_effort 观察一个测试宿主周期；WP4 切快照格式时旧快照自动失效重建；WP6 的存量 dangling 默认 unknown 是保守默认（宁停勿重跑副作用）。每 WP 可独立 revert。

## Open Questions

（无——方案 §4–§7 已固定决策；本地取舍见 D1–D9。）
