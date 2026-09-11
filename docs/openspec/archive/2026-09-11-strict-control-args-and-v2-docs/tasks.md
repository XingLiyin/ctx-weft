# Tasks: strict-control-args-and-v2-docs

## 1. Gateway 严格校验（核心行为）

- [x] 1.1 从 `_strip_unknown_keys` 抽出共享的「剥键资格 + 未知键计算」helper（组合关键字 / `$ref` / 显式 `additionalProperties` → fail-open 判定不变），并保证 `tests/unit/test_strip_unknown_keys.py` 全绿（纯函数行为不变）
- [x] 1.2 在 `CapabilityGateway.invoke()` 的 `_raw` 哨兵之后、strip 之前插入控制工具严格检查（`cap.id.startswith(f"{CONTROL}:")` + 未知顶层键 → `_error_and_record` 返回 `unknown parameter(s): …; declared parameters: …` 错误、不调 provider），验证 `tests/unit/test_gateway_arg_validation.py` 新增用例通过
- [x] 1.3 新增 gateway 单测三例：控制工具+未知键报错（provider 不被调用、文案含未知键与声明参数）/ 控制工具+全声明键正常 / 非控制工具+未知键仍静默剥（回归锚），全部通过

## 2. 观察循环 terminal 容错（预存缺陷随行修复）

- [x] 2.1 `run_observe_react`（observe.py）terminal 工具结果加 `is_error` 守卫：错误不终止循环、照常回灌重试，仅成功结果作为 terminal_result
- [x] 2.2 新增单测：terminal 工具首调参数非法报错、次调成功 → 返回成功结果而非错误文案；轮次耗尽 → 返回 `(None, last_text)` 兜底，验证通过

## 3. 根因 docstring 修正

- [x] 3.1 修正 `control_tools.py` L415（report_task_outcome.task_summary）与 L514（collect_process_report.task_summary）两处 description，删除教 LLM 用 `finish_task` `result` 参数的措辞、改为「最终答复 = 收尾回合正文」契约，验证 `tests/unit/test_tooldecl_docstring.py` 等相关测试通过

## 4. 既有测试 mock 更新（收紧后的预期可见效应）

- [x] 4.1 全量 grep `"control__finish_task"` 复查经 gateway 的 `result=` 调用点清单（设计 D7 口径），确认无遗漏、纯 memory fixture 型用例不动
- [x] 4.2 更新 mock（4.1 全量复查后实际 14 个文件）：finish_task 的 `result=` → 裸参+正文（`test_finish_delegate_e2e`×2、`test_outage_resume`、`test_send_message_after_session_done`、`test_send_message_e2e`、`test_subagent_instantiated_event`、`test_session_lifecycle_forget_rebuild`、`test_runtime_agent_api`×2、`test_turn_handle`）；observe mock 旧参数名 `task_process_report` → 声明参数 `act_recap`/`task_summary`（上列集成文件 + `test_background_observe`、`test_deferred_close_raw_fold`、`test_segment_scoped_fold`、`test_dispatch_boundary_recap_e2e` 本就正确）；fake gateway 返回形态对齐生产 `InvocationResult`（含 `is_error`，`test_observe_react_helper`/`test_background_observe` 等 7 处），逐个验证原断言仍通过
- [x] 4.3 在 `tests/integration/test_subagent_invoke_e2e.py` 追加 e2e：root 第一次 `finish_task(result=…)` 被拒（错误回灌可见）、第二次裸调用成功 → 任务 FINISHED 且 outputs 正常，验证通过

## 5. ARCHITECTURE.md 重写对齐 v2

- [x] 5.1 重写 §1–§4（总览图与数据流、组件职责表、Loop 引擎与 step 流水线含 mermaid+ASCII 双图、LoopState/LoopContext/StepOutcome），按漂移地图更新：`prepare→act→observe→finalize` 8 step、`_reconcile_or` 选起步、PrepareStep 内联升级压缩、recognize_intent 旁路、`_run_loop` 新结局语义与 RunOutcome；抽查组件表 file:line 锚点与源码一致
- [x] 5.2 重写 §5–§7（ContextAssembler 8 Source、CapabilityGateway v2 链路并写入本 change 的控制工具严格校验、两阶段任务派发与 `_SessionTaskRunner.assemble` 的 use_subagent 分支），抽查 file:line 一致
- [x] 5.3 重写记忆写入时机表、Blackboard/Topic 机制（PUBLICATION 覆盖语义、订阅 no-op 现状、派发框/finish 对/bubble 真实通道）、会话生命周期与 `recover_agent` 单 owner 恢复、事件系统（78 类型 + TRANSIENT/L_TIER 治理集合 + provisional 窗口），抽查 file:line 一致
- [x] 5.4 文首行号说明改为当前分支口径；用 grep 核对全文引用的文件路径全部真实存在

## 6. 全量验证

- [x] 6.1 跑 `uv run pytest tests/unit -q` 全量通过（无新增失败）
- [x] 6.2 跑 `uv run pytest tests/integration -q`：基线 105/106（唯一预存失败 `test_multiround_retry_accumulates_then_l3_collapses_e2e` 保持、不新增失败）
- [x] 6.3 跑 `openspec validate "strict-control-args-and-v2-docs"` 通过（工件完整性校验）
