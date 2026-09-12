# Tasks: reliability-wp3

> 硬边界：不动快照/恢复读取（WP4）；h2 与无桩 h2 判定保持复现。每任务组独立 commit 可回滚。

## 1. 协议与总线基建（spec: event-commit 的接线面）

- [x] 1.1 `protocols/events.py`：`EventType.EVENTS_DROPPED`（入 TRANSIENT 集合）、`PersistenceUnavailableError`、`EventBus.attach_commit_gate` 可选扩展（默认不支持）、`subscribe(..., required=False)` 参数；形状单测通过
- [x] 1.2 `InProcessEventBus`：gate hook（emit/commit_provisional 的 fanout 前调用）、required 订阅类别（异常穿出 emit，穿出前不产生 committed 可见性）、观察者丢弃合成 EventsDropped（payload：subscriber_id + first/last_position + dropped_count，position 取自 gate 返回的 StoredEvent；无 gate 时回落 dropped 计数旧行为）——新建 `tests/unit/test_observer_backpressure.py`：慢观察者不阻塞（E-T10）、丢弃元事件可达且可按 read_range 补读、required 异常上抛进入健康故障（E-T11）
- [x] 1.3 `commit_provisional` 整批化：buffer → gate.append_batch(round_batch_id) → 成功才 re-fanout，失败缓冲保留；round_batch_id 由 TM begin_round 生成并透传 bus（begin_provisional 签名扩展）；同 batch_id 重试恰好一次提交（复用 WP2 幂等）

## 2. CommitGate 与 runtime 接线

- [x] 2.1 新建 `core/events/commit_gate.py`：`CommitGate(store, policy)`，`commit(events) -> list[StoredEvent]`（瞬态跳过、异常包成 PersistenceUnavailableError 并先标记会话健康）；新建 `tests/unit/test_commit_gate.py`：required 确认后通知（E-T01 的组件级）/ 失败抛错不 fanout / best_effort 旧吞错语义 + 启动告警 / 自定义 bus 不支持 → 构造失败带适配说明
- [x] 2.2 runtime 构造期接线：`event_commit_policy` 配置（RuntimeConfig 字段，默认 required）；required 且 bus 支持 → gate 替换 persister 接线（保留 `attach_persistence` 为 best_effort/兼容路径，`test_event_persistence_wiring.py` 在 best_effort 模式全绿）；ALM/SessionRegistry 订阅升级 `required=True`（构造期校验：required 必须配 provisional）
- [x] 2.3 派生事件归属与重入：contextvar round_key 传播（窗口路径设置、drain 期间生效、结束复原）；嵌套派生 conformance 三断言：父先于子（position 序）、无 task_id 派生事件随窗口归属（E-T08）、提交期间重入不死锁不持锁（E-T07）；`test_agent_lifecycle_manager_events` 等既有 ALM 测试全量回归
- [x] 2.4 discard 重聚合（D6）：discard_provisional 后对窗口内出现过的 agent 重发当前聚合事实；含「无幽灵 agent 状态」断言（E-T09 的 live 等价面）

## 3. 存储不可用隔离（spec 第 4 条 requirement）

- [x] 3.1 会话健康表 + 公开查询/等待接口（storage_unavailable 带原因）；`_run_loop` / TaskManager `_run_task`/`apply_run_outcome` / 后台 recap 三处前置 PersistenceUnavailableError 分支（不重试、不发终态事件）；`wait_for_finish` 健康检查抛错；其他会话不受影响（E-T14）
- [x] 3.2 恢复确认：隔离恢复路径先按 batch_id 幂等查询收口未知提交（复用 WP2），不盲目重发新 ID；崩溃于「提交后、内存状态更新前」的冷恢复收敛（E-T12）

## 4. 旧契约翻转与门禁（D7 清单逐项成对）

- [x] 4.1 改写 `test_persister_swallows_store_errors` 为双契约（required 抛 / best_effort 吞+告警）；`test_event_persistence_wiring.py` 余项标注 best_effort 模式
- [x] 4.2 翻转 WP0 夹具 `test_runtime_storage_failure`：required 形态（drop-table → 隔离 + wait_for_finish 抛 PersistenceUnavailableError + 无 committed 通知 + stored 0）；保留 best_effort 形态钉旧契约
- [x] 4.3 验证脚本判定翻转：`verify_agent_architecture.py --expect fixed` 的 H1 转绿（emit_rejected=True、observer_count=0）；`verify_no_stubs_e2e.py h1` 的 defect_reproduced 转 false 并按新契约断言；**h2/h3 判定必须保持复现**（WP4/WP6 未做）
- [x] 4.4 全量回归 `uv run pytest tests/unit tests/integration -q -W ignore`：仅既知 2 个预存失败，零新增；性能抽查：单会话 100 次工具调用的 wall time 对比（记录数据，超 15% 预算则先批量化散事件再复测）

## 5. 文档与收尾

- [x] 5.1 ARCHITECTURE.md：§11 补「提交门与通知分离（required/best_effort、EventsDropped、storage_unavailable）」段，§1 数据流图 gate 位置；README 事件节一句 + 升级说明（自定义 bus 适配）
- [x] 5.2 `openspec validate "reliability-wp3"` 通过；总纲 reliability-remaining-defects 任务组 3 勾选；提交拆分：基建/接线/隔离/翻转各自独立 commit
