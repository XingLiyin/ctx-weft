# Design: reliability-wp6

## Context

WP5 已交付：账本（prepared→started→completed 状态机 + CAS）、operation_id 确定性派生（reconcile 对 dangling 已携带原 record_id+ordinal）、gateway 五步序（completed 短路、park→waiting_human、裸调旁路）、`operation_memory_result_id` 确定性 memory id。现状盲点：reconcile.py:77-81 完成判定仍是 tool_call_id 集合；ToolCapability（protocols/capability.py:68-73）无 policy 字段；TaskErrorCode（discriminators.py:37）无 TOOL_OUTCOME_UNKNOWN；无 OperationUncertain 事件与 resolve_operation。

## Goals / Non-Goals

**Goals:** 策略表全分支落地（默认 manual）；匹配切换到 operation_id；unknown 处置闭环（事件 + 闸门 + resolve_operation）；控制工具核验钉子；翻转面成对；**H3 ✅（无桩 h3 翻转、探针 H3 转 True）**。

**Non-Goals:** 不承诺 exactly-once；不动 gateway 五步序（第二道防线保留）；不做分布式锁；PostgreSQL（WP8）。

## Decisions

### D1：匹配切换的双通道判据（P1）

`_dangling_tool_calls` 改为对每个 tc 先派生 op_id（record_id+ordinal——WP5 已带）：

```
done(op_id) = 账本 get(op_id).status == COMPLETED
           或 task view 存在 id == operation_memory_result_id(op_id) 的 tool 记录（memory 通道兜底：
             账本被清/未接但 memory 写成功过的场合——两通道任一即视为已完结）
```

dangling = 未 done 的 tc → 进入策略分派。串扰根治：判据是逻辑身份，与 wire id 无关。

### D2：策略分派放 reconcile，gateway 短路保留（P1 收尾）

reconcile 对每个 dangling：账本 get →

| 状态 | 动作 |
|---|---|
| None（存量无身份） | unknown（保守停住——方案明令） |
| PREPARED | 重新走 gateway.invoke（gateway 会 CAS started→执行；授权链自然重查） |
| STARTED + retry_safe | gateway.invoke（同 op_id 重试——账本重入容忍只追加 attempt） |
| STARTED + idempotent | 同上（幂等键=op_id 由 Provider 侧实现兑现承诺） |
| STARTED + queryable | `provider.query_result(op_id, ctx)`：completed→结果入账本+补 memory（不执行）；definitely_not_started→invoke；unknown→置 unknown |
| STARTED + manual | 置 unknown |
| WAITING_HUMAN | 走既有 HITL 恢复（不 invoke） |
| COMPLETED | 不应出现在 dangling（D1 已滤）；防御性跳过 |

「置 unknown」= 账本 CAS unknown + task INTERRUPTED（RunOutcome(kind=INTERRUPTED, error_code=TOOL_OUTCOME_UNKNOWN)）+ OperationUncertain 事件（payload 带 revision 供 resolve_operation）+ **本步短路不再继续后续 dangling**。

- 备选（弃）：分派逻辑放 gateway——gateway 是执行咽喉不是恢复决策者；恢复的「该不该」与执行的「怎么做」分层正交。

### D3：闸门与 resolve_operation（P3）

- **闸门**：unknown 的 task 在 TaskManager 侧带 `error_code=TOOL_OUTCOME_UNKNOWN` 标记；`recover_agent` 的 assemble 遇到该标记且未 resolve → 抛专属 `OperationUncertainPending`（RuntimeError 子类）拒绝续跑，提示走 resolve_operation。
- **`resolve_operation`**（runtime 公开方法，与 recover_agent 同层）：
  - `supply_result`：CAS completed（result=宿主给的结构）→ 按 `operation_memory_result_id` 补写 TOOL_RESULT → task 重排（PENDING）续跑；
  - `retry_confirmed`：CAS 回 started（append attempt 标记宿主授权）→ task 重排；原 op_id 不变；
  - `cancel_task`：task 终态 CANCELED（不撤销外部动作——payload 如实措辞）；
  - revision 取自 OperationUncertain payload；CAS 失败即拒绝（天然双宿主互斥）。
  - 决策本身**记账**：CAS 的 update 里带上 decision 字符串（attempt 追加 `resolve:retry_confirmed`），审计可溯。

### D4：QueryResult 接口的最小面

```python
class QueryResult(Protocol):
    async def query_result(self, operation_id, ctx) -> QueryOutcome
# QueryOutcome: "completed"（带结果）| "definitely_not_started" | "unknown"
```

启动校验：ProviderRegistry 注册时扫描——cap 声明 queryable 且 provider 未实现 → ValueError（响亮，不静默降级为 manual）。

### D5：翻转面成对清单（P2）

| 载体 | 旧断言 | 新断言 |
|---|---|---|
| `test_crash_recovery_reconcile` 组 | dangling 必重跑 | policy 参数化：retry_safe 夹具重跑保持；manual 夹具 → INTERRUPTED+TOOL_OUTCOME_UNKNOWN+副作用 1 次 |
| WP0 夹具 `test_tool_outcome_unknown` | 副作用 1→2（盲重跑钉子） | manual 下副作用 1 次 + unknown + 事件带 revision；保留 `retry_confirmed` 后续跑的对照分支 |
| 无桩 h3 | after_recovery==2 | ==1 + task INTERRUPTED + TOOL_OUTCOME_UNKNOWN（fixed 语义） |
| 探针 H3 | False | True（recovery_duplicate 场景改接策略分派：manual 下计数 1） |

### D6：控制工具核验 = 测试钉子（P2 收尾）

WP5 已给控制工具账本身份。delegate 的「确认丢失重入」：gateway 短路对 completed 的 delegate 已防双建；测试钉 O-T14（staged 子任务存在 + 重入不再生成第二棵）。finish/metadata 幂等：completed 短路天然幂等，钉两枚断言。ask_user 复用：HITL 既有 waiting_human 路径，钉重入不重开请求。

## Risks / Trade-offs

- [默认 manual 终结「自动重跑」的旧习惯] → 升级说明 + reconcile 测试参数化让两种语义都有钉子；宿主显式声明是一行配置。
- [queryable 的外部查询本身失败] → QueryOutcome.unknown → 归 unknown 停住（不猜）； Provider 承诺权威否定才重跑。
- [resolve_operation 与 drain 竞态] → CAS revision 互斥 + task 重排走 TM 正常入队（不旁路）。
- [存量数据（WP5 前的 dangling）全部停住] → 这是方案的保守默认；宿主 resolve 一次即解锁。

## Migration Plan

无数据迁移；回滚 = revert（policy 字段默认值使行为回到盲重跑——回滚即重新引入 H3，README 注明）。生产宿主灰度：先 retry_safe 只读工具，再幂等键工具，最后 manual 需人处置工具（方案 §10.2 原序）。

## Open Questions

（无——P1/P2/P3 已定；方案 §5.4/§5.5 固定其余。）
