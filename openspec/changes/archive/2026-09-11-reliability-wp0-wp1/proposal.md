# Proposal: reliability-wp0-wp1

> 上游方案：`docs/plans/2026-09-11-agent-core-reliability-plan.md`（WP0 + WP1，方案 §8）。
> 本 change 基于该方案的验证报告（2026-09-11：六项假设 6/6 属实，探针 `--expect baseline` 退出 0 逐字节复现）。

## Why

验证报告坐实 H4：CapabilityGateway 把脱敏后的参数（`_sanitize` 将 `headers` 里的 `Authorization/Cookie/x-api-key/x-auth-token` 改写为 `***`）传给真实 Provider（`capability_gateway.py:386` → `:603` `provider.invoke(cap_id, sanitized, ...)`）。任何以 `headers` 为功能参数的工具（HTTP 抓取类 capability）的真实凭证在进 Provider 前就被销毁——包括人在 HITL 里刚批准/修改过的参数。探针实测：provider 收到的 `Authorization` 为 `***`。审计脱敏本身是对的，错在执行通道与审计通道共用同一份对象。

同时，H1（落库失败仍通知成功）、H2（延迟提交的旧 ID 被快照跳过）、H3（副作用完成后恢复盲重跑）已在验证中坐实但修复属后续发布单元（WP2–WP4 / WP5–WP6）；按方案 WP0，先把这三个缺陷的探针移植为完整 Runtime 基线夹具，把「现象成立」钉进测试，防止后续工作包实施时基线漂移。

## What Changes

- **WP1（行为修复，H4）**：CapabilityGateway 内部固定区分三条参数通道——`original_arguments`（模型原始请求，审批指纹，不为执行破坏原值）、`effective_arguments`（授权/HITL 修改后、再次通过 schema 校验，不脱敏，传 Provider）、`audit_arguments`（对 effective 递归生成的脱敏副本，用于事件/日志/审计展示）。Provider 收到授权后的有效参数；审计保留脱敏副本。
- **WP1 边界**：本包只修复当前 headers 脱敏问题并保证输入 dict 不被修改；鉴权顺序保持「原请求 → 授权/HITL → 修改后参数重新校验 → 执行」。递归脱敏扩展、嵌套敏感键识别不在本包（后续包须补测试，不宣称自动识别所有秘密）。冷恢复读取原执行参数的通道属 WP5（操作账本），不在本包。
- **WP0（基线夹具，无行为变更）**：把 H1/H2/H3 探针移植为三份完整 Runtime 测试（`test_runtime_storage_failure.py` / `test_snapshot_commit_interleaving.py` / `test_tool_outcome_unknown.py`），使用 asyncio.Event/barrier 控制时序、不用随机 sleep；H3 增加子进程夹具（独立 SQLite 计数器，Runtime 进程退出后副作用证据仍存在，tmp_path，不连真实服务）。这些测试**钉住当前缺陷行为**（与既有 `test_persister_swallows_store_errors` 同性质），将在 WP3/WP4/WP6 实施时按新契约有意翻转——测试内注释标明所钉的假设编号与翻转归属工作包。
- 不改：事件提交/快照机制（WP2–WP4）、操作账本（WP5–WP6）、执行限制（WP7，含验证报告新发现的 `timeout_per_step_sec` 死配置弃用）、Runtime 拆分（WP9）。

## Capabilities

### New Capabilities

（无）

### Modified Capabilities

- `capability-gateway`: 新增「执行参数与审计参数分离」需求——Provider SHALL 收到授权后未脱敏的有效参数；事件/审计 SHALL 使用脱敏副本；模型原始参数 SHALL 保持原值用于审批指纹。（对既有 4 条 requirement 无变更，纯新增关注点。）

## Impact

- **代码**：`src/ctx_weft/core/loop/capability_gateway.py`（`invoke` 的参数管线：`_sanitize` 只作用于审计副本，`_stream_tool`/`provider.invoke` 改收 `effective_args`；确认 HITL `modified_arguments` 路径同样生效）。
- **测试**：新建 `tests/unit/test_gateway_argument_channels.py`（WP1 验收：原始认证头、HITL 改写后的认证头、非敏感字段、输入 dict 不被修改、审计副本脱敏）+ WP0 三份基线夹具。
- **回归面**：`test_gateway_authz_hitl.py`、`test_event_redaction.py`（WP1 命令组）；WP0 不触碰现有测试。
- **风险**：曾依赖「provider 收到 ***」的错误行为的宿主（理论为零——该行为是功能性破坏）；审计事件 payload 的脱敏语义不变。
