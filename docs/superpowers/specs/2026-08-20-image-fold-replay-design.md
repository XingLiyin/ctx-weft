# 图片折叠与回放（image fold / replay）设计

日期：2026-08-20
状态：设计已评审，待实现
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
