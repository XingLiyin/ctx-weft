"""数据类型层：这个系统的名词、词表、错误与旋钮。

    session.py         Session
    task.py            Task + TaskSettings ×3 + deserialize_settings + TaskInteractionMode
    agent.py           Agent + LoopGuard
    status.py          三种实体的生命周期状态词表 + 终态与 park 判据
    discriminators.py  事件 payload 里的判别值（reason / error_code）
    errors.py          异常类型
    config.py          RuntimeConfig —— host 注入的运行期旋钮

共同点很硬，这也是它们住在一起的全部理由：
- **几乎都是叶子**——只引 `protocols`、`core.content` 与彼此（唯一的例外见下）；
- **多数是对外契约**——状态字符串、判别值、错误码都由 host 按字面量分流，改值等于
  改协议（见 `status.py` / `discriminators.py` 各自的 docstring）；
- **入度最高**——core 的每个包都引它们。

包内依赖单向：`status` / `discriminators` / `config` 是纯叶子；
`session` / `task` 引 `status`；`errors` 引 `discriminators`。

⚠️ **`errors.py` 不是叶子**，如实记在这里：
- `crash_run_outcome` 在函数内引 `orchestrator.task.disposition.RunOutcome`
  （另有一处 `TYPE_CHECKING` 同源）。这是全仓仅存的一条 import 环
  （`models.errors ⇄ orchestrator`）。原处 docstring 写明了取舍：该工厂与
  `crash_error_code` 同处，而 `disposition` 是纯 stdlib、此方向无环，故函数内 import
  可行。真要消掉，得把 `crash_run_outcome` 移进 `disposition.py`——那会拆散它与
  `crash_error_code`，是另一次权衡，不在本次范围。
- `ContextOverflowError` 的报错文案在函数内引 `core.content._IMAGE_PART_TOKENS`。
  这一处**必须**保持函数内 import：`core.content` 模块级就引 `core.models.errors`，
  顶层引会成真环。原注释说「不成环、就地导入是刻意的」，在常量搬进 content 之后
  理由更硬了，不是更软。

**不做 re-export**：一个符号只有一条 import 路径，`Task` 恒是
`from ctx_weft.core.models.task import Task`。这样「这个名字从哪来」永远只有一个
答案，也不会出现包级出口与子模块两条路并存、日后谁也说不清该用哪条。
"""
