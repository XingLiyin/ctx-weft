"""生命周期注册表：session 与 agent 的身份、配置、状态住在哪里。

`agent_manager.py` 是 agent 身份与配置的**唯一住所**（`_agents`），并订阅总线把
`TASK_*` 翻成五态机输入、发出 `AGENT_*`；`agent_state.py` 是那台状态机的纯函数层。
`session_registry.py` 只是「这个 session 里注册了哪些 agent」的登记表——会话状态
本身住在各 agent 身上。`template_lookup.py` 按 `cap.id` 前缀路由加载模板。

依赖方向：本包 → `../task`（`session_registry` 运行期构造 `TaskManager`）。反向没有
——那条曾经存在的 `TaskManager → SessionRegistry` 类型边，其唯一来源是一个从未被读过
的字段，已随 TaskManagerHooks 一并删除。

本包不做 re-export，请按子模块引。
"""
