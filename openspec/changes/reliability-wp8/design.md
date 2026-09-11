# Design: reliability-wp8

## Context

H1–H5 已修复且各有测试锚，但方案的验收矩阵从未逐条对照；wp6 的强退矩阵和 wp7 的 budget persisted 接线诚实移入本单元。本 change 无新行为——设计文档记录收口决策。

## Goals / Non-Goals

**Goals:** 子进程强退矩阵 pytest 化；budget persisted 接线；性能基准脚本与原始数据；验收矩阵映射清单 + 补齐无锚项；核验表逐项填写；总纲终勾。

**Non-Goals:** 不修两个既知预存失败；不做 PostgreSQL 真库（无实例——「未验证」）；不实现 WP9；不改生产行为。

## Decisions

### D1：子进程强退 = 无桩 h3 的 pytest 移植

无桩脚本的 worker/recover 子进程模式（`subprocess.Popen` → poll marker → `proc.kill()` → 新进程 `recover_agent`）已验证 O-T05 形态。移植为 pytest 夹具：
- **O-T05**（manual started → 真退出 → 副作用 1 次）：直接移植，`recovery_policy="manual"` 断言翻转后语义。
- **O-T06**（completed 后 memory 写前 → 账本补写）：worker 在 provider 返回后、memory ingest 前注入崩溃（monkeypatch memory.ingest 抛错→ os._exit(1)）；新实例从账本 `get(op_id).status == COMPLETED` 补写 TOOL_RESULT（`operation_memory_result_id`），不重执行。
- 子进程用 `sys.executable` + `-u`（同无桩脚本）；`tmp_path` 隔离 SQLite。

### D2：budget persisted = 投影透传 + TM restore 一行

- **投影**：`reducers.py` 的 TASK_CREATED/TASK_STARTED payload 加 `budget_consumed` 字段（dict，从 task 内存字段读取）；`converters.py` 的 `task_from_projection` 反向透传。
- **接线**：`TaskManager._run_task`（或 `_build_loop_ctx` 调用方）在 task 已有 `budget_consumed` 时 `ExecutionBudget.restore(limits, consumed)` 而非新建空实例。**snapshot 写回**在 `_run_loop` 的 finally（每次 RUN_FINISHED 前）把 budget.snapshot() 写 `task.budget_consumed`——随下一个 TASK_* 事件投影落库。
- 跨进程恢复：`task_from_projection` 读到 `budget_consumed` → TM restore → 续用剩余预算。算术已由 wp7 的 8 例单测背书。

### D3：性能基准的形态

```python
# scripts/benchmark_runtime_commit.py --output benchmarks/2026-09-11.json
# 五场景 × MockLLM 固定延迟 × 5 预热 + 30 测量
# 输出：{"scenarios": [{"name": ..., "p50_ms": ..., "p95_ms": ..., "raw": [...]}]}
```

- 场景 1-3（单会话 100 工具 / 4 agent 各 50 / 10 会话）：测 wall time p50/p95。
- 场景 4（恢复 1k/10k/100k 事件含快照开/关）：构造事件库 → `rebuild_view` 计时（快照开 = 先写快照）。
- 场景 5（阻塞观察者 + 正常观察者并存）：验证主循环不挂死 + 丢弃计数。
- **无对照基线**（改前代码不在手上）——首轮数据即**绝对基线**，后续变更对照。15% 预算仅作为后续对照的参考线。

### D4：验收矩阵映射 = 文档产物（不改测试名）

产出 `docs/plans/acceptance-matrix.md`：方案 §9 四个表逐条标注（已有测试的 `file::test_name` / 「未落地——原因」/ 本 change 补齐的新锚）。这是**审计对照表**，不是新测试集。

### D5：两个既知预存失败的处理

`test_multiround_retry_accumulates_then_l3_collapses_e2e`（compact L3 坍缩）与 `test_default_role_prompt_uses_two_fields`（ROLE.md 外部路径）——**不修**、不归可靠性范畴。核验表如实记录。skip/xfail 审阅：确认无 xfail（全量仅这 2 failed + 1 skipped 均有说明）。

## Risks / Trade-offs

- [子进程夹具在 CI 的稳定性] → `tmp_path` + stdlib-only + 无端口；超时兜底 120s。
- [budget 投影加了新字段，旧投影无此字段] → `budget_consumed` 默认 None → restore 走空 dict → 行为同 wp7（无 persisted）。
- [基准首轮无对照] → 绝对基线策略写进脚本 docstring；后续变更以 `--baseline` 参数对照。

## Migration Plan

无数据迁移；回滚 = revert（接线修复与测试独立）。

## Open Questions

（无——全部是收口，无新设计分歧。）
