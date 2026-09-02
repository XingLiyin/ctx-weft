# task 状态所有权重构 —— 遗留与后续立项

本次重构（`disposition_for` + `TaskManager.apply_run_outcome`）把 task 状态收口到了
TaskManager。以下是执行与评审过程中**有意留下**的尾巴，逐条都有裁定依据，不是遗漏。
按处理价值排序。

## 一、判据「唯一真源」尚未成真：现有四份

`disposition_for` 是名义上的唯一真源，实际有四份「该变成什么」的判断：

| # | 位置 | 状态 | 为什么留着 |
|---|---|---|---|
| 1 | `task_disposition.py::disposition_for` | 真源 | — |
| 2 | `finalize.py` 的 `retry_exhausted` | 与 #1 逐字等价，未漂移 | 它兼着门控 `terminal` → `finalize_task_memory` / BLACKBOARD / `TASK_FINALIZED.outcome`，整段删会丢闭合与回传 |
| 3 | `task_manager.py::_handle_task_failure` | 与 #1 的 INTERRUPTED 支逐字同构 | 装配阶段没有 run。但它完全可以喂 `RunOutcome(kind=INTERRUPTED, reason="assembly_failure", …)` 走同一张表 |
| 4 | `runtime.py` 的 `will_retry` | **已分叉** | 它没有终态守卫 |

**#4 的分叉是有后果的**：熔断已判 root FAILED、该 run 随后崩溃的竞态里，
`RunFinished.will_retry=True` 而 TM 实际一步不动 —— host 据此以为「先别关流」
会永远等不到。已写进升级须知，但没有测试钉住。

「唯一真源」这个说法目前在文档与注释里被反复使用，而读者去核会发现不成立 ——
这比一开始就承认「三处尾巴」更危险。要么收口 #3/#4，要么改措辞。

## 二、`_run_loop` 的守卫仍在读一个自己不再拥有的字段

`cancel_takes_effect` 与两处 `was_interrupted` 读的都是 `task.status`，
而 run 已经不写它了。**本次三条行为差异全部源于此**：

- observer 判死后 finalize 又崩 → 多发一条 `RunInterrupted`
- 熔断竞态下终态守卫收紧
- observer 判 fail 后边界取消 → 终态由 FAILED 变 CANCELED，且 `RunCanceled` 由不发变发

三条都已裁定接受（方向上都是修复：旧行为在拿一个「事件流从没见过的内存状态」
压掉真实发生的 run 级事实）。但**形态是错的**：正确做法是问 TM
（一个 `is_terminal(task_id)` 查询），或干脆交给 TM 侧统一守卫。
维持现状的代价：下一次「TM 什么时候写状态」的改动会**再次静默改变** run 域事件的发不发。

## 三、`retriable` 硬契约只有注释在守

`LLMOutageError.retriable` 实际是 `True`。「outage 从不原地重试」重构前靠路径隔离、
重构后靠 `RunOutcome.retriable=False`，而这条契约今天由三段注释加分支顺序维系
（`task_disposition.py` / `errors.py` / `runtime.py` 的 outage 支）。
结构上没有任何东西阻止下一个人把 `except LLMOutageError` 合进 `except Exception`、
或改调 `crash_run_outcome(exc)`（它用 `getattr(exc, "retriable", True)`）。

建议：给 outage 也做 `outage_run_outcome(exc)` 工厂，并让 `RunOutcome.retriable`
在 `kind is INTERRUPTED` 时成为必传（`__post_init__` 校验）——
把「忘了传」从静默默认变成构造期爆炸。

## 四、`TaskManager._emit` 恒写 `run_id=None, sequence=0`

不是本次引入（`4deb462` 起就是），但本次让它从 2 条事件扩大到 **9 条**。
现在事件流里一整类事件既无所属 run 又无序号，任何按 `(run_id, sequence)` 做排序、
去重、幂等重放的下游都退化成只能靠到达顺序 + 时间戳。
这是收口带来的**结构性代价**，值得单独立项：给 TM 一个 session 级单调序列，
或让处置事件继承 run 的 `run_id`。

另：`docs/spec/golden/02` 与 `10` 里 reopen 形态的 `TaskRequeued` 仍带 `runId`
与非零 `sequence`，与 `_emit` 的实际行为不符 —— 是**重构前就存在**的 golden 失真。

## 五、`suspend_requested` 住错了地方

它按定义是 run 内的东西，却挂在 `Task` 上。今天安全：`Task` 从不整体序列化，
恢复走构造器取默认 `False`，且 `_run_task` 每次派发归零。
但一旦将来 Task 被快照序列化并跳过 `_run_task` 恢复（如热迁移），
一个陈旧的 `True` 会让父任务被路由进 SuspendStep 却没有任何子任务，**永远醒不过来**。
归宿应是 `LoopState.extra`。

## 六、零碎

- `_suspend_task_interrupted` 对装配失败也硬编码 `reason="run_crash"`，
  调用方传的 `assembly_failure` 被丢（BASE 同形的既有缺陷；修它会改可见 payload）。
- outage 支的 `task.error` / `error_code` 与 `apply_run_outcome` 各写一份，纯冗余。
- 守卫 A 只认 `EventType.<NAME>` 属性访问，认不出**字符串字面量**形态 ——
  而 `task_disposition.py` 自己就用字符串表达事件类型，`apply_run_outcome` 才转回来。
  模仿这个写法的新模块能绕过守卫 A。
- 装配失败路径的端到端覆盖实际在
  `tests/unit/test_two_phase_dispatch.py::test_assembly_failure_no_task_started_and_labeled_requeue`
  （而非某轮报告里点名的另两个文件——那两个测的是 `_handle_task_failure` 直调分支）。
