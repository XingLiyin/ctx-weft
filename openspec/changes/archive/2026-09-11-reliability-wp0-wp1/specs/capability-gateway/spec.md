# Delta Spec: capability-gateway

## ADDED Requirements

### Requirement: 执行参数与审计参数分离

CapabilityGateway 处理工具调用的参数时 SHALL 区分三条通道，三者不得共用同一可变对象：

- **original_arguments**：模型原始请求参数。SHALL 保持原值不被破坏，用于审批指纹（invocation_key 等）；公开展示如需脱敏 MUST 使用单独副本。
- **effective_arguments**：经授权/HITL 修改（如有）并再次通过 schema 校验后的参数。SHALL **不脱敏**，原样传给 Provider 执行。
- **audit_arguments**：对 effective_arguments 生成的脱敏副本（当前为 `headers` 中 `authorization/cookie/x-api-key/x-auth-token` 值替换为 `***`）。事件、审计与普通日志 MUST 只使用该副本。

Provider MUST NOT 收到被脱敏破坏的参数；审计与事件 payload MUST NOT 泄露敏感头明文。鉴权与校验顺序 SHALL 保持：原始请求 → 授权/HITL → 修改后参数重新校验 → 执行。

#### Scenario: 原始认证头到达 Provider

- **WHEN** LLM 调用某带 `headers.Authorization` 参数的工具且授权通过
- **THEN** Provider 收到的 `headers.Authorization` 为原始明文值；对应审计事件 / TOOL_AUDIT 记录中该值为 `***`

#### Scenario: HITL 修改后的参数原样执行

- **WHEN** 人工在 HITL 中批准并修改了参数（含认证头），调用继续执行
- **THEN** Provider 收到修改后、未脱敏的参数；该参数已经过 schema 校验

#### Scenario: 调用方传入的原始参数字典不被修改

- **WHEN** 一次 invoke 全流程完成（含脱敏审计路径）
- **THEN** 调用方传入的 original arguments dict 内容保持不变（脱敏发生在副本上，无原地改写）

#### Scenario: 审计脱敏不影响执行通道

- **WHEN** 审计副本生成后执行继续
- **THEN** 传给 Provider 的 effective 参数对象不因脱敏操作而被改写（两通道无别名共享）
