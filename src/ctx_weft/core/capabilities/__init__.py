"""Capability 层：缓存 / 解析 / 两个内置 provider。

**为什么不在 orchestrator**：这四个模块此前住在 `core.orchestrator` 下，但调度那半边
一行都没引用它们——真正的消费者是 `core.loop`（`steps/_capabilities.py`、
`capability_gateway.py`、若干 step）、`core.assembler.composer`（引控制工具的限定名）
与 `core.runtime`（构造并注册）。orchestrator 对它们**零读零写**。

`capability_cache.py` 原先的 docstring 说「实例化时填充（AgentLifecycleManager.
instantiate）」，那句话是假的：全仓唯一的 `put()` 调用点在
`core/loop/steps/_capabilities.py`（PrepareStep）。它住在 orchestrator 从来没有过
技术理由，只有历史——ALM 曾经填充过缓存，那条线后来搬去了 PrepareStep，文件没跟着走。

**为什么不在 `core/loop/` 里**：`core.assembler.composer` 也要引控制工具的限定名，
而 `core.loop → core.assembler` 已有 6 处依赖。放进 loop 会造出
`assembler → loop → assembler` 的环。本包坐在两者共同的下游。

**为什么 `control_tools.py` 不叫 `control.py`**：`core/control/` 已经存在（控制平面：
CancelToken / reducers / replay），两个 `control` 并存会持续误导。而且这个名字更准确
——文件内容就是 7 个内置控制**工具**加它们的 provider。

本包不做 re-export（一个符号只有一条 import 路径），请按子模块引。
"""
