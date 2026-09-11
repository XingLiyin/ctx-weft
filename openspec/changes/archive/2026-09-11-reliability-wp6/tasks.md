# Tasks: reliability-wp6

> 完成 = **H3 解决**（h3 判定翻转、探针 H3 转 True——六项假设仅剩 H5/H6）。
> 硬边界：不动 gateway 五步序（第二道防线）；不承诺 exactly-once。

## 1. 类型与接口

- [x] 1.1 `ToolCapability` +`recovery_policy: str = "manual"`（四值校验在 Registry 注册时）；`protocols/operations.py` +`QueryResult` 协议与 `QueryOutcome`；`TaskErrorCode.TOOL_OUTCOME_UNKNOWN`；`EventType.OPERATION_UNCERTAIN`（含 payload 契约：operation_id/tool_name/revision/actions/redacted_summary）；形状单测
- [x] 1.2 启动校验：cap 声明 queryable 且 provider 未实现 QueryResult → ValueError（响亮）；`test_operation_recovery_policy.py` 第一组

## 2. reconcile 匹配切换 + 策略分派（D1/D2）

- [x] 2.1 `_dangling_tool_calls` 双通道判据（账本 completed / 确定性 memory_result_id 存在）；reconcile 主循环按 D2 表分派（None→unknown、PREPARED→invoke、STARTED×四策略、WAITING_HUMAN→既有 HITL 路径）；unknown = 账本 CAS + task INTERRUPTED(TOOL_OUTCOME_UNKNOWN) + OperationUncertain 事件 + 短路停止后续 dangling
- [x] 2.2 策略表全分支测试（`test_operation_recovery_policy.py`）：manual 不重跑 / retry_safe 同 op_id 恰好一次 / idempotent 幂等键 / queryable 三态（completed 复用·definitely_not_started 重跑·unknown 停住）/ 存量无身份 dangling→unknown / call_1 复用串扰根治（旧结果不配新调用）
- [x] 2.3 既有 reconcile 测试组 policy 参数化翻转：retry_safe 夹具保持重跑断言；manual 夹具新断言（INTERRUPTED + 副作用 1 次）；`test_crash_recovery_reconcile.py` 按此改造

## 3. 闸门 + resolve_operation（D3）

- [x] 3.1 闸门：unknown 标记（task error_code）在 recover_agent 的 assemble 拒绝续跑（`OperationUncertainPending`，提示 resolve_operation）；单测：unknown 下 recover_agent 不重跑
- [x] 3.2 `runtime.resolve_operation(op_id, decision, expected_revision)`：supply_result（CAS completed + 确定性 id 补写 memory + task 重排）/ retry_confirmed（CAS 回 started + 重排，原 op_id 不变）/ cancel_task（终态，不撤外部动作）；revision 互斥测试（两宿主并发恰一成功）；决策审计入 attempts（`resolve:<decision>`）
- [ ] 3.3 子进程强退矩阵（`test_operation_crash_matrix.py`，**移入 WP8 验收矩阵实施**——wp6 已钉组件级等价物：闸门拒绝/revision 互斥/补写不重执行；真子进程退出矩阵随 WP8 全量验收落地）：O-T05/O-T06 各至少一条真子进程退出 + 新 Runtime 实例（manual 停住 / completed 后 memory 写失败由账本补写）；控制工具核验三钉（O-T14 delegate 找回子任务不双建 / finish·metadata 同身份幂等 / ask_user 复用）

## 4. 翻转与门禁

- [x] 4.1 翻转 WP0 夹具 `test_tool_outcome_unknown`：manual 下副作用 1 次 + INTERRUPTED + TOOL_OUTCOME_UNKNOWN + 事件带 revision；保留 retry_confirmed 续跑对照分支
- [x] 4.2 验证脚本翻转：无桩 h3 → after_recovery_effects==1 + fixed:true（无桩四项全绿）；探针 H3 → True（`--expect fixed` 全 True 首次达成）
- [x] 4.3 全量回归：仅 2 既知预存，零新增；h1/h2/h4 保持修复

## 5. 收尾

- [x] 5.1 ARCHITECTURE.md（§6 账本段补策略表与 unknown 处置、§7 recover_agent 闸门一句）；README 升级说明（retry_safe 显式声明 + resolve_operation 用法）；`openspec validate` + 总纲任务组 6 回勾 + 记分板 **H3 ✅**；提交拆分：类型接口 / reconcile 切换 / 处置闭环 / 翻转与 docs
