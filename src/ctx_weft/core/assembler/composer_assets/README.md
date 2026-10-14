# Composer prompt assets

Prompt 的“怎么写”和 loop 的“取哪些数据、装饰哪条消息”分开维护。
没有 YAML recipe，也不再用 PromptDefinition 的多种 messages 类型描述正文。

## 从哪里开始编辑

每个 purpose 有一个标准 Jinja 入口，内部用 import/include 组合已有片段：

| 请求 | 总入口 | 尾部正文 | 身份 facet / 工具过滤 |
| --- | --- | --- | --- |
| act | entries/act.en.jinja | act/guidance.en.jinja；必要时追加 resume | act / act |
| observe | entries/observe.en.jinja，judge_task=True | ROLE + COMPACT + review/context + review/judge | observe + compact / observe |
| observe（仅摘要） | 同 observe，judge_task=False | COMPACT + review/context + review/recap | compact / observe |
| compact | entries/compact.en.jinja | compact/prompt/task 或 agent | compact / 无工具 |
| recognize_intent | entries/metadata.en.jinja | metadata/prompt.en.jinja | metadata（兼容 recognize_intent key）/ recognize_intent |

观察只使用 `purpose="observe"`；前台和后台不再选择不同 purpose。
`ReviewIntent(judge_task=True/False)` 控制是否加载 ROLE、是否要求判决。
两档都使用 `report_observation`：`act_recap` 必填，`task_status` 在 schema 中可选，
但判决模式漏填时 ObserveStep 会要求补交。仅摘要模式忽略判决字段；不携带 ask_user。
`act_recap` 是本段增量回顾，`task_summary` 在任务收尾时承载整体过程报告。
工具只返回结构化数据，判决修正和结果应用属于 ObserveStep／既有任务管理路径。

中文对应 .zh.jinja；现有运行路径继续默认英文，不改变原来的语言选择。
observe/compact/metadata 的 system 仍使用 act 人格。
observe 的 evaluation_role 只承载可选的 ROLE，summary_principles 始终承载 COMPACT；
两者分别进入 identity block、计入预算，不互相冒充，也不因 COMPACT 存在而开启评价。
judge 与 recap 都固定输出总结原则段；COMPACT 缺失或为空时，在同一位置引用对应语言的 compact/role_fallback。
compact/metadata 入口仍使用原有 Role 段及回退行为。
ROLE 负责评价是否完成、是否符合约束；COMPACT 负责总结内容的取舍与忠实性；
本次范围、边界、字段及提交工具仍由 review 指令与工具契约规定。
judge / recap 共用 observe 入口，以现有 ReviewIntent.judge_task 分支；不再有独立的 judge / recap 入口文件。
正文按 evaluation_role 是否非空决定是否附加评价原则；judge_task 选择末尾的提交协议。

每个 entry 直接定义自己的 system、before/after、fresh 和尾部 instruction，
打开该文件就能看到完整消息布局及各段出现条件。不再设置 shared 布局入口，
system 也不另列文件；允许各入口保留少量相同布局，以便独立阅读和调整。
只复用有明确语义的正文片段，例如 review 的取材说明、boundary 和能力清单。
已有 prose 文件继续负责具体措辞；编辑正文不需要修改 Python 的类型目录。
runtime/ 的提示词是 loop 写入 memory/工具结果的运行期事实，不经过此处的消息装饰。
layouts/ 放资源清单、角色、skill 指令和子任务列表的排版；Python 仅准备结构化数据。
这些布局目前沿用正式运行路径的英文，不增加新的语言选择配置。

## 合并后的编辑入口

- `entries/observe.*.jinja`：统一排列角色、总结原则、共享上下文，再按 mode 选择尾部要求。
- `review/context.*.jinja`：共用的任务锚、边界、取材范围及最终产出证据，不插入角色或提交要求。
- `review/judge.*.jinja` / `review/recap.*.jinja`：仅维护判决 / 过程报告的追加要求，不再自行引用 context。
- `review/boundary.*.jinja`：在同一文件里按 `boundary_kind` 对照所有结束边界。
  边界值仍各自独立；loop 负责把未知值回退到 normal。
- `act/resume.*.jinja`：正常续跑与 fallback、具名与无名版本放在一起。
  `ref=None` 表示无名；`full_context` 决定是否使用完整快照说明；
  `completed_tasks` 控制已完成子任务提醒。原有分支措辞不变。
- `common/reply_language.*.jinja`：所有入口共用的回复语言提醒，不再放在 act 目录。
- `compact/prompt/task` 与 `agent` 继续分别说明各自覆盖范围，不合并成泛化的总结 prompt。

runtime 不再一句一个文件，按事件分为七组；组内每个具名 macro 对应一个提示：

| 文件 | 使用位置 / 事件 |
| --- | --- |
| runtime/tools | gateway、act、observing 的工具错误与输出截断 |
| runtime/human | 人工交互、授权、无人值守与空人工回复 |
| runtime/tasks | 派发、结束、取消、阈值终止、复核关闭挂起任务 |
| runtime/history | finish_pair 的最终答复、过程复述与缺省占位 |
| runtime/recovery | 工具副作用未确认，禁止未经授权的重试 |
| runtime/conversation | observer 空回复、用户修改请求的说明 |
| runtime/compaction | task / agent 压缩缺省占位 |

例如在 `runtime/tools.en.jinja` 编辑 `unknown_tool(tool)`；它与 `no_output()`
在同一文件但仍是不同事件。加载器从 macro 定义自动发现名称与参数，不维护另一张路由表。
macro 名称必须全局唯一，中英文名称、参数和默认参数数量必须一致。
`final_reply_note(ref=none)` 在未提供 ref 时使用无名版本。

## 集中范围与 runtime 调用

只集中框架拥有的 prompt。所有 provider 自有内容（身份 facet、工具及参数描述、
工具结果、分页/恢复说明等）继续由 provider 或宿主维护，包括 core/capabilities、
core/media 中的内置 provider。layouts 只给这些数据加框架，不改写其正文。
解析器要求逐字匹配的协议常量仍留在协议/解析层。

loop 在事件发生处显式渲染运行期文案，再按原有路径写入 memory 或工具结果：

```python
from ctx_weft.core.assembler.prompts import runtime_prompt

text = runtime_prompt("unknown_tool", tool=tool_name)
```

runtime 也使用真正的 Jinja：注释、条件和 include 均可用；缺变量或拼错参数会报错。
组内或跨组复用使用普通 Jinja macro 调用；macro 定义外的文字不作为事件正文输出。
不再用字符串 replace/format 替换占位符，传入正文里的花括号不会二次执行。
工具结果中的图片仍作为结构化 part 保留，不交给模板转成纯文本。

## 通用模板契约

入口必须定义四个普通 Jinja macro（不是自定义 Jinja 扩展）：

- system(ctx)：system 字符串。
- before(message, slots, ctx)：指定消息正文前的文字。
- after(message, slots, ctx)：指定消息正文后的文字。
- instruction(ctx)：本次请求的尾部指令。

本项目的四类 entry 均直接定义这四个 macro，不通过另一个入口转发。
通用渲染器仍支持标准 Jinja macro 别名，并校验导出的类型与参数，供自定义模板使用。
现有入口顶层只负责定义与导入；自定义入口若在顶层读取 ctx，则在实际渲染时校验导出。

message 仅包含只读 ref、role；slots 是该消息绑定的槽位集合；ctx 是本次渲染数据。
正文仍是结构化 LLMMessage，不传给模板展开，也不渲染成 JSON 后再解析回来。
图片、tool_calls、tool_call_id 和 reasoning_content 不因文字装饰而丢失。

例如，一个完全不依赖 task 或 memory 的模板：

```jinja
{% macro system(ctx) %}{{ ctx.persona }}{% endmacro %}
{% macro before(message, slots, ctx) -%}
{% if "question" in slots %}Question:
{% endif %}
{%- endmacro %}
{% macro after(message, slots, ctx) -%}
{% if "question" in slots and ctx.constraints %}

{{ ctx.constraints }}
{% endif %}
{%- endmacro %}
{% macro instruction(ctx) %}{{ ctx.instruction }}{% endmacro %}
```

同一条消息可以绑定多个槽位；输出顺序由一个 before/after macro 中的顺序决定，
不由 Python 字典或一串 patch 的注册顺序决定。槽位名由模板与调用方约定，
通用编辑器没有 task_opening、resource_host 等内建定位概念。

已有入口另外提供 fresh、resume、guidance、continuation 等普通辅助 macro，
它们属于当前 loop 适配约定，不是通用模板必需字段。

## 消息定位与编辑

PromptDocument 复制输入消息，为每个消息出现位置分配唯一 MessageRef。
引用在一次编译中稳定，插入消息不会改变已有引用；不把可变数组下标当编辑目标。
它不是跨请求、跨压缩的持久 memory ID。

```python
document = PromptDocument(messages)
question = document.refs[0]  # 由调用方选择；编辑器不猜测业务意义
document.decorate(
    entry,
    bindings={"question": (question,)},
    context=context,
)
document.set_system(entry.system(context))
document.append(
    LLMMessage(role="user", content=entry.instruction(context)),
    merge_with_last=True,
)
```

编辑操作包括 wrap、insert_before、insert_after、append。
未知引用报错，重复初始引用报错；模板缺变量使用 StrictUndefined 报错。
decorate 先渲染全部补丁，再应用，避免渲染异常留下半份修改。
append 默认不合并；当前 loop 显式选择末尾 user 合并，保持原消息边界。
空尾部指令不会产生空消息。输入字符串中的 Jinja 符号不会二次执行。

## 运行路径与职责

1. Sources 收集 ContextBlock；原预算策略保持不变。
2. request_policy.py 根据 purpose 与匹配的 intent 选入口、facet、工具过滤和图片策略。
   这是 loop 的运行策略，不是一套新的 prompt 模板描述语言。
3. loop_adapter.py 重建历史，并把现有 task/memory 定位规则转换为 MessageRef 绑定。
4. composer.py 通过入口渲染 system、装饰历史、追加 instruction，生成 PromptPlan。
5. emit 应用已完成的计划并按实际发送内容计数；非 act 的图片在计数前降级。
   ContextAssembler 仍安装 live tools resolver，Composer 不缓存工具数组。

适配层保持原有落点规则：当前任务首条 user_prompt 绑定 context，
最新一条绑定 input；不同时首条另绑 opening。
资源和 skill 指令的落点、旧记录缺 task_id 时的回退也保持原样。
这些槽位是本组模板的约定，换一种 loop 可以完全换一套绑定。

Prepare 在清除一次性 next_step_hint 前保存 guidance/resume 数据快照。
GuidanceSource 在 budget 前渲染 guidance，因此它继续参与优先级裁剪与 token 估算；
同一次 Prepare 中压缩后重装配复用快照，下一次 Prepare 不再携带已消费的 hint。
旧 extra["act_guidance"]、extra["act_resume_cue"] 文本输入仍兼容。

## 预览与回归

```python
preview = await DefaultComposer().preview(blocks, request)
```

预览使用真实 plan/emit 路径，返回最终 system、每条完整消息及引用、
绑定/插入/合并轨迹和 token_count；不调用模型或工具 provider。
输入是已收集的 blocks，不会替你执行 Sources 或预算裁剪。
因此它适合检查“这句话落在哪条消息、为什么带上”，不是单独维护的示例 prompt。

tests/fixtures/prompt_migration.json 在切换编译实现前捕获。
372 个用例覆盖各 purpose、复核边界、fresh/resume/follow-up、
跨 task 历史、缺角色、图片、工具调用和空内容。
回归逐字段精确比较 system、messages、tools 和 token_count（含换行）。
不要用新编译器自动覆盖基准来消除差异。

tests/fixtures/prompt_cleanup.json 另行冻结了收尾前的 runtime、任务结束回复、
派发状态和工具截断输出，test_prompt_cleanup.py 对其做精确比较。
test_connectors_pack.py 扫描 core 中散落的框架文案；provider-owned 内容明确豁免。

tests/fixtures/prompt_asset_merge.json 在片段合并前另行冻结中英文的 352 个输出，
test_prompt_asset_merge.py 将旧场景映射到合并后的入口，逐字比较空值/非空值分支。
这些基准都不应为适配新实现而重新生成。

```powershell
.venv/Scripts/pytest.exe -o addopts= -q tests/unit/test_prompt_migration_baseline.py tests/unit/test_prompt_cleanup.py
.venv/Scripts/pytest.exe -o addopts= -q tests/unit/test_prompt_asset_merge.py
```
