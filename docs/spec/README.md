# CtxWeft 语言无关规范（spec）

> **2026-06-05 · 基于 `master` 分支现状抽取**
>
> 本目录是 Python / Java / TS 三份实现的**唯一真相**。凡涉及事件分类、状态机、reducer 规则、
> blackboard 语义的行为，以本目录为准；任一实现与本目录冲突，是实现的 bug。
>
> 配套：[TS 移植参考](../ctx-weft_TS移植参考.md) · [ARCHITECTURE](../../ARCHITECTURE.md)

---

## 为什么需要它

事件溯源架构里，**事件分类 + reducer 规则 + 状态机**决定了投影、崩溃恢复、前端渲染的一致性。
一旦有多份语言实现，这些规则若各自维护，必然漂移——投影对不上、恢复结果不一致、前端串台。

本目录把这些规则从任一语言抽出，配一套**黄金用例**（语言无关的 `事件序列 → 期望投影`），
三份实现各跑同一套用例，断言结果一致。

## 文件清单

| 文件 | 内容 | 对应源码（Python） |
|------|------|-------------------|
| [01-events.md](./01-events.md) | 冻结的事件类型清单、Event 结构、瞬态事件集合 | `core/events/types.py` |
| [02-step-state-machine.md](./02-step-state-machine.md) | initial_step 选择 + Step 跳转 | `core/loop/driver.py`、`runtime._resolve` |
| [03-reducer-rules.md](./03-reducer-rules.md) | 事件 → 投影的变更规则、状态映射 | `core/control/reducers.py` |
| [04-blackboard.md](./04-blackboard.md) | topic / intent / 发布覆盖 / 订阅 / review-reopen | `ARCHITECTURE.md §Blackboard` |
| [05-authz-and-hitl.md](./05-authz-and-hitl.md) | 工具鉴权（Authorizer/Gateway）+ HITL 生命周期 | `core/auth/`、`core/orchestrator/hitl_manager.py` |
| [06-memory-layers-and-compaction.md](./06-memory-layers-and-compaction.md) | **（设计目标，未实现）** 分层 memory（session/task/agent）+ 上下文两 source 重建 + 委派黑盒 + 两类 compact + observe 五态 | `protocols/memory.py`、`assembler/sources/`、`core/loop/steps/` |
| [07-hitl-suspend-resume.md](./07-hitl-suspend-resume.md) | **（设计提案，未实现）** HITL 热/冷两层：请求即持久化 + 协程热阻塞 + 超时驱逐降级为冷持久挂起 + 应答后热续跑/冷 resume + 跨重启可恢复 | `core/orchestrator/hitl_manager.py`、`core/auth/authorizer.py`、`task_manager.py` |
| [golden/](./golden/) | 黄金用例（JSON：事件序列 → 期望投影） | `tests/unit/test_snapshot_recovery.py` 等 |

## 一致性契约

每份实现必须提供一个测试入口，对 `golden/*.json` 逐个：

1. 读 `events`，按顺序喂入 `reduceEvents`（全量回放）。
2. 断言产出的 `RunStateView` 与用例的 `expected` 逐字段相等（见 [03](./03-reducer-rules.md) 的字段定义）。
3. 若用例含 `snapshotAt`，额外验证「快照(前缀) + 增量」与全量回放结果一致（见 [02 恢复语义](./02-step-state-machine.md)）。

## 版本与冻结

- 事件类型清单（`EVENT_TYPES`）在 V1 **冻结**：业务代码不得发未登记类型；新增类型须先改本目录。
- 任何对 reducer 规则 / 状态映射 / blackboard 语义的修改，**先改本目录 + 加黄金用例**，再改三份实现。
