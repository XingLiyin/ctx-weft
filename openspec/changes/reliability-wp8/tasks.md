# Tasks: reliability-wp8

> 收口单元：不引入新行为。全量基线 3082 过 / 2 既知预存 / 1 skip。

## 1. budget persisted 接线（wp7 遗留）

- [x] 1.1 reducers + converters 加 `budget_consumed` 投影透传（TASK_CREATED/TASK_STARTED payload）；`_run_loop` finally 写回 budget.snapshot() 到 task 内存字段；TM 派发时 `ExecutionBudget.restore()`（有 persisted 时）而非空实例
- [x] 1.2 新建 `tests/unit/test_budget_persisted.py`：跨 retry 累计（retry 后 budget 含前次 consumed）+ 重启恢复（投影含 budget_consumed → 新 TM restore → 续用剩余）+ 旧投影无字段 → 空 dict 回落

## 2. 子进程强退矩阵（wp6 移入项）

- [x] 2.1 新建 `tests/integration/test_operation_crash_matrix.py` O-T05：manual + started → worker 子进程真退出（硬杀）→ 新 Runtime 实例 recover → 副作用 1 次 + INTERRUPTED + TOOL_OUTCOME_UNKNOWN（无桩 h3 形态 pytest 化）
- [x] 2.2 O-T06：completed 后 memory 写前崩溃（monkeypatch memory.ingest 抛错 + os._exit）→ 新实例从账本补写 TOOL_RESULT（确定性 id）、provider 不重执行
- [x] 2.3 O-T14 控制工具核验钉：delegate 的 op completed 后确认丢失 → 重入找回原 child（不生成第二棵子树）

## 3. 性能基准

- [x] 3.1 新建 `scripts/benchmark_runtime_commit.py`：五场景（单会话 100 工具 5ms / 4 agent 并发 / 10 会话 / 恢复 1k-100k 含快照开关 / 阻塞观察者并存）、MockLLM 固定延迟、5 预热 + 30 测量、机器可读 JSON + raw 数据留存
- [x] 3.2 跑一轮记录绝对基线数据（首轮无对照——写进 docstring 说明）

## 4. 验收矩阵映射与补齐

- [x] 4.1 新建 `docs/plans/acceptance-matrix.md`：方案 §9 E-T×14 / O-T×16 / L-T×6 / X-T×2 逐条标注已有测试锚（file::test_name）或「未落地/移入 WP9」
- [x] 4.2 补齐无锚项（预计 3-4 条）：E-T02（批中第 k 条失败回滚——已有 conformance 等价物确认即可）/ E-T03（同 batch 重试）/ E-T07（回调嵌套不死锁）/ O-T14 若 2.3 未覆盖则补

## 5. 终核与收口

- [x] 5.1 验证脚本终核：探针 `--expect fixed` 四 True；无桩 h1-h4 全 fixed:true；2 个既知预存失败如实记录（非 PG）
- [x] 5.2 核验表（方案 §10.3）逐项填写实测（产出 `docs/plans/verification-report.md`：H1-H5 各行 + 性能基线行；空项写「未验证」——PostgreSQL 行）
- [x] 5.3 全量回归零新增；总纲任务组 8 回勾 + 记分板终态（H1-H5 ✅ / H6 ⬜ wp9 可选）；`openspec validate`；提交拆分：接线 / 强退矩阵 / 基准 / 矩阵映射+docs
