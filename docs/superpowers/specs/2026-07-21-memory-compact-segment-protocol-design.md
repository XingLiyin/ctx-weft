# memory 协议：apply_compact 段作用域正式化与 provider 迁移

日期：2026-07-21
状态：已批准（用户确认设计后实施）

## 问题

段作用域折叠（同日两个 fix：`since_last` 参数 + 渲染序排序）改变了 `MemoryProvider.apply_compact`
的**协议**——签名多了参数、段界/锚点判定的排序基准从 seq_no 改为渲染序。但这次演化只落在
ctx-weft 内置的 `InMemoryMemoryProvider` 上，协议之外的实现方没有同步：

1. **静默失败**：IpMasterCoworkPy 的 `postgres.py` provider 签名没有 `since_last`，
   每次段折叠直接 `TypeError`，被 `_run_background_observe` 的 `except Exception` 当作
   运行时故障吞掉——recap 事件照发、段折叠永远不发生、日志只有一条易被忽略的
   exception（实证：`tsk_01KY1XZ5TN0BVV5YW89ZXVXJXC` 共 16 条记录，零摘要、零 supersede）。
2. **排序契约不可见**：「apply_compact 的段界与锚点判定必须按渲染序 (timestamp, seq_no)，
   与 recall/装配一致」是语义约定，只存在于 in_memory 的实现注释里。旧 seq 序实现（如
   postgres.py 现状）在坍缩 UP（timestamp 回填、seq 最高）在场时段界失效——即使补上
   `since_last` 参数也会复现「摘要写了、raw 不折」。

## 决策

**协议正式演化，不加探测层、不 bump 版本。**

- `since_last` 与排序契约进协议正文（`protocols/memory.py` docstring），成为
  `apply_compact` 的正式语义；所有 provider 实现方据此迁移。
- 兼容管理靠 wheel 锁版本（provider 与 ctx-weft 同步升级），不引入启动时签名探测 /
  能力 flag / 双轨回退——一作者两仓库的生态里那些是纯仪式（YAGNI）。
- 失败可见性：契约错误（TypeError）**打 ERROR 日志 + 段保 raw 降级**，不抛——与运行时
  故障同样降级，但日志显式指出「provider 与协议不兼容」，第一次折叠即可发现。

## 协议契约（写进 protocols/memory.py）

```
async def apply_compact(scope, summary, keep_last, ctx,
                        layer=AGENT, protect_types=(), since_last=None) -> CompactResult
```

1. **since_last**：非 None 时归档池限定在「最后一条 active 该类型记录之后」；该类型记录
   不存在则不限定（整 scope）。段边界折叠传 `USER_PROMPT`。
2. **排序契约**：段界搜索、归档池切分、锚点判定一律按**渲染序 `(timestamp, seq_no)`**，
   与 recall 的 timestamp 序一致。不得用裸 seq_no——存在 timestamp 回填、seq 更高的
   合法记录（L3 坍缩 UP，`collapse_task_layer`）。
3. **锚点语义**（不变，随契约显式化）：摘要落「被折区起点之后第一条幸存事件之前」；
   段尾无幸存者则锚被折段末条位置（不用 now()）。

## 改动点

1. **ctx-weft `protocols/memory.py`**：`apply_compact` docstring 补排序契约（第 2、3 条）；
   `since_last` 语义已写，无需重复。
2. **ctx-weft `background_observe.py`**：`_run_background_observe` 的异常处理拆两层——
   `except TypeError` 先行：`logger.error("apply_compact 协议不匹配（provider 缺 since_last
   或签名过旧）…segment kept raw")`，段保 raw、close 边界照弹 synth 登记；其余
   `except Exception` 维持现状（logger.exception + 段保 raw）。`observe._fold_retry_segment`
   在 run 内同步执行、异常本就上抛，不动。
3. **IpMasterCoworkPy `providers/memory/postgres.py`**：镜像 in_memory 修复——
   - 签名加 `since_last: MemoryEventType | None = None`；
   - 全量查询排序改 `ORDER BY timestamp, seq_no`；段界 = 结果集中最后一条该类型记录，
     归档池 = 其后的切片；
   - 锚点判定的 `archived_min_seq` / `following` 比较键改 `(timestamp, seq_no)`；
   - 补 provider 测试：两段场景只折当前段、坍缩 UP（ts 回填 + seq 最高）在场仍正确折叠
     （断言与 ctx-weft `test_segment_scoped_fold.py` 的 provider 层用例同构）。
4. **发布**：版本号不变；rebuild vendored wheel → 重装 venv → 应用侧重跑
   「多轮对话 + 查看工作区」场景，验证段折叠落库（有 TASK_COMPACT_SUMMARY、raw 被
   supersede、摘要锚在对应 UP 之后）。

## 不做的事

- 不 bump ctx-weft 版本号（用户决策）。
- 不做启动时签名探测 / MemoryProviderInfo 能力 flag / 支持性双轨回退。
- 不改 postgres.py 之外的应用侧代码；不动 `escalating_compact` 等 apply_compact 的
  其他调用方（不传 since_last = 整 scope 折叠，旧语义保留为参数缺省行为）。

## 验证

- ctx-weft：`test_segment_scoped_fold.py` 全绿（已有）；新增 bg observe TypeError 降级
  路径测试（provider 抛 TypeError → ERROR 日志 + 段保 raw + 不抛出）。
- IpMasterCoworkPy：postgres provider 新增测试全绿；真实 DB 场景复验（上述发布步骤）。
