# 图片折叠与回放（image fold / replay）设计

日期：2026-08-20
状态：**已实现（Phase 4，2026-08-27，`5a6f5e3..4867795` 九个提交）**；原文为设计评审稿，
下文四处被实现推翻的表述以「**订正（Phase 4）**」块**就地加注，原文一字不删**
（§2 / §4.1 / §5 / §6.1）。落地全貌见 §13。
上级设计：[多模态整体设计](2026-08-20-multimodal-design.md)（本设计是其 Phase 4）

## 1. 背景与范围

多模态改造后，`ImagePart` 会随用户消息进入 memory 并参与装配。一张图按现行口径约
1600 token（`utils.py:154 _IMAGE_PART_TOKENS`），几张就能把窗口吃穿，因此必须能被
compact 收走；但收走之后模型若需要重看，又必须有取回路径。

本设计覆盖两件事：

- **收起**：预算紧张时把 memory 记录里的 `ImagePart` 就地降级成文本占位；
- **回放**：模型调 `media:get_image(ref)`，图**随工具结果回到对话尾部**。

本设计**不**覆盖（各自独立推进，本设计依赖它们）：

- 入口多模态改造（`SessionStartParams.user_prompt` 等签名放宽、事件/投影序列化）；
- `providers/llm/*` 出网时的 `ContentPart` → wire blocks 转换；
- token 估算的图片感知修复（见 §2，本设计的**硬前置**）。

## 2. 硬前置：token 估算必须图片感知

现状：五处 token 估算都先 `content_to_text` 再数字符，**图片一律算 0**。

| 位置 | 影响 |
|---|---|
| `assembler/sources/_history.py:111` | `ContextBlock.token_estimate` 为 0 → budget 认为没超、一条不裁 → 整份 prompt 打到 provider 400 |
| `assembler/composer.py:400` | 装配期总量估算失真 |
| `loop/steps/compact.py:205` `_active_memory_tokens` | 降级图片测出 `freed_tokens == 0` → `escalating_compact` 的 `est` 不减 → **L0.5 等于白跑**，编排继续升级 |
| `loop/steps/background_observe.py:99` `is_short_segment` | 图片密集段被误判「短段免折」→ 该段 raw 永久保留 |
| `loop/steps/finalize.py:415` `_is_short_leaf` | 图片密集 task 被误判 short |

改法见上级设计 §6.5：**不是**直接套 `estimate_content_tokens`（它带 4 token 的
framing 补偿，对纯文本非恒等，会静默移动裁剪与 compact 阈值），而是在 `utils.py`
新增只补图片的 `image_tokens(content)`，五处改成
`既有的文本计数 + image_tokens(content)`。

按评审决定，图片 token 口径留在 `utils`，不进 media 模块；将来若要按
media_type / 尺寸细化，在 `utils` 内演进。

> **订正（Phase 4 Task 7，2026-08-27）——本节整段已过期，是「已完成」不是「待做」。**
>
> 1. 「五处 token 估算……**图片一律算 0**」描述的是 **Phase 0 之前**的现状。
>    Phase 0 已补上 `utils.image_tokens`，上表五个位置**早已全部修完**；
>    读本节时不要照着表去改代码，那五处现在都是「文本计数 + `image_tokens(content)`」。
> 2. 口径也不再是「一张 1600」。**Phase 3c Task D（裁定 D2）改成按体积估**：
>    `max(1600, byte_size // 128)`（`utils.py::image_tokens` / `_IMAGE_BYTES_PER_TOKEN`）。
>    该函数建模的是**字节压力不是计费 token**——单图真实计费大约封顶在 1600，
>    别拿它算成本。因此本文其余各处出现的「1600 tok/图」应读作「**至少** 1600」。
> 3. 「图片数」仍走 `utils.image_part_count`（`budget.py` 的报错文案据此报数），
>    **不得**对 `image_tokens` 整除反推——按体积估之后它不再是 1600 的整数倍。
>
> 保留原文的理由：它记着「为什么不直接套 `estimate_content_tokens`」这条仍然有效的
> 判断（那个函数带 4 token 的 framing 补偿，对纯文本非恒等，会静默移动裁剪与 compact 阈值）。

## 3. 入口准入：不新增，复用装配期溢出

评审决定：**不做独立的入口准入层**。理由是单条消息内塞过多图片这一情形，
预算驱动的 compact 从定义上够不着——`budget.py:103` 把当前 task 的 user_prompt
pin 成 priority 0，`budget.py:77` 明确「priority-0 地板永不丢」，而 L3
`collapse_task_layer` 里这条恰是锚点、最不会被折。

修完 §2 之后，这种情形会在 prepare 装配期由 `budget.py:85` 抛
`ContextOverflowError`（`errors.py:76`，`retriable=False`），经
`runtime.py:1773` 使 task 落 SUSPENDED、错误文案随 `TASK_SUSPENDED.error_message`
抵达 host。行为正确，无需新机制。

**唯一改动**：`ContextOverflowError` 的默认文案补上图片维度，例如
「当前消息含 N 张图片（约 X tokens）」，让用户知道该删图而不是删字。

## 4. 核心机制

### 4.1 降级（写 memory，持久）

对一条 memory 记录，把 content 里的 `ImagePart` 换成 `TextPart` 占位：

```
[image ab12cd media_type=image/png — dropped to save context;
 call media:get_image("ab12cd") to bring it back]
```

执行方式：`memory.fold([r.id], [新事件])`（`protocols/memory.py:283`，原子
「遗忘 + 补偿」）。新事件的 `timestamp` / `role` / `metadata`（含 `seq_no`）
原样带过去——视图按 `(timestamp, seq_no)` 排序，因此**位置不变**。

不需要新增 `MemoryProvider` 方法。

> **订正（Phase 4 Task 2/Task 7，2026-08-27）——上面那句「位置不变」的论证是错的，
> 结论靠另一条机制才成立。**
>
> **`seq_no` 带不过去。** 它由 provider 在 ingest 时分配（补偿事件是**新** ingest 的），
> 调用方写什么都不算数。真正把位置钉住的是 **`timestamp`**（照抄有效），
> `seq_no` 只在**同一 timestamp 内**决定次序——而补偿记录拿到的是**新的、更大的**
> `seq_no`，于是同刻的几条记录会被重排到组尾。
>
> **而「同刻」不是测试假象，是生产里的显式不变量。** `finalize.py:155` 的
> `ts = _as_utc(task.started_at or …)` 同时给派发框（assistant）与其 result（tool），
> 注释明写「**框与 result 同锚、严格相邻**」。指望
> `reorder_tool_results_after_calls` 兜底，等于把正确性转嫁给 legalize 层。
>
> **实现取的办法**（`core/media/policy.py::plan_demotions`）：把**同刻 tie 组自第一条
> 被降者起到组尾整段**放进同一次 `fold()`，新 `seq_no` 依原序递增 → 组内有序，
> 组整体位置由 `timestamp` 钉住、不后移。代价是组内本不需降级的记录被原样重写一遍
> （只换 record id）。
> 不选「跳过会乱序的记录」：那会让 L0.5 在 timestamp 精度粗的 provider 上**静默全失效**。
>
> **另外两条实现结论（原文未预见）：**
>
> - **补偿事件必须 `id=None`。** record-id 契约是「已存在的 id（**含已 superseded**）
>   = no-op」；照抄原 id → 原记录被标 superseded、补偿被当重放丢弃 →
>   **整条记录连同图片一起消失，返回值却完全正常**。
> - **因此降级会换掉 record id。** 任何「在降级**之前**采集 id、降级**之后**拿它去
>   `fold`」的调用方都会**静默失效**（一条都 supersede 不掉，同一段对话在视图里出现
>   两次）——这正是 §6.1 两处必须在 `demote_all` 之后**重新 `load_view`** 的原因。
> - **`causation_id` 会丢**：`MemoryRecord` 上没有这个字段，重建时无从照抄（P4-L3）。
>
> **未做的更好修法（P4-L2，提出未实施）**：`fold` 的 replacement 在 1:1 对应时应能
> 继承被 supersede 记录的 `seq_no`，从根上消除同刻重排。当前 `supersede_ids` 与
> `replacements` 是两个无配对关系的列表，改它属**协议级**改动。tie 组整批重写是
> 框架侧的等效兜底。

### 4.2 取回：图放在 tool result 里

`media:get_image(ref)` 的返回值是一个 `ContentPart` 列表：

```python
[
    TextPart("Restored image ab12cd (image/png), originally attached to "
             "your 2nd message in this task."),
    ImagePart(data=<ref>, media_type="image/png", source_type="ref"),
]
```

文本部分带**位置信息**（该 ref 的占位原本在本 task 第几条 user 回合），因为图现在
出现在对话**尾部**而非原位，模型需要知道它对应的是哪一条消息。

这条 tool result 经 `_record_result`（`capability_gateway.py:386`）作为普通
`TOOL_RESULT` 记录落库，content 里带着 `ImagePart`。于是：

- **跨重启天然成立** —— 它就是一条普通对话记录，不需要任何额外状态；
- **下一轮装配天然包含** —— 走既有 history 路径，不需要装配期的还原钩子；
- **不需要 unfold 集合** —— 图已经在对话里了。

#### `InvocationResult.content` 放宽

`InvocationResult.content` 由 `str` 放宽为 `str | list[ContentPart]`
（`capability_gateway.py:86`）。

现有 `_stream_tool` 的流式协议只产出文本块（`result_parts: list[str]`），不改。
非文本部分由 provider 经 `metadata["content_parts"]` 贡献，gateway 组装：

```python
content = "\n".join(result_parts) or ...
content = await self._maybe_spill(content, ...)      # 只作用于文本
if decision.message:
    content = f"[Human note: {decision.message}]\n{content}"
if parts := metadata.get("content_parts"):
    content = [TextPart(content), *parts]
```

落盘截断（`_maybe_spill`）、human note 拼接、事件 payload 的
`content[:8000]`（`capability_gateway.py:382`）全部只作用于**文本部分**，
不必为 parts 分支。

这个 `metadata["content_parts"]` 通道同时是**将来「工具返图」的接缝**（浏览器截图、
图表生成），本期只有 `media:get_image` 使用。

### 4.3 provider 差异在 adapter 层吸收

core 侧统一：`LLMMessage(role="tool", content=[TextPart, ImagePart])`。
两家 provider 的差异全部关在各自 adapter 内。

**Anthropic**（`anthropic.py:356-375`）—— 原生支持。该 adapter 已经把连续 tool
消息聚成一条 `{"role":"user","content":[tool_result...]}`，只需让
`tool_result.content` 从字符串变成 block 列表：

```python
{"type": "tool_result", "tool_use_id": …,
 "content": [{"type": "text", "text": …},
             {"type": "image", "source": {…}}]}
```

**OpenAI**（`openai.py:396-402`）—— `role="tool"` 消息只接受文本。adapter 把 tool
消息里的文本照常发出，**在该组连续 tool 消息之后追加一条 `user` 消息**承载图片：

```python
{"role": "user", "content": [{"type": "image_url",
                              "image_url": {"url": "data:image/png;base64,…"}}]}
```

需要把 `openai.py:376` 的 `for m in messages` 改成与 anthropic 同构的 `while` 分组
循环，才能定位「该组 tool 消息的末尾」——多个 tool call 同批时，追加的 user 消息必须
在**整组之后**，不能夹在两条 tool 消息中间（会触发
`insufficient tool messages following tool_calls message`）。

两个关键性质：

1. **这条 user 消息只存在于 wire payload，不落 memory。** 因此不会制造段边界——
   `segment_fold.py:47`、`finalize.py:466`、`background_observe.py:87` 三处都用
   「最后一条 role=user 回合」划段，若把它落库会打乱段折叠与胶囊范围。
2. **与 `reorder_tool_results_after_calls` 无冲突。** 那一步在 core
   （`llm_gateway.py:198`）执行，adapter 的 `_serialize_messages` 在其之后，
   追加的消息不会再被搬动。

### 4.4 为什么是尾部追加而不是原位还原

**KV cache。** 在历史原位还原图片等于修改 prompt 中部，cache 前缀从那条记录起
全部失效；而被折叠的图往往位置很靠前，实际代价接近整份 prompt 重付。

tool result 追加在尾部是 append-only，前缀不动。这是本设计选择「图随工具结果回来」
而非「装配期就地还原」的决定性理由。

代价是图不在它原本的上下文位置上——由 §4.2 的位置信息文本补偿。

## 5. 取回结果的生命周期

取回的图落在当前段的 `TOOL_RESULT` 记录里，因此：

- 下一个段边界到来时，被 `segment_fold` 折走（段内 raw **不受保护**，
  `segment_fold.py:50` 只保 user 回合与 SUMMARY）；
- 预算紧张时，也会被 L0.5 降级成占位。

也就是说**取回天然是短时的**：够模型看几轮，然后自动回收，不需要任何额外的回收
机制。需要时再取一次即可——占位始终留在原位，ref 一直可见。

这一性质替代了早先方案里的 unfold 状态与其回收逻辑。

> **订正（controller 裁定 R2，Phase 4 Task 4/5，2026-08-27）——「占位始终留在原位，
> ref 一直可见」说得太满，与 §6 自相矛盾；以 §6 为准。**
>
> §6 的「两段衰减」明写：记录被 L1/L3 折进摘要之后「**不再提供取回**」（并说明这是
> 自觉取舍而非缺陷）。那是深思熟虑的一段；§5 这句是随手写松了。
> `media:get_image` 只查**当前 task 视图**，占位所属的记录一旦离开该视图，
> 就取不回来了——实现无误。
>
> **订正的措辞**：把「占位始终留在原位，ref 一直可见」读作
> 「**只要承载占位的记录还在当前 task 视图里**，占位就留在原位、ref 就可见、随时可再取一次」。
>
> **P4-L11 —— 这个衰减段比设计时预想的窄得多（好消息）：**
> §6.1 的前置降级落地后，**L3 折叠不再让 `get_image` 失效**——占位随 `original` 节
> 留在坍缩后的 USER_PROMPT 里，`find_image_placeholders` 照样扫得到，图取得回来
> （已由端到端用例的对照半边钉住）。真正进入衰减段的只有 **L1/L2 那种纯遗忘路径**。
>
> **那条路径经测试确认是诚实的**：模型拿到一条 `TextPart`，逐字含 ref 与
> `is present in this task`，不抛异常、不返回错图；且此时 blob store 里字节其实还在
> ——判据是「**本视图占位里有没有它**」而不是「blob 存不存在」。
> 残留的不诚实只有一处：占位文本写着 `call media:get_image(…) to bring it back`，
> 衰减之后这句邀请是假的，代价是模型浪费一次工具调用。Task 4 的失败文案已把
> 「本 task 里没有这个 ref」（抄错了）与「字节没了」（别再试了）**刻意分成两句**，
> 判为可接受。
>
> **P4-L8（已知、不修）**：位置信息里的「第几条 user 回合」按**当前视图现算**，
> 同一 ref 在折叠前后调两次报出的序号会不同。要稳定就得在降级时把序号写进占位，
> 那会打破占位的**逐字节确定性**（缓存前缀约束），故不做。

## 6. 折叠层级与策略

现有五级折叠中，只有三级会碰到图片：

| 级别 | 实现 | user 回合（图片宿主） |
|---|---|---|
| L0a 段折叠·边界 | `segment_fold.segment_fold` | 受保护（`_protected()`） |
| L0b 段折叠·retry | 同上 | 受保护 |
| L0c 胶囊形成 | `finalize._supersede_final_raw_segment:444` | 受保护 |
| L0d finish 对重写 | `background_observe._replace_finish_report` | 不碰 task 层 |
| L1 胶囊收起 | `compact.fold_root_experience:242` | 整体折走 |
| L2 胶囊降级 | `compact.demote_kept_capsules:367` | 纯遗忘 |
| L3 当前 task 坍缩 | `compact.collapse_task_layer:102` | 折走并重建为纯文本 UP |

**策略：新增 L0.5，插在 L1 之前。**

理由：图片降级无 LLM、单位收益最高（1600 tok/图）、且**可逆**（占位仍在原位，
`media:get_image` 随时可取）。这三条正是「应该最先跑」的层级画像。L1/L2/L3 折的是
记录本身，一旦执行位置就没了。

因此形成一个**两段衰减**，这是自觉取舍而非缺陷：

- **可逆窗口**（记录仍在）：图为占位，ref 可见，随时可取回；
- **衰减之后**（记录被 L1/L3 折进摘要）：ref 只以文本形式活在摘要里，
  已无从判断它原属哪条消息，不再提供取回。

L0.5 跑在最前，意味着绝大多数情况下预算在可逆窗口内就够了，很少走到衰减段。

### 6.1 L1/L3 的残留图片必须先降级再折

L0.5 会保留最近 `keep_recent` 张图不降级，因此当编排继续升级到 L1/L3 时，折叠范围内
**仍可能存在真图**。若不处理，`compact.py:275`（L3 取 `original` 节）与
`summarize_for_compact`（L1/L3 的摘要输入）都走 `content_to_text`，会把这些图**静默
拍扁**——而它们恰是最新的那几张。结果是老图留下了可取回的占位、最新的图反而彻底消失，
优先级完全颠倒。

因此 `fold_root_experience` 与 `collapse_task_layer` 在动手之前，各自对**自己的折叠
范围**无条件调一次降级（不受 `keep_recent` 保护）：

```python
await media.demote_all(memory, [r.id for r in fold], ctx)
```

这样图统一变成文本占位，ref 随 `original` 节 / 摘要文本一起留存，衰减是「位置丢失」
而非「痕迹全无」。

L2 `demote_kept_capsules` 是纯遗忘、不产摘要，无处承载 ref，因此**不加**这一步：
其范围内的残留图片直接随记录消失（blob 本体仍在，只是不再可达）。这是可接受的——
L2 折的是已结束的历史胶囊。

除这一处前置调用外，L1/L2/L3 的折叠逻辑本身不改。

> **订正（P4-L10，Task 5 发现 / Task 5b 修复，2026-08-27，commit `c90398a`）——
> 「除这一处前置调用外不改」对 L1 不成立：只加前置调用，§6.1 对 L1 完全无效。**
>
> **顺序错在调用方而不在 `fold_root_experience` 内部：**
>
> ```
> compact.py  summary_agent = await summarize_for_compact(...)   ← 摘要先算
> compact.py  fold_root_experience(..., summary_agent)           ← demote_all 在其内部，之后才跑
> ```
>
> 摘要是按**真图**算的，而 `content_to_text([TextPart('看这张'), ImagePart(...)])`
> → `'看这张'`，图**无痕消失、连占位都没有**。后果正是本节开头写的「优先级完全颠倒」：
> 被 `keep_recent` 保住的那几张**最新的**图先被拍扁进摘要（ref 彻底没了），
> 然后记录被 supersede；老图反倒留下了可取回的占位。
> **L3 无此问题**——`original` 节是降级**之后**逐字取的。
>
> **修法（Task 5b）**：把 `fold_root_experience` 的 `summary_text` 参数放宽成
> `str | Callable[[], Awaitable[str]]`，摘要**在前置降级 + 重新 `load_view` 之后**
> 才求值；`escalating_compact` 的 L1 改传
> `lambda: summarize_for_compact(state, ctx, scope="agent")`。
> L1 的新顺序是：`load_view → 算折区 ids → demote_all×2 → 重新 load_view + 重算 ids
> → **求值摘要** → fold(ids, [SUMMARY])`。
>
> **为什么不是「把降级提到 `_apply` 之外」**：折区 `ids` 只有 `fold_root_experience`
> 内部算得出（`_expand` 沿 parent 链 + `_collect` 跨 TASK/AGENT 两个视图），降级依赖
> ids，**搬不出去**；能动的只有摘要。推迟摘要之后 L1 从 `_apply` 视角**仍是原子的
> 一步**，`before` 继续复用上一级的 `after`，降级省下的 token 自然落进 L1 的 `freed`。
> 反方案实测：把降级挪到 `_apply` 之外 + 让 `last_tokens` 失效，会让降级那段
> **从账上彻底消失**（变异实测报 `freed_tokens=61`，真实下降 `3208-65=3143`）。
>
> **`fold_root_experience` 内部原有的 `demote_all` + 重新 `load_view` 位置一行不动**
> ——提前的是摘要不是降级，且重新 `load_view` 是 record-id 契约的硬要求（见 §4.1 订正）。
>
> **P4-L13（口径变化，无当前受害者）**：摘要改惰性求值后，`fold_root_experience` 的
> 两条早退路径（`len(top) <= keep_last` / `not ids`）**不再触发那次摘要 LLM 调用**。
> 生产上是纯收益（省一次注定被丢弃的调用），但**按 LLM 调用次序编排的 mock 会错位**
> （本仓有 `_RouterLLM` 这类）。全量回归无一条因此转红，但那是「两处 guard 同进同退」
> 的巧合级安全，不是保证。

## 7. 模块结构

```
core/media/
  __init__.py     — 只导出 §8 的三个函数，其余私有
  refs.py         — 占位文本编解码 + blob ref 格式（唯一知道占位长什么样的地方）
  policy.py       — 哪些该降、保留几张（纯函数，无 IO）
  fold.py         — 降级执行：读记录 → 重写 content → memory.fold()
  capability.py   — MediaCapabilityProvider（core 侧，提供 media:get_image）
```

`capability.py` 做成 core 侧 provider 而非放进 `FilesystemToolsProvider`，
是因为工具体要读 task 视图（定位 ref 的占位在第几条 user 回合，生成位置信息），
而 capability provider 拿不到 `MemoryProvider`。仓库既有先例是
`ControlCapabilityProvider`（`control_capability.py:617`，core 侧、构造时注入依赖）。

**边界**：占位格式、blob ref 交互、位置描述——全在墙内。`_history.py` 不解析占位，
`composer` 不感知取回，`reducers` / `control/types` 一个字不改。

**明确不属于本模块**：

- `BlobStore` 协议 —— 归 `protocols/filesystem.py`（与 `SpillSink` 同处，形状一致：
  「core 不直接碰存储，只知道有个 sink」）；本模块只是消费者；
- ref → base64 的 rehydrate —— 归 `providers/llm/*`（出网前最后一步）；
- 入口 base64 → ref 外部化 —— 归内容归一层；
- 图片 token 口径 —— 归 `utils`。

相比早先方案，`unfold.py` 与 `render.py` 两个文件消失——图随 tool result 回到对话
里，不再需要 unfold 状态与装配期还原。

## 8. 对外 API

```python
async def demote_for_budget(memory, scope, ctx, *, keep_recent: int) -> int:
    """L0.5 调用。降级 scope 内除最近 keep_recent 张之外的所有图片。
    返回降级的图片张数。"""

async def demote_all(memory, record_ids: list[str], ctx) -> int:
    """L1/L3 折叠前调用（见 §6.1）。对指定记录无条件降级，不受 keep_recent 保护。
    返回降级的图片张数。"""

async def get_image(memory, scope, ctx, ref: str) -> list[ContentPart]:
    """media:get_image 工具体。返回 [TextPart(位置信息), ImagePart(ref)]。
    ref 不存在于本 scope 的任何占位中时，返回单条说明性 TextPart（不抛）。"""
```

> **补记（Phase 4 落地，2026-08-27）——签名与上表有出入，以代码为准：**
>
> ```python
> async def demote_for_budget(memory, address, ctx, *, keep_recent,
>                             scope=MemoryScope.TASK, kinds=None) -> int
> async def demote_all(memory, record_ids, ctx, *, address,
>                      scope=MemoryScope.TASK, kinds=None) -> int
> ```
>
> - 原文的位置参数 `scope` 指的是**坐标**；v2 已把坐标改名 `address`，
>   `scope` 让位给归属范围枚举（`MemoryScope`）。位置参数顺序不变。
> - `demote_all` 多了**必填关键字** `address`：`MemoryProvider` 没有「按 id 取记录」
>   的读接口，且同刻 tie 组的顺序保护必须看**整个视图**（折区边界很可能正好切在一个
>   同刻组中间）。故它照样 `load_view` 全量视图，只把降级范围限定在 `record_ids` 内。
> - `get_image` 落在 `core/media/capability.py`，取回逻辑按**列表**写
>   （`MAX_REFS_PER_CALL = 1` 只钉在常量与 schema 上），放宽 §12 不必动逻辑。

## 9. 调用点

| 调用方 | 改动 |
|---|---|
| `compact.escalating_compact:466`（L1 之前） | 插一级 `_apply(media.demote_for_budget(...))` + 一条 `MEMORY_COMPACTED` 事件（`source="demote_images"`），与现有各级同构 |
| `compact.fold_root_experience:242` / `collapse_task_layer:102` | 各自在 `fold()` / 取 `original` 节之前调一次 `media.demote_all(...)`（见 §6.1） |
| `ProviderRegistry` / `CtxWeftRuntime.__init__` | 注册 `MediaCapabilityProvider`（同 `ControlCapabilityProvider`，`runtime.py:423`） |
| `capability_gateway.py:86,229-241` | `InvocationResult.content` 放宽；组装 `metadata["content_parts"]`（见 §4.2） |
| `providers/llm/anthropic.py:356-375` | `tool_result.content` 支持 block 列表 |
| `providers/llm/openai.py:376-402` | 改分组循环；组尾追加承载图片的 user 消息（见 §4.3） |

装配侧（`agent_recall.py` / `_history.py` / `composer.py`）**无本设计专属改动**——
它们只需完成上级设计 §6.4 的通用「保 parts」改造。

> **订正/补记（Phase 4 Task 7）——上表最后两行在 Phase 4 开工前就已经做掉了：**
>
> | 行 | 实况 |
> |---|---|
> | `anthropic.py` `tool_result.content` 收 block 列表 | ✅ 早已走 `_parts_to_blocks` |
> | `openai.py` 分组循环 + 组尾追加 user 消息 | ✅ **Phase 3c Task B**（`76c86cd`） |
> | 其余四行 | ✅ Phase 4（`5a6f5e3..4867795`） |
>
> **`MediaCapabilityProvider` 的注册是无条件的**（`runtime.py`，紧跟
> `SkillExecutorCapabilityProvider`），故 `media:get_image` 对**每个 agent** 全局可见，
> 与 `control` / `skill_executor` 同口径。全量回归无一条既有用例因工具面多一项而红，
> 但**宿主若有按工具面快照断言的外部测试会受影响**（P4-L9）。
> 它持 `ProviderRegistry`、**调用时**才解析 memory / blob store——宿主常常先建 runtime
> 再 `register_memory`，构造期取一次会把接线顺序变成隐性约束。

## 10. 错误处理

| 情形 | 行为 |
|---|---|
| `BlobStore` 未注册 | `demote_for_budget` / `demote_all` 返回 0（不降级）。行为与改造前完全一致 |
| `get_image` 的 ref 不存在于本 scope | 返回单条说明性 `TextPart`，不抛、不返回 `ImagePart` |
| adapter 出网时 `blob.get()` 返回 None | 降级成 `[image unavailable]` 文本 block，不抛（上级设计 §6.6） |
| 降级过程中 `fold()` 失败 | 记 warning，跳过该条继续；本级 `freed_tokens` 相应减少，编排自然升级到 L1 |

## 11. 测试

- `policy.py` 纯函数：给定记录列表 + `keep_recent`，断言选中集合（无 IO，快）
- `refs.py` 编解码往返
- 降级：`fold()` 后位置不变（`(timestamp, seq_no)` 与降级前一致）
- 取回：tool result 含 `ImagePart`；落库后下一轮装配里该图出现在尾部
- 取回文本含正确的「第几条 user 回合」
- Anthropic adapter：`tool_result.content` 为 block 列表
- OpenAI adapter：同批多个 tool call 时，追加的 user 消息在**整组之后**；
  且该消息不出现在任何 memory 记录里
- 生命周期：段边界之后取回的图被折走；再次取回可用
- 回归：未注册 `BlobStore` 时全链路行为不变；纯文本工具结果的 wire 形态逐字节不变

## 12. 未决参数

- `keep_recent` 默认值：暂定 **2**（最近两张图保原样）。需实测校准。
- 占位文本的确切措辞：影响模型是否会主动调 `get_image`，需实测。
- `get_image` 是否允许一次取多个 ref：暂定单个，避免一次调用把窗口打满。

> **落地（Phase 4，2026-08-27）——三条都按暂定值执行并做成了可配置，仍待实测校准：**
>
> | 参数 | 落点 | 现值 |
> |---|---|---|
> | `keep_recent` | `LoopConfig.compact_keep_recent_images` | `2`（按**图片张数**数，一条记录可只降一部分） |
> | 占位措辞 | `core/media/refs.py::IMAGE_PLACEHOLDER_TEMPLATE` | 见 §4.1 订正；**与工具描述同源** |
> | 多 ref | `capability.py::MAX_REFS_PER_CALL` | `1` |
>
> `keep_recent` **按图片张数而不是按记录**：整条保护会让一条 20 图的消息在
> `keep_recent=2` 下把 20 张全扣住，而「一次贴一叠图」恰恰最需要 L0.5。
> 不可降的 inline base64 图**照样占名额**——这个名额保的是「模型还看得见的最近 N 张」。
>
> **占位措辞与工具描述必须同源**：占位是 `get_image` 的**唯一召唤入口**，
> `dropped to save context` 与 `call media:get_image("blob:<sha>") to bring it back`
> 原样出现在工具描述里；两边换一套说法，模型就认不出「这个工具就是刚才那句话让我调的」。
> 位置信息里「第几条 user 回合」的口径（**从 1 数、只数 `role=="user"` 的记录**）
> 也写进了工具描述，否则模型只能猜。

---

## 13. Phase 4 落地实录（2026-08-27，`5a6f5e3..4867795`）

九个提交：`e5ab6a5` refs / `2f1f6cf` docstring 订正 / `6dc40aa` policy+fold /
`d1f0ea5` gateway 放宽 / `64f23c0` 修 flake / `dda0c4f` capability /
`08b3eaf` 编排接入 / `c90398a` L1 顺序修复 / `4867795` 端到端。
全量 `2088 total / 2080 passed / 3 failed / 5 skipped`（3 条既有环境性失败）。

### 13.1 三条本文原先没写、但后来者必须知道的实现结论

**（a）两族占位并存、互不替代（裁定 R1）。** 本仓有两种「图变文本」，长得像、职责完全不同：

| | 谁做 | 落库？ | 占位 | 可回读？ |
|---|---|---|---|---|
| **per-purpose 降级**（Phase 3c） | `content.py::downgrade_images_to_text`（composer 路径） | **否**，只影响本次 prompt | `[image {media_type}]` | 否，单向渲染 |
| **L0.5 降级**（本设计） | `core/media/fold.py` | **是**，重写 memory 记录 | 含完整 ref | **是**，`get_image` 解得回来 |

per-purpose 是「这次不发」，L0.5 是「从记忆里收起来」。**不要合并。**
全仓共四种占位（另两种是 `content.py` 的 `[image unavailable: …]` 与 `openai.py` 的
`[image see the following message]`），**只有 L0.5 那种有解析语义**，
故 `core/media/refs.py` 是它的唯一归口，**其模块 docstring 就是占位清单**；
另外三种保持在原处（归一层 / wire 层各归其位，不把 wire 格式知识拖进 media 模块），
各加一行注释指向清单。三种非 L0.5 占位经实测**都解不出 ref**（`decode` 返回 `None`）。

**（b）`_media_enabled(ctx)` 总闸**（`compact.py`，本文原文没有）。判据是
`ctx.blob_store is not None and blob_store.can_externalize`。不加的话，未接 BlobStore 的
纯文本宿主每次 compact 都要为 L0.5 白跑 `load_view` 与两次 `_active_memory_tokens`，
§6.1 两处还各多一次全量 `load_view`——§10 的「行为与改造前完全一致」就不再是**逐字节**的了。
它**不是**「哪些 part 该降」的第二处判据（那仍然只由 `policy.demotable_ref` 的
`source_type == "ref"` 决定）：本闸只能让降级**少做**、不能让它多做。
代价是一个边角：曾接过 blob store、现已摘掉的宿主，存量 ref 图不再被降级
（那些图本来也 rehydrate 不回来了）。

**（c）`metadata["content_parts"]` 通道是通用的，不认发布者。** key 提成
`capability_gateway.CONTENT_PARTS_KEY` 供任意 provider 导入；本期只有 `media:get_image`
在用，浏览器截图 / 图表生成将来走同一条路。两处**加固**（本文原文未要求）：
① 守卫 `isinstance(parts, (list, tuple)) and parts`——宿主误给字符串时
`[TextPart(t), *"ab"]` 会**静默**把裸 `str` 塞进 content；
② 过 `normalize_content_parts`——JSON 往返的 provider 给 dict 形态 part，不归一会在
adapter 被**静默丢掉**。

**（d）放宽 `content` 的连带修法：读取方必须逐个枚举。** 实测抓到两个**都不报错、
都静默**的缺口：`_record_result` 的事件 payload 对 list 做 `content[:8000]`
（切的是前 8000 个 **part**，且 base64 随 repr 泄漏进事件库）→ 改走
`redact_content_for_event`；`background_observe.py` 的 `act_recap` 对 list 做 `.strip()`
→ AttributeError 被 `_run_background_observe` 整段 `except Exception` 吞掉，症状是
**段摘要静默丢失、段保 raw** → 改走 `content_to_text`。
连带教训：「读取方在 list 形态下行为正确」若写成「不抛异常」，**在吞异常的函数里恒成立**。

### 13.2 遗留逐条处置（P4-L1 ~ P4-L14）

| # | 内容 | 处置 |
|---|---|---|
| L1 | 占位含全角破折号 `—` 与半角引号 | **明确不做 / 移交宿主**：两家 adapter 都不做 NFC/NFKC 或智能引号替换，今日无受害者。**接新 provider 时须检查这一条**——若它对 prompt 做归一，占位会解不回来 |
| L2 | `fold` 的 replacement 应能继承被 supersede 记录的 `seq_no` | **移交后续（协议级）**：见 §4.1 订正。tie 组整批重写是等效兜底，代价是无谓重写与换 id，无正确性缺口 |
| L3 | L0.5 重建丢 `causation_id` | **移交后续（低优先）**：`MemoryRecord` 上没有该字段，无从照抄；要修得先给协议加字段。已写进 `fold.py` docstring，以免日后当成 provider 的 bug 去查 |
| L4 | 带 `topic` 的记录被降级会推进 `topic_seq`，订阅方重收一次（图已变占位） | **已闭合（设计取舍）**：照抄 `topic` 而非丢弃——丢了黑板读取就断了，重收是良性的 |
| L5 | `test_task_recap_markers::test_started_and_done_emitted_on_success` **走异常路径通过**（`fake_state_ctx` 缺 `loop_config.max_turns_per_observe`，`AttributeError` 被吞，只剩 `finally` 发 DONE） | **移交后续（建议单开任务）**：它自称验「成功路径发两个事件」，实际只验了「异常路径也发」。「fixture 缺字段 + 函数吞异常」的组合会让**一整片**测试变成半永真，须连带复核依赖该 fixture 的用例 |
| L6 | `InvocationResult.metadata` 里留着 `ContentPart`（按原文用 `.get` 不 pop） | **明确不做（记为契约警示）**：当前全仓无人序列化该 metadata。若日后有人把它塞进事件或日志，base64 会从这条侧路泄漏 |
| L7 | `get_image` 只查当前 task 视图，跨层之后取不回 | **已闭合**：裁定 R2（以 §6 为准，实现无误）+ P4-L11（影响面窄）。见 §5 订正 |
| L8 | 位置序号按当前视图现算，折叠前后不同 | **明确不做**：要稳定就得把序号写进占位，与占位的**逐字节确定性**（缓存前缀约束）冲突。见 §5 订正 |
| L9 | `media:get_image` 对每个 agent 无条件可见 | **已闭合（符合 §9）/ 移交宿主**：宿主若有按工具面快照断言的外部测试需更新。见 §9 补记 |
| L10 | §6.1 对 L1 因摘要早于降级而无效 | **已闭合**：Task 5b（`c90398a`）。见 §6.1 订正 |
| L11 | 裁定 R2 的衰减段比预想窄（L3 折叠不再让 `get_image` 失效） | **已闭合（结论，非缺陷）**：见 §5 订正 |
| L12 | 🔴 `tests/integration/test_dispatch_boundary_recap_e2e.py` 间歇失败，**可能盖着一个真的折叠缺口** | **移交后续（本 Phase 最要紧的一条，见 §13.3）** |
| L13 | 惰性摘要使 `fold_root_experience` 的两条早退路径少一次 LLM 调用 | **已闭合（口径变化）/ 移交宿主**：生产上是纯收益，但按 LLM 调用次序编排的 mock 会错位。见 §6.1 订正 |
| L14 | 端到端用例的 compact 触发依赖 `context_limit=4300` 这个**手工卡出来的窗口** | **移交后续（低优先）**：`_IMAGE_PART_TOKENS` / `default_output_reserve` / composer 固定开销一变，它会滑出窗口。届时**是红不是假绿**，但排障的人得先知道根因在这个常数上。已写进该文件的注释 |

### 13.3 🔴 P4-L12 —— 留给专门任务，附完整诊断

**「全量负载下超时」的假说是错的。** 抓到的失败原文：

```
AssertionError: 派发前 raw 应已被折掉/胶囊化，实得 ['done']
残留记录：seq_no=4, output_tokens=692, content='done', type=LLM_RESPONSE
```

第 2 条断言（「必须有 dispatch 段摘要」）**是通过的**——折叠确实发生了，
只是 root 在子任务返回后那次收尾 act 的 raw 没被折掉。`output_tokens=692`
说明这是一次**真实的 act 应答**，不是 mock 产物。

**机制查明**：测试里的 `_RouterLLM` 按 `request.tools` 路由，最后一个分支是「兜底即 act」。
而 `summarize_for_compact` 发的是 `tools=[]`（Phase 3c 起 purpose→tools 明确 compact = `[]`），
三个具名分支都不匹配 → **掉进 act 分支** → `_act_calls += 1` 且被应答成
`text="done"` + `finish_task`。Task 5b 把摘要改成惰性求值后调用次数变了，后果就浮出来。

**试修并已回退**：给空工具面加独立分支（语义上正确——摘要期望纯文本），该测试
**由间歇失败变成确定失败**。这说明**它此前是因为错误的原因才通过的**——
摘要调用误占 act 计数，恰好把状态推到了 raw 被折掉的那条路上。
controller 已回退（根因未定时不改产品代码，也不把仓库留在确定失败状态）。

**处置建议**：① 先给 `_RouterLLM` 的空工具面加独立分支，让失败**稳定复现**；
② 再判断底下那个折叠缺口是**产品问题**（root 收尾 act 的 raw 确实该被折而没折）
还是**测试断言过宽**（`raws == []` 取的是 scope 内**全部** `LLM_RESPONSE`，
而断言名字说的是「派发**前**」的 raw）。**不要盲改超时**。

### 13.4 四条不变量的收口确认

1. **不接 BlobStore 时行为不变（§10）**——L0.5 与 §6.1 三处调用点**全部**被
   `_media_enabled(ctx)` 短路，连一次多余的 memory 读都不发；即便闸被绕开，
   `policy.demotable_ref` 的 `source_type == "ref"` 判据也让选中集为空、`fold()` 一次不发。
   **两条已知例外**（都不由 BlobStore 门控，如实记此）：`media:get_image` 无条件出现在
   工具面（P4-L9）；`openai.py::_TOOL_IMAGE_NOTICE` 的文案由中文改英文（L6 收口，
   wire-only，且不接 BlobStore 时工具结果不会带图，实际不可达）。
2. **纯文本行为逐字节不变**——`redact_content_for_event(str)` 与 `content_to_text(str)`
   对 `str` 输入**返回同一个对象**（实测 `is` 为真）；gateway 的 parts 拼接在
   spill / human note **之后**，无 `content_parts` 时 `content` 仍是同一个 `str`。
3. **判据 `not hasattr(p, "text")` 未解冻**（裁定 D1）——`composer.py` / `utils.py`
   `image_part_count` / `content.py` `_is_text_part` 三处逐字节未变；
   新模块（`media/policy.py` `is_image_part`、`media/capability.py`）**复用**同一判据，
   不新造第二份真源。
4. **两族占位互不污染**——见 §13.1(a)。
