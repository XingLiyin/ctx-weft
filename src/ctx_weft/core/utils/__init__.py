"""通用工具：时间、id、事件封套、内容处理、token 估算、跨层契约字符串。

    clock.py      now_utc / as_utc
    ids.py        generate_id
    event.py      new_event / emit_event（事件封套的唯一构造点）
    content.py    多模态 content 的归一化、渲染、外部化、图片计量
    estimate.py   token 估算：文本费率、窗口换算、消息与工具调用
    headings.py   跨层的 prompt 标题契约字符串

**共同点**：它们的消费者横跨 core 的每个包，多数连 `providers/` 也在引。这正是它们
不能下放进任何一个包的原因——放进其中之一，就会逼其余的反向引它。

前身是一个 389 行的 `core/utils.py` 杂货铺：id/时间、token 估算、内容渲染、
JSON Schema 提取、两个 prompt 标题挤在一起。拆开后，真正属于某个包的两样已经归位：
`extract_schema` -> `core/capabilities/schema.py`（消费者只有 capability 域与
providers 的工具声明）、`PROGRESS_SO_FAR_HEADING` -> `assembler/sources/_history.py`
（唯一消费者，也是唯一渲染者）。留在这里的是真正跨层的那部分。

包内依赖单向：`clock` / `ids` / `headings` 是纯叶子；`event` 引 clock + ids；
`estimate` 引 content。

**不做 re-export**：一个符号只有一条 import 路径。
"""
