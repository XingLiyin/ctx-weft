# Design: reliability-wp0-wp1

## Context

上游方案 `docs/plans/2026-09-11-agent-core-reliability-plan.md` §5.1（H4 参数分离的确定性规则）与 §8（WP0/WP1 工作包）。验证报告（2026-09-11）已坐实：`capability_gateway.py:386` `sanitized = _sanitize(effective_args)` → `:603` `provider.invoke(cap_id, sanitized, ...)`——执行与审计共用同一份脱敏对象；探针 `--expect baseline` 的 H4 输出 `provider_authorization: "***"`。

现状关键事实：

- `_sanitize`（capability_gateway.py:1005）是浅拷贝 dict + 重建 `headers` 子 dict，不改入参——脱敏本身已是副本操作，问题只在**把副本同时喂给了执行通道**。
- `_record_invocation`（:397）与 `_stream_tool`（:406→:603）都收 `sanitized`；审计用脱敏版是对的，执行用脱敏版是 bug。
- `invocation_key`（:130）用**原始** `arguments` 计算指纹（HITL 决定缓存第四维）——original 通道已在位，无需新建。
- HITL 改写经 `decision.modified_arguments`（:336）进入 `effective_args`，随后 coerce/校验——顺序符合方案要求，本包不动。

## Goals / Non-Goals

**Goals:**

- Provider 收到授权后未脱敏的有效参数（H4 修复）；审计/事件只含脱敏副本。
- 调用方传入的原始 dict 全程不被修改（探针已隐含，本包显式钉住）。
- H1/H2/H3 缺陷行为被三份 Runtime 基线夹具钉进测试（WP0），供 WP3/WP4/WP6 实施时按新契约翻转。

**Non-Goals:**

- 递归脱敏 / 嵌套敏感键识别（方案 §5.1：后续包补测试后再扩展）。
- 冷恢复读取原执行参数的受保护通道（WP5 操作账本）。
- 事件提交/快照/操作账本/执行限制的任何行为变更（WP2–WP7）。
- 不移除 `_sanitize` 对 `headers` 之外的扩展点——本包维持现有脱敏键集合。

## Decisions

### D1：三条通道 = 两个既有对象 + 一个显式审计副本，零新类型

不引入 `original/effective/audit` 三元 dataclass——original 已是入参 `arguments`（`invocation_key` 在用），effective 已是 `effective_args`（coerce/校验后）。改动仅两处：

```python
sanitized = _sanitize(effective_args)          # 审计副本（现名沿用，语义收窄为 audit）
await self._record_invocation(..., sanitized, ...)        # 审计：脱敏版（不变）
streamed = await self._stream_tool(provider, cap.id,
                                    effective_args, ...)  # 执行：不脱敏（原先传 sanitized）
```

- 备选：新建 `ArgumentChannels` 包装对象贯穿 gateway——为纯内部管道引入新类型徒增噪声；三条通道在本包只在 invoke 内部流转，局部变量即边界。弃。
- `_sanitize` 改名/注释收窄语义为「audit 副本生成」；`sanitized` 变量更名 `audit_args`（局部，无外部影响）。
- `_stream_tool` 内部的 `provider.invoke(cap_id, sanitized, ...)`（:603）随之改收执行参数；确认该函数内没有把同一对象又写进审计（事件 payload 的脱敏由 `_redact_content_for_event` 等独立机制承担，与参数无关——实施时核实）。

### D2：事件 payload 的脱敏与参数脱敏是两条既有机制，不合并

`CapabilityInvoked` 等事件 payload 里若带参数快照，走事件侧 `redact_content_for_event`；本包只改「进 Provider 的执行实参」与「TOOL_AUDIT/invocation 记录的参数」。两机制保持独立，避免为 D1 顺带重写事件脱敏（回归面失控）。实施时若发现 `_record_invocation` 之外还有直接序列化 `effective_args` 进事件的位置，逐一确认脱敏路径后再放行——这是本包唯一的谨慎点。

### D3：WP1 验收测试先建、必须在基线失败

`tests/unit/test_gateway_argument_channels.py` 五组用例（方案 §8 WP1）：原始认证头 / HITL 改写后认证头 / 非敏感字段不受影响 / 输入 dict 不变 / 审计副本为 `***`。前两组在当前基线**必红**（provider 收到 `***`），修复后转绿——按方案「先建立失败用例，再改变实现」的固定步骤。

### D4：WP0 夹具钉「缺陷现状」，翻转归属写进注释

三份夹具复刻探针语义但走完整 Runtime（真 bus/store/TaskManager），barrier 控时序：

- `test_runtime_storage_failure.py`（H1）：FailingStore + 观察 emit 不抛 / observer 收到 / stored=0——**钉住旧契约**，注释标明「WP3 按 required 提交门翻转」。
- `test_snapshot_commit_interleaving.py`（H2）：双 task 交错（B 触发快照、A 后提交），断言全量= {a,b} 而快照恢复={b}——钉住缺陷，注释标「WP4 按 position 游标翻转」。
- `test_tool_outcome_unknown.py`（H3）：结果写入失败 + ReconcileStep 重跑（副作用×2）——钉住缺陷，注释标「WP6 按恢复策略翻转」；含子进程夹具（独立 SQLite 计数器验证进程退出后副作用证据仍在）。

夹具与探针脚本并存：探针是评审证据（机器可读、单文件），夹具是 CI 内的回归锚。**不修改**既有 `test_persister_swallows_store_errors`（那是 WP3 的活）。

### D5：H3 子进程夹具的边界

子进程只承载「副作用计数器」（SQLite 文件），Runtime 仍跑在测试进程内、用 `os.kill`/`Process.exitcode` 模拟中断点——不真杀测试进程（pytest 语义不允许）。真·进程退出矩阵归 WP6（方案 O-T05/O-T06），本包夹具只要「崩溃点之后计数器可读」成立。

## Risks / Trade-offs

- [执行通道改传 effective 后，某 provider 依赖 `***` 占位的隐藏行为] → 全量跑 `test_gateway_*`/`test_mcp_*`/`test_builtin_*`；`***` 从执行通道消失是修复而非回归，若现有测试断言 `***` 到达 provider，属本包有意翻转（逐一记录）。
- [审计侧出现新的明文泄漏点（effective 改名后误传事件）] → D2 的逐一核实 + `test_event_redaction.py` 全绿作为门禁。
- [WP0 夹具与探针对「缺陷值」的双重维护] → 夹具注释引用探针行号；WP3/4/6 翻转时两者同 PR 更新。
- [子进程夹具在 Windows/CI 的稳定性] → 计数器用 SQLite 文件（跨平台、无端口占用）；子进程只用 stdlib；tmp_path 隔离。

## Migration Plan

WP1 无数据/协议迁移，单 commit（测试+实现）可独立发布与回滚；WP0 夹具独立 commit（纯新增测试）。回滚 = revert，无状态残留。

## Open Questions

（无——通道定义、测试口径、夹具翻转归属均沿用上游方案 §5.1/§8 的固定表述。）
