# Tasks: reliability-wp5

> 诚实门禁：无桩 h3 判定**保持复现**（1→2）——本 change 只建账本，恢复行为不变。
> 硬边界：不切 reconcile 匹配、不消费恢复策略、不做 resolve_operation（WP6）。

## 1. 协议与类型

- [x] 1.1 新建 `protocols/operations.py`：`OperationRecord`（身份五元组/参数指纹/策略字段/attempts/完整结果或 blob 引用/error/memory_result_id/revision/status）、`OperationUpdate`、`OperationStore` 协议（get/prepare/compare_and_set）、状态枚举（prepared/started/completed/waiting_human/unknown）；形状单测
- [x] 1.2 `ProviderContext` 加 `operation_id: str | None = None`（invocation_id/extra 兼容保留）；`operation_id_for(tenant, session, agent, record_id, ordinal)` 确定性派生 helper（sha1 截断 + 前缀）

## 2. 双实现 + conformance

- [x] 2.1 `providers/operations/in_memory.py`（dict + revision CAS）；`providers/operations/sql.py`（operations 表 + `open_sqlite_operation_store` open 上下文，沿 WP2/WP4 习惯）；`ProviderRegistry.register_operation_store` + runtime 缺省注册内存版、注入口
- [x] 2.2 新建 `tests/unit/test_operation_store_conformance.py`（参数化内存/SQL）：prepare 幂等（同 id 同内容 no-op、异内容拒）、CAS 串行（revision 拒后到者、状态机不倒退不跳跃）、get/全字段往返（含 ContentParts 与 blob ref 形态）

## 3. 身份贯通（act → gateway）

- [x] 3.1 `_ingest_assistant_turn` 返回 `PersistedAssistantTurn(record_id, tool_calls)`（`.tool_calls` 直通旧形态）；act 的调用点（message 重建/_compose_final_outputs）与 reconcile 逐一升级；新建 `tests/unit/test_operation_identity.py` 三性质：跨重启同 id（重入派生一致）/ 同参两次合法调用不同 id（O-T09）/ call_1 复用不串扰
- [x] 3.2 调用侧铸 id：act 执行每个 tool_call 前 `provider_ctx.operation_id = operation_id_for(...)`；reconcile 对 dangling 同样携带（record_id 来自 task view 的 LLM_RESPONSE 记录）

## 4. gateway 账本五步（D2）

- [x] 4.1 gateway 串接：get（completed → 短路复用账本结果，不再打 provider）/ prepare（参数指纹复用 invocation_key）/ CAS started / provider（既有路径）→ completed（完整结果或 blob ref；HITL park → waiting_human）→ TOOL_RESULT（memory result id = op_id 确定性派生）→ CapabilityFinished；**无 operation_id 的裸调全程旁路**（既有 gateway 测试零改动全绿 = 回归门禁）
- [x] 4.2 账本写失败 → 包 `PersistenceUnavailableError` 上抛（复用 WP3 隔离链路）；单测：SQL 账本故障 → 会话隔离
- [x] 4.3 新建 `tests/unit/test_gateway_operation_ledger.py`：五步顺序断言（事件序）/ completed 重入短路（O-T08 前半）/ completed 后 memory 写失败由恢复路径从账本补写（O-T06 组件级：夹具直接调 recovery helper）/ silent+dispatch 工具入账

## 5. 门禁与收尾

- [x] 5.1 全量回归 `uv run pytest tests/unit tests/integration -q -W ignore`：仅 2 既知预存失败，零新增；**无桩 h3 保持复现**（defect_reproduced=true，1→2）、h1/h2/h4 保持修复；探针 H3 保持 False
- [x] 5.2 微基准：内存账本下 gateway invoke 开销对比（记录数据）；ARCHITECTURE.md §6 补账本段（五步顺序 + operation_id 三性质 + WP6 边界声明）；`openspec validate` + 总纲任务组 5 勾选（记分板注明：账本就绪、H3 行为未切）；提交拆分：协议+实现 / 接线 / 测试+docs
