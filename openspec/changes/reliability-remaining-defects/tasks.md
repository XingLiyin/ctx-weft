# Tasks: reliability-remaining-defects

> 硬前置：reliability-wp2（OrderedEventStore）先落地。每个 WP 独立 commit、可独立回滚；
> 夹具/验证脚本翻转是该 WP 的验收门禁。方案验收矩阵条目号（E-T/O-T/L-T/X-T）在括注。

## WP3 提交门与通知分离（H1 + 附①）

- [x] 3.1 新建 `tests/unit/test_commit_gate.py`：required 提交确认（store 失败 → 调用方收错、无 committed 通知）/ best_effort 显式告警 / 自定义 bus 不支持 → 构造失败；先基线红（现状吞异常）
- [x] 3.2 新建 `core/events/commit_gate.py` 并替换 runtime 构造期 persister 接线；ALM/SessionRegistry 订阅改经 gate 注册为 required consumer；观察者独立有界队列 + `EventsDropped` 元事件（带 position 区间）；有意改写 `test_persister_swallows_store_errors` 为双契约形态（方案 WP3 明令）
- [x] 3.3 provisional 批次：commit_provisional 走 append_batch（round batch_id）、COMMITTING 竞态、round_id 经执行上下文传播（E-T08/E-T09）；新建 `tests/unit/test_observer_backpressure.py`（慢观察者不挂死、丢弃可观测，E-T10/E-T11）
- [x] 3.4 `PersistenceUnavailableError` 链路：TM/后台 recap 先于通用重试、会话隔离可查询、wait_for_finish 显式抛错、batch_id 确认未知提交（E-T01/E-T12/E-T14）；新建 `tests/integration/test_runtime_storage_failure.py` 的 required 形态（翻转 WP0 夹具：drop-table → 隔离而非伪装成功）

## WP4 快照一致切面与迁移（H2）

- [x] 4.1 新建 `tests/unit/test_snapshot_consistent_cut.py`：head 截断算法（延迟提交不丢 E-T04、并发提交不混入 E-T05、版本不匹配降级 E-T06）与两路恢复等价（session/task/agent/HITL/outputs 逐项）
- [x] 4.2 SnapshotWriter 改三步切面 + `RunSnapshot.last_commit_position/projection_version`；rebuild_view 优先 position 游标、`read_after` 降 legacy；翻转夹具 `test_snapshot_commit_interleaving` 与无桩验证 h2（两条路径都见 a、b）
- [x] 4.3 迁移实装与验收：`scripts/migrate_event_positions.py --execute` + 旧快照失效重建 + 新建 `tests/integration/test_event_position_migration.py`（E-T13）；WP2–WP4 联合验收后才允许生产宿主切日志格式（方案 §8-WP4）

## WP5 稳定操作身份与账本（H3 前半）

- [x] 5.1 新建 `tests/unit/test_operation_identity.py`：五元组确定性（跨重启不变）、同参两次合法调用不去重（O-T09）、call_1 复用不串扰；先基线红（现状无 operation_id）
- [x] 5.2 `protocols/operations.py` + 内存/SQL OperationStore（prepare/CAS/get，prepared→started→completed/waiting_human/unknown）；`tests/unit/test_operation_store_conformance.py` 参数化
- [x] 5.3 接线：`_ingest_assistant_turn` 返回 PersistedAssistantTurn、执行入口带 record_id+ordinal、`ProviderContext.operation_id`、silent/dispatch 工具账本身份；执行顺序按 spec（prepared→CAS started→Provider→completed→幂等 memory→事件，O-T03/O-T06/O-T07）；大结果/多模态入账本（O-T13）

## WP6 恢复策略与 unknown 处置（H3 后半）

- [x] 6.1 `ToolCapability.recovery_policy` 四态 + Registry/启动校验（queryable 未实现即失败）；`tests/unit/test_operation_recovery_policy.py` 覆盖策略表全分支（O-T04/O-T05/O-T08/O-T15/O-T16）
- [x] 6.2 Reconcile 完成匹配改 operation_id；存量无身份 dangling 默认 unknown；有意更新「所有 dangling 都重跑」的既有测试（仅 retry_safe 夹具保留重跑断言）；控制工具幂等核验（delegate 找回子任务 O-T14、finish/ask_user 复用）
- [x] 6.3 `TOOL_OUTCOME_UNKNOWN` + `resolve_operation`（supply_result/retry_confirmed/cancel_task + revision 乐观锁 O-T12；unknown 下 recover_agent 不可绕过 O-T11）；HITL 热/冷竞态单执行者（O-T10）；新建 `tests/integration/test_tool_outcome_unknown.py` required 形态（翻转 WP0 夹具：manual 副作用保持 1 次）
- [x] 6.4 子进程强退矩阵：`tests/integration/test_operation_crash_matrix.py`（O-T05/O-T06 至少各一条真子进程退出 + 新 Runtime 实例）；无桩验证 h3 翻转（after_recovery_effects == 1）

## WP7 ExecutionLimits（H5）

- [x] 7.1 `core/control/execution_budget.py` + RuntimeConfig 注入 + 可注入 monotonic clock；`tests/unit/test_execution_budget.py` 验证计量算术（active time 排除等待 L-T02、retry 不重置 L-T03、重启恢复 L-T04、checkpoint 漏记声明）
- [x] 7.2 barrier 驱动集成 `tests/integration/test_execution_limits.py`（L-T01/L-T05：合作型超时 + 不合作 Provider 标记未终止）；错误码四态、超时取消先走 unknown 规则
- [x] 7.3 三个旧字段（max_turns_per_agent / timeout_ms / timeout_per_step_sec）去重 DeprecationWarning + README 迁移说明，验证「警告但不激活」（L-T06）

## WP8 整体验收与性能门禁

- [x] 8.1 方案 §9 剩余矩阵条目补齐（E-T02/E-T03/E-T07 回调嵌套与批次重试等）；全量 `uv run pytest tests -q` + skip/xfail 审阅 + PostgreSQL 未运行时如实报告
- [x] 8.2 `scripts/benchmark_runtime_commit.py`（固定负载五场景、机器可读 JSON、原始数据留存；15% p95 为待验证预算）；四个验证脚本判定终核：h1/h2/h3 defect_reproduced 全 false、h4 fixed 保持 true
- [x] 8.3 方案 §10.3 核验表逐项填写实测（空项写「未验证」）；ARCHITECTURE.md 对应章节更新（提交门/快照/账本/限制）

## WP9 Runtime 职责拆分（H6，可选，可整体否决）

- [ ] 9.1 仅在 WP3–WP8 稳定后：Recovery/Interaction/ContextPolicy 三边界逐个拆（一次一验），新建 `tests/integration/test_context_policy_extension.py`（两个示例策略宿主，无 core 修改/无私有导入 X-T01）与 `test_runtime_refactor_trace_equivalence.py`（默认轨迹等价 X-T02）；无收益证据则记录否决结论并跳过实施
