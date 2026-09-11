# Design: reliability-wp3

## Context

现状接线与三处吞错点（bus.py:112-113 handler 异常、persister.py:47-50 存储异常、队列丢弃仅计数）已在探索中逐行核实。关键结构事实：

- persister 是 fanout 尾部的普通订阅者——任何「订阅者形态」的提交确认都注定在伪装成功之后，**gate 必须在 emit 路径内、fanout 之前**。
- ALM（agent_manager.py:170）与 SessionRegistry（:112）已是 `provisional=True` 订阅者；ALM 在 handler 内同步再 emit 派生 Agent* 事件（6 处 `emit_event`）——重入与窗口逃逸都真实存在。
- core 内 49 个 emit 调用点 = PersistenceUnavailableError 的传播面；`_run_loop`/TM 的通用异常链必须给它让位。
- WP2 已交付：单事件 append（batch_id=event.id）、append_batch 幂等、position——本 change 的存储代码量为零。

## Goals / Non-Goals

**Goals:** 提交先于通知（required 默认）；required 消费者异常上抛；观察者背压可观测（EventsDropped + position 补读）；provisional 整批原子 + 派生事件不逃逸；存储不可用显式隔离；三处旧契约按方案明令翻转/改写。

**Non-Goals:** 不动快照与恢复读取（WP4；h2 保持复现）；不承诺 EventStore 与 MemoryProvider 同事务（方案 §4.10）；不做全量 live 状态对账；PostgreSQL 真库验证（WP8）；不改瞬态事件口径。

## Decisions

### D1：Gate 挂接点 = bus 内嵌 hook（P1）

`EventBus` 协议加可选 `attach_commit_gate(gate) -> None`（基类默认 raise/无 = 不支持）；`InProcessEventBus` 实现并在 `emit` / `commit_provisional` 的 fanout **之前**调用。`CommitGate`（core/events/commit_gate.py，编排层、不反向依赖 Runtime）持有 OrderedEventStore + policy，对外只有 `commit(events) -> list[StoredEvent]`。runtime 构造期：required 且 store 为 OrderedEventStore 且 bus 支持 hook → 接线；自定义 bus 不支持 → 构造失败带适配说明；best_effort → 退回旧 persister 路径并告警。

- 备选（弃）：gate 做成头号订阅者——在 fanout 之后，通知先于确认，恰是要修的病；包装 bus（装饰器）——49 个 emit 点全部换持有者，扰动面大且协议错位（gate 是提交语义，不是转发语义）。

### D2：两相 fanout——required 同步、观察者队列（P2）

`subscribe` 加 `required: bool = False`。fanout 改为：required 订阅者（必然 provisional=True，构造期校验）同步 drain、异常**穿出** emit（先标会话健康故障再抛）；观察者维持 put_nowait + 丢弃。丢弃时合成 `EventsDropped`（EventType 新增，TRANSIENT 成员，不落库）注入该订阅者队列，payload = `{subscriber_id, first_position, last_position, dropped_count}`——补读游标用 WP2 position，不用 event id。

### D3：provisional 整批与 round 归属（P3a）

`commit_provisional(task_id)` 改为：buffer → gate 一次性 `append_batch(session, batch_id=round_batch_id, buffered)` → 成功才逐条 re-fanout（顺序保持）；失败 buffer 留存、会话隔离、可重试（batch_id 不变，WP2 幂等保证不双写）。round_batch_id 由 TM 在 begin_round 时生成并透传（与 task_id 一一对应但独立命名域，重试重进同 round 复用）。派生事件归属：`contextvars.ContextVar["round_key"]` 在 emit 进入窗口路径时设置、fanout drain 期间生效——ALM 的同步再 emit 读到同一 round_key，无 task_id 的派生事件也进 buffer；drain 结束复原。

### D4：短临界区与有序可见性（P3b）

gate.commit 只做 append_batch（WP2 内部自带串行化），**不持任何锁等待回调**；ALM 派生事件在父事件 fanout 期间再入 emit → 走自己的（单事件）提交 → 位置必然在父之后 → 观察者按 position 序消费即天然「父先于子」。EventsDropped 亦不参与排序（transient，无 position 语义冲突——payload 引用区间而非自身占位）。

### D5：隔离的传播面——窄切口而非 49 处

gate 失败时先把会话标记 `storage_unavailable`（runtime 级健康表 + 公开查询），再抛 `PersistenceUnavailableError`。异常处理只在三处前置分支：`_run_loop` 的 except 链（先于 generic → 不进 RUN_INTERRUPTED 重试，直接停）、TaskManager `apply_run_outcome`/`_run_task` 的异常分支（不重排、不发终态事件）、后台 recap 任务的异常包裹。`wait_for_finish` 在等待前检查健康标志。49 个 emit 点零改动——异常自然穿出。

### D6：discard 重聚合的最小实现

`discard_provisional` 后，比对窗口内出现过的 agent_id 集合（buffer 里可取），对每个受影响 agent 让 SessionRegistry 重发一条当前聚合事实事件（不回滚、不删事件——只是新增一条校正事实）。不做事件级撤销（那是把 RoundDiscarded 语义改成事件回滚，方案未要求）。

### D7：旧契约翻转清单（集中一处，逐项成对）

| 旧测试/夹具 | 旧断言 | 新断言 |
|---|---|---|
| `test_persister_swallows_store_errors` | 吞异常通过 | 改写双契约：required → 抛；best_effort → 吞 + 告警 |
| `test_event_persistence_wiring.py` 其余 | persister 接线 | attach_persistence 保留为 best_effort/兼容路径，原断言在该模式继续成立 |
| WP0 `test_runtime_storage_failure` | FINISHED + 死后通知 + 0 落库 | required：隔离 + wait_for_finish 抛 + 无 committed 通知 |
| 探针 H1 / 无桩 h1 | 复现 | 判定翻转（fixed 语义）——探针脚本 `--expect fixed` 的 H1 转绿路径打通 |

### D8：SnapshotWriter 的位置

本 change 不动 SnapshotWriter（仍订阅非 provisional、persister 之后）；它消费的是已确认提交的事件流，gate 之后语义不变。快照边界修正（committed_head 截断）是 WP4——届时 writer 改走 read_range。

## Risks / Trade-offs

- [emit 同步确认的延迟（每事件一次 SQL 往返）] → round 批次整批一次（净减）；无 round 的散事件（恢复路径、后台）占比低；≤15% p95 预算在验收时实测，超预算先批量化散事件再谈其他。
- [required 异常穿出改变了 bus 的容错性格] → 仅 required 类别（构造期显式声明，默认 False）；观察者行为逐字节不变（除丢弃可观测）。
- [ALM 同步再 emit 的重入在新路径下行为漂移] → conformance 加嵌套派生用例（父先于子、round 归属、无死锁）三断言钉住；既有 test_agent_lifecycle_manager_* 全量回归。
- [best_effort 退回旧路径的双实现漂移] → best_effort 复用同一个 persister 类不加分支，仅接线位置不同；双模式各留一组 wiring 测试。
- [健康表本身是内存态、崩溃即失] → 恢复路径从持久日志重建（未确认 batch_id 经 WP2 幂等查询收口），健康表只是 live 加速器。

## Migration Plan

默认 required 即行为变更——宿主升级说明：自定义 bus 需实现 attach_commit_gate（或显式 best_effort + 接受告警）；SQL/内存内置 bus 自动获得。无数据迁移（WP2 已铺）。回滚 = revert 单 commit 序列。

## Open Questions

（无——P1/P2/P3 三点在探索中已定取舍；方案 §4.5-§4.7 的其余细节照抄。）
