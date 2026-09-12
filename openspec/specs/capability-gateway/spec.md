# capability-gateway

## Purpose

定义 CapabilityGateway 对工具调用的参数校验行为契约：控制工具（core 自有编排工具）未知参数的显式拒绝与错误回灌、外部工具的容错剥键语义，以及观察循环对终止工具失败的容错重试，保证 LLM 的参数误用可见、可恢复、不静默丢失信息；并固定执行参数与审计参数的三通道分离（Provider 收有效参数、审计只见脱敏副本）。

## Requirements

### Requirement: 控制工具未知参数显式拒绝

CapabilityGateway 对控制工具（capability id 以 `control:` 为前缀的工具）的调用 SHALL 校验顶层参数：当存在 schema 未声明的顶层键时，SHALL 返回结构化错误结果（`is_error=True`），错误文案 MUST 包含未声明的键名与 schema 已声明的参数清单，并以 LLM 可读的形式引导其只带声明参数重发调用；被调用的工具实现 MUST NOT 被执行。错误结果 SHALL 经既有的工具结果回灌通道进入当前 run 的消息序列，使 LLM 能在同一 run 内改参重试。

校验的适用范围 MUST 与剥键资格判定保持一致：当 schema 含组合关键字（`allOf`/`anyOf`/`oneOf`/`not`）、顶层 `$ref`、或显式允许附加属性（`additionalProperties` 为真值或子 schema）时，SHALL 不作未知参数拒绝（fail-open）。

#### Scenario: finish_task 误带 result 参数被拒

- **WHEN** LLM 调用 `control__finish_task` 并传入未声明的 `result="..."` 参数
- **THEN** 该调用返回 `is_error=True` 的错误结果，文案含未知键名 `result` 与已声明参数 `deliverables_summary`；finish_task 的实现不被执行；错误内容出现在当前 run 的后续消息序列中供 LLM 重试

#### Scenario: 控制工具全声明参数正常放行

- **WHEN** LLM 调用任一控制工具且所有顶层参数均在 schema 声明
- **THEN** 调用正常执行，行为与收紧前完全一致

#### Scenario: 剥键不适用的 schema 不拒绝

- **WHEN** 某控制工具的 schema 含组合关键字或显式 `additionalProperties` 允许附加属性，且调用带了 schema 之外可能的顶层键
- **THEN** 不触发未知参数拒绝（与剥键的 fail-open 判定一致），调用按既有容错语义处理

### Requirement: 非控制工具维持剥键容错语义

CapabilityGateway 对控制工具之外的工具（MCP、builtin、skill 等）的未知顶层参数 SHALL 维持既有剥键行为：未声明键被剥除后仅以声明参数调用工具，不产生错误反馈；参数校验仍仅拦截 required 缺失、type 不符、enum 越界三类约束。

#### Scenario: MCP 工具臆造键被静默剥除

- **WHEN** LLM 调用某 MCP 工具并附带 schema 未声明的臆造键
- **THEN** 该键被剥除，工具以声明参数正常执行，调用方收到正常结果而非错误

### Requirement: 观察循环终止工具失败可重试

observe 的 ReAct 循环（前台裁决与后台过程报告共用）在终止工具（terminal tool）调用返回 `is_error=True` 时 SHALL NOT 以该错误结果终止循环；错误内容 SHALL 作为工具结果回灌消息序列，LLM MUST 能在后续轮次修正参数重新调用；仅当终止工具调用成功时其结果才 SHALL 作为循环的终止结果返回。

#### Scenario: 终止工具首次参数非法后重试成功

- **WHEN** 后台观察循环中 LLM 首次调用终止工具时参数非法（返回错误结果），下一轮以合法参数再次调用
- **THEN** 循环不在首次错误处终止；最终返回的是第二次调用的成功结果，错误文案不进入过程报告

#### Scenario: 轮次耗尽仍无成功终止调用

- **WHEN** 终止工具连续失败直至轮次耗尽
- **THEN** 循环返回无终止结果（与纯文本超时路径一致的既有兜底行为），不把错误文案当作终止结果

### Requirement: 控制工具 schema 描述与声明参数一致

控制工具对 LLM 暴露的参数描述（description）MUST 与其 schema 声明的参数一致，MUST NOT 引导 LLM 使用不存在的参数；特别是 finish_task 契约：最终答复是收尾回合的消息正文，不是任何工具参数。

#### Scenario: finish_task 描述不再提及 result 参数

- **WHEN** 读取暴露给 LLM 的控制工具 schema 与描述
- **THEN** finish_task 及相关工具的描述中不存在「把最终输出放进 finish_task 的 `result` 参数」一类措辞，均指向「正文即答复」的正确契约

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
