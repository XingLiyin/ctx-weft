# Proposal: strict-control-args-and-v2-docs

## Why

LLM 调用控制工具（如 `control__finish_task`）时传入 schema 未声明的参数（如 `result="..."`），CapabilityGateway 会静默剥除该参数：交付物丢失、结果不回流父任务、blackboard 不发布，全程零错误反馈、零事件、零日志告警（仅一条 INFO）。子 agent 端到端测试已实证这条静默失败链；且 `control_tools.py` 两处陈旧 description 仍在主动教 LLM 使用不存在的 `finish_task` `result` 参数，线上模型按旧习惯调用即触发。同时 ARCHITECTURE.md 停留在 v1 实现（`reason` 起步、`MemoryEventType` 词表、已删除的组件路径），与代码严重漂移，会误导贡献者。

## What Changes

- CapabilityGateway 对**控制工具**（`cap.id` 以 `control:` 开头）的未知顶层参数从「静默剥除」收紧为「显式错误反馈」：返回 `[Error: invalid arguments for '{tool}': unknown parameter(s) ...; declared parameters: ...]`，不调用 provider；错误经既有 act 循环回灌机制（append 进 current_messages）让 LLM 同 run 内改参重试。非控制工具（MCP / builtin / skill）维持既有 strip 语义不变。
- 修复 `run_observe_react` 的 terminal 工具缺口：terminal 工具（`report_task_outcome` / `collect_process_report`）返回 `is_error=True` 时不再终止观察循环，错误照常回灌、模型可重试；否则收紧后一次坏参数调用会把错误文案写成 Process Report。
- 修正 `control_tools.py` L415 / L514 两处陈旧 description（教 LLM 用 `finish_task` 的 `result` 参数 → 改为「最终答复 = 收尾回合正文」的正确契约）。
- 按 11 节漂移地图整篇重写 ARCHITECTURE.md 对齐 v2 实现（`prepare→act→observe→finalize` 流水线、MemoryKind 词表与派发框/finish 对机制、两阶段派发、AgentLifecycleManager / SessionRegistry 新组件路径、`recover_agent` 单 owner 恢复、78 个事件类型与治理集合等），并把新的控制工具严格参数校验写入 §6。
- 更新受影响的既有测试 mock（约 8 处模拟 LLM 传无效 `result=` 的调用点，机械修改：去掉无效键、正文挪进 `MockResponse(text=...)`）；新增 gateway 严格校验单测、observe 终端重试单测、以及「finish_task 误用 → 报错 → 裸调用重试 → FINISHED」的端到端回归测试。

## Capabilities

### New Capabilities

- `capability-gateway`: CapabilityGateway 对工具调用的参数校验契约——控制工具未知参数的显式拒绝与错误回灌、非控制工具的剥键语义、observe ReAct 循环对 terminal 工具失败的容错重试。

### Modified Capabilities

（无——`openspec/specs/` 当前为空，本 change 首建该 capability。）

## Impact

- **代码**：`src/ctx_weft/core/loop/capability_gateway.py`（invoke 插入严格校验 + 抽共享剥键资格 helper）、`src/ctx_weft/core/loop/steps/observe.py`（terminal is_error 守卫）、`src/ctx_weft/core/capabilities/control_tools.py`（2 处 description）。
- **测试**：新增 3 组测试；更新 `test_finish_delegate_e2e` / `test_outage_resume` / `test_send_message_after_session_done` / `test_send_message_e2e` / `test_subagent_instantiated_event` / `test_session_lifecycle_forget_rebuild` / `test_runtime_agent_api` 等约 8 处 mock。纯 memory fixture 型用例（`test_capsule_interleaved` / `test_legacy_capsule_recall`）不经 gateway，不受影响。
- **文档**：ARCHITECTURE.md 整篇重写（499 行 → 对齐 v2）。
- **不改动**：外部工具（MCP / builtin / skill）的参数处理行为；SILENT_TOOLS 不落 memory 的既有语义；`_strip_unknown_keys` 对非控制工具的实现与其锁定测试。
- **风险**：线上模型若习惯性带无效参数调用控制工具，会消耗额外轮次重试（act 循环既有容错语义兜底），换取的是误用可见、可恢复。
