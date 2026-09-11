# Design: reliability-wp5

## Context

身份现状（已核实）：`ProviderContext.invocation_id`（protocols/context.py:122，单次尝试）；`extra['tool_call_id']`（wire 配对）；`_ingest_assistant_turn`（act.py:516）内部拿到 memory record id 但只返回 tool_call dicts；gateway `invoke` 是所有工具调用（含 silent/dispatch）的唯一咽喉。WP1 已分参数通道（effective 入 provider）；WP3 已有存储不可用隔离链路可复用。

## Goals / Non-Goals

**Goals:** operation_id 三性质成立并贯通（act 铸 id → provider_ctx → gateway 账本）；OperationStore 状态机双实现 + conformance；执行五步串进 gateway；账本不可用走隔离；h3 判定保持复现（只建不切）。

**Non-Goals:** 不切 reconcile 匹配、不消费恢复策略、不做 resolve_operation（WP6）；不承诺 exactly-once；不动 HitlService。

## Decisions

### D1：operation_id 的铸点与派生（P1）

`operation_id = "op_" + sha1(f"{tenant}|{session}|{agent}|{record_id}|{ordinal}")[:24]`——确定性哈希而非 ULID：同一逻辑调用跨进程/跨重启必然同值（ULID 做不到）；可读前缀 + 截断哈希平衡可读与碰撞面（同会话内 record_id 已是 ULID，碰撞概率可忽略）。铸点在 **act/reconcile 的调用侧**（不是 gateway）：gateway 只消费 provider_ctx.operation_id——咽喉点保持无状态，调用侧才知道 record_id+ordinal。

- `PersistedAssistantTurn(record_id, tool_calls)`：`_ingest_assistant_turn` 返回值升级。旧消费点（act 的 message 重建、_compose_final_outputs）逐一改读 `.tool_calls`；reconcile 从 task view 读 record_id（LLM_RESPONSE 的 memory record id 经 load_view 可得——dangling 对账本来就遍历它）。

### D2：账本五步串进 gateway 的位置（P2）

在 `invoke` 的参数管线（effective_args 就绪）之后、`_record_invocation` 之前：

```
1. provider_ctx.operation_id 缺失 → 跳过账本（兼容裸调 gateway 的单测/宿主），记 debug
2. get(op_id)：
   - 已 completed → 直接走结果回填路径（幂等重入；本轮真正消费在 WP6，现在只保证不重复执行外部调用——completed 即短路返回账本结果）
   - 无 → prepare（参数指纹 = invocation_key 已有实现复用）
3. CAS started（revision=1 → 2）
4. provider 执行（既有路径不动）
5. completed（完整结果或 blob ref + error）→ TOOL_RESULT（memory result id = "res_" + op_id 后缀，确定性）→ CapabilityFinished
```

silent/dispatch 控制工具走同一串（它们经同一 invoke）。HITL park 发生在 provider 步内 → 账本置 `waiting_human`（park 返回后重新 CAS started 继续）。

- **completed 短路**是 WP5 唯一的行为变化：同 operation_id 的重入不再打 provider——这正是 O-T08（task retry/恢复多次重入不多做一次）的前半。但它不改变 h3 判定：无桩 h3 的重跑经 reconcile 用**新 operation_id**（新 assistant record）——盲重跑路径不变。诚实门禁成立。

### D3：账本不可用 = 存储不可用（P2 收尾）

OperationStore 写失败抛出 → gateway 包成 `PersistenceUnavailableError` 上抛——复用 WP3 全套隔离链路（健康表/停止调度/wait_for_finish），零新机制。内存默认实现不会失败（进程内 dict）；SQL 实现失败即真存储故障，语义正确。

### D4：完整结果入账本的形态（P3）

`OperationRecord.result`：`str | list[ContentPart] | BlobRef`——文本直存；超阈值（复用 gateway spill_threshold 语义）或含 parts 时引用 blob（`MemoryBlobStore` 已有），账本只存 ref 不复制字节。审计事件仍存截断文本（两条通道不同目的，不合并——与 WP1 D2 同纪律）。

### D5：OperationStore 协议面

```python
class OperationStore(Protocol):
    async def get(self, operation_id, ctx) -> OperationRecord | None
    async def prepare(self, record: OperationRecord, ctx) -> OperationRecord      # 幂等（同 id 同内容 no-op）
    async def compare_and_set(self, operation_id, expected_revision, update: OperationUpdate, ctx) -> OperationRecord
```

SQL 表 `operations`（operation_id PK、revision、status、身份五元组列、args_hash、policy、attempts_json、result/ref、error、memory_result_id、created/updated）；`open_sqlite_operation_store` 沿 WP2/WP4 的 open 上下文习惯。runtime：`ProviderRegistry.register_operation_store`（缺省注册内存版）。

### D6：兼容缝

- gateway 裸调（单测/宿主直构）无 operation_id → 账本全程跳过，行为与今天逐字节一致——既有 gateway 测试零改动即全绿是回归门禁。
- act 侧铸 id 失败（record_id 缺失，理论不可达）→ 跳过账本 + warning，不阻断执行（账本是恢复优化不是执行前提）。

## Risks / Trade-offs

- [+2 次持久确认/调用（SQL 下）] → 微基准随验收记录；内存默认零成本；宿主按需注 SQL。
- [act 接口面改动波及调用点] → PersistedAssistantTurn 提供 `.tool_calls` 属性直通旧形态，消费点机械升级；全量回归兜底。
- [completed 短路与 WP6 的恢复策略耦合] → 本包短路只看「completed 即复用」；策略分支（unknown/manual 停住）WP6 在 reconcile 侧加，两层正交。
- [同 op_id 并发推进] → CAS revision 拒绝后到者；conformance 专测。

## Migration Plan

无数据迁移（新表新协议）；回滚 = revert（无 operation_id 时账本全程旁路）。生产宿主接 SQL 账本与 WP6 一同灰度。

## Open Questions

（无——P1/P2/P3 已定；方案 §5.2/§5.3 固定其余。）
