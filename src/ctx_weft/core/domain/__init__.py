"""领域层：这个系统的名词与词表。

`models.py` —— Session / Task / Agent / LoopGuard / TaskSettings
`status.py` —— 三种实体的生命周期状态词表 + 终态与 park 判据

两者都是**纯 stdlib 叶子**（只引 `protocols`），也都是**对外契约**：状态字符串由
host 按字面量分流，改值等于改协议。

**本包刻意不做 re-export**——一个符号只有一条 import 路径（与
`tests/unit/test_protocols_events_relocation.py` 守的是同一条性质）。请按子模块引：
`from ctx_weft.core.domain.models import Task`。

前身是 `core/state/`。那个名字说的是「运行期状态」，装的却是领域实体与词表；它的
包 `__init__` 还转发着 `providers.events.InMemoryEventStore`——一条 core → providers
的反向依赖，且全仓零消费者（134 处 import 全部直接走 `core.state.models`）。随本次
更名一并删除。
"""
