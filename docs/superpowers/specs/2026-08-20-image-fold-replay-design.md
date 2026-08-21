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
- **回放**：模型主动调工具后，装配期把占位在**原位**还原成真图。

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

全部改为 `utils.estimate_content_tokens`（`utils.py:168`，已存在且已被
`prepare.py:74,95` / `llm_gateway.py:281` 正确使用）。

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

三个动作，只有第一个写 memory：

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

### 4.2 取回（写一条 unfold 记录）

`media:get_image(ref)` 工具做两件事：

1. 写一条 unfold 标记记录（见 §5）；
2. 返回带**位置信息**的回执文本，例如：
   `Restored image ab12cd (image/png), originally attached to your 2nd message
   in this task. It now appears inline at its original position above.`

位置信息由模块从 task 视图推出（该 ref 的占位所在记录是本 task 第几条 user 回合）。

工具**不投递图片本身**。这是刻意的：OpenAI chat completions 的 `role="tool"`
消息只接受文本，塞不进图；而注入一条额外的 `role="user"` 回合会凭空制造段边界
（`segment_fold.py:47`、`finalize.py:466`、`background_observe.py:87` 三处都用
「最后一条 role=user 回合」划段），破坏段折叠与胶囊范围。

### 4.3 还原（渲染期，不写 memory）

装配期在 records → ContextBlock 之前加一趟：读 unfold 集合，把命中的占位
`TextPart` 从 `BlobStore` 取回、换回 `ImagePart`。

这与仓库既有原则一致——`loop/driver.py:168` 明确「呈现态框架由 composer 渲染期
生成，不落库」。unfold 属于同一类。

**取不到必须降级，不得抛**：blob 过期 / 宿主换机 / GC 误删都会发生。取不到时
保持占位并追加 `(unavailable)`，loop 继续。

## 5. unfold 状态的归属

放 `MemoryKind.TOOL_AUDIT` + metadata 约定：

```python
MemoryEvent(
    kind=MemoryKind.TOOL_AUDIT, scope=MemoryScope.TASK,
    address=scope, role="tool", content="",
    metadata={"mech": "image_unfold", "ref": ref},
)
```

依据仓库自己的 memory v2 演进规则（`protocols/memory.py:44,54`）：

> kind: CONVERSATION_TURN | SUMMARY | TOOL_AUDIT | PUBLICATION（封死，永不为新机制扩）
>
> 演进规则：**新框架机制 = 新 metadata 约定，永不铸新 kind**

这个槽位同时满足四件事：

1. **跨重启持久** —— 是 memory 记录，不依赖进程状态；
2. **不污染 prompt** —— 装配侧取 TASK 视图默认 kinds（`CONVERSATION_TURN + SUMMARY`，
   见 `agent_recall.py:59,75`），不含 `TOOL_AUDIT`；
3. **自动回收** —— compact 的 `_TASK_VIEW_KINDS` **含** `TOOL_AUDIT`，所以这条记录
   会随 L2/L3 一起被折走，unfold 随之失效、图折回去。不需要写反向回收逻辑；
4. **零协议改动** —— 不铸新 kind，不动 reducers / control types / converters。

评审时否决的三个替代方案及理由：

- **从 assistant 记录的 `tool_calls` 反推**（`act.py:352`）：能工作且零改动，但依赖
  「tool_calls 持久化 + `non_dispatch_tool_dicts` 过滤规则」这个隐式约定，将来被改会
  无声失效。
- **`InvocationResult.metadata` 透传进 TOOL_RESULT 记录**：需要改 gateway，且把 media
  的知识散进 `capability_gateway`。
- **新增 `IMAGE_UNFOLDED` 事件 + 投影字段**：要动 EventType 白名单、`reducers.py`、
  `control/types.py`、`converters.py` 四处，且**回收对不齐**——事件投影不知道 compact
  折了什么，会出现「投影说该展开、记录早已不在」。

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

理由：图片降级无 LLM、单位收益最高（1600 tok/图）、且**可逆**（§4.1 就地重写，
记录仍在原位）。这三条正是「应该最先跑」的层级画像。L1/L2/L3 折的是记录本身，
一旦执行，位置就没了、不可逆。

因此形成一个**两段衰减**，这是设计的自觉取舍而非缺陷：

- **可逆窗口**（记录仍在）：图为占位，`media:get_image` 可精确还原到原位；
- **衰减之后**（记录被 L1/L3 折进摘要）：ref 只以文本形式活在摘要里，位置丢失，
  不再可还原。

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

这样图统一变成文本占位，ref 随 `original` 节 / 摘要文本一起留存，衰减是"位置丢失"
而非"痕迹全无"。

L2 `demote_kept_capsules` 是纯遗忘、不产摘要，无处承载 ref，因此**不加**这一步：
其范围内的残留图片直接随记录消失（blob 本体仍在，只是不再可达）。这是可接受的——
L2 折的是已结束的历史胶囊。

除这一处前置调用外，L1/L2/L3 的折叠逻辑本身不改。

**滚动窗口（与预算无关地按张数/轮次收图）：评审决定不做。** L0.5 已覆盖其场景，
只是触发晚一点；两套规则同时作用会让「图是被哪条规则收走的」难以判断。

## 7. 模块结构

```
core/media/
  __init__.py     — 只导出 §8 的三个函数，其余私有
  refs.py         — 占位文本编解码 + blob ref 格式（唯一知道占位长什么样的地方）
  policy.py       — 哪些该降、保留几张（纯函数，无 IO）
  fold.py         — 降级执行：读记录 → 重写 content → memory.fold()
  unfold.py       — unfold 记录的读写
  render.py       — 渲染期还原
  capability.py   — MediaCapabilityProvider（core 侧，提供 media:get_image）
```

`capability.py` 做成 core 侧 provider 而非放进 `FilesystemToolsProvider`，
是因为工具体要写 memory，而 capability provider 拿不到 `MemoryProvider`。
仓库既有先例是 `ControlCapabilityProvider`（`control_capability.py:617`，
core 侧、构造时注入依赖）。本 provider 同形。

**边界**：占位格式、blob 交互、unfold 存储、位置描述——四样全在墙内。
`_history.py` 不解析占位，`capability_gateway` 不做透传，`reducers` /
`control/types` 一个字不改。

**明确不属于本模块**：

- `BlobStore` 协议 —— 归 `protocols/filesystem.py`（与 `SpillSink` 同处，形状一致：
  「core 不直接碰存储，只知道有个 sink」）；本模块只是消费者；
- 入口 base64 → ref 外部化 —— 归内容归一层；
- adapter 出网 rehydrate —— 归 `providers/llm/*`；
- 图片 token 口径 —— 归 `utils`（评审决定）。

## 8. 对外 API

```python
async def demote_for_budget(memory, scope, ctx, *, keep_recent: int) -> int:
    """L0.5 调用。降级 scope 内除最近 keep_recent 张之外的所有图片。
    返回降级的图片张数。"""

async def mark_unfolded(memory, scope, ctx, ref: str) -> str:
    """media:get_image 工具体。写 unfold 记录，返回带位置信息的回执文本。
    ref 不存在于本 scope 的任何占位中时，返回说明性文本（不抛）。"""

async def rehydrate(records, memory, blob, ctx) -> list[MemoryRecord]:
    """装配期调用。读 unfold 集合，把命中的占位换回 ImagePart。
    blob 取不到时保持占位并标 (unavailable)，不抛。"""

async def demote_all(memory, record_ids: list[str], ctx) -> int:
    """L1/L3 折叠前调用（见 §6.1）。对指定记录无条件降级，不受 keep_recent 保护。
    返回降级的图片张数。"""
```

## 9. 调用点

| 调用方 | 改动 |
|---|---|
| `compact.escalating_compact:466`（L1 之前） | 插一级 `_apply(media.demote_for_budget(...))` + 一条 `MEMORY_COMPACTED` 事件（`source="demote_images"`），与现有各级同构 |
| `compact.fold_root_experience:242` / `collapse_task_layer:102` | 各自在 `fold()` / 取 `original` 节之前调一次 `media.demote_all(...)`（见 §6.1） |
| `ProviderRegistry` / `CtxWeftRuntime.__init__` | 注册 `MediaCapabilityProvider`（同 `ControlCapabilityProvider` 的注册方式，`runtime.py:423`） |
| `assembler/sources/agent_recall.py:59` | `task_records = await media.rehydrate(task_records, ...)` |

## 10. 错误处理

| 情形 | 行为 |
|---|---|
| `BlobStore` 未注册 | `demote_for_budget` 返回 0（不降级）；`rehydrate` 直通。无 blob 时行为与改造前完全一致 |
| `blob.get()` 返回 None | 占位保留 + 追加 `(unavailable)`，不抛 |
| `get_image(ref)` 的 ref 不存在 | 返回说明性文本，不抛、不写 unfold 记录 |
| 降级过程中 `fold()` 失败 | 记 warning，跳过该条继续；本级 `freed_tokens` 相应减少，编排自然升级到 L1 |

## 11. 测试

- `policy.py` 纯函数：给定记录列表 + `keep_recent`，断言选中集合（无 IO，快）
- `refs.py` 编解码往返
- 降级：`fold()` 后位置不变（`(timestamp, seq_no)` 与降级前一致）
- 还原：unfold 后图回到原位；unfold 记录被折走后图自动折回
- unfold 记录不进 prompt（装配结果里不含该记录）
- 跨重启：重建 memory 后 unfold 仍生效
- `blob.get()` 返 None 时不抛且占位标记正确
- 回归：未注册 `BlobStore` 时全链路行为不变

## 12. 未决参数

- `keep_recent` 默认值：暂定 **2**（最近两张图保原样）。需实测校准。
- 占位文本的确切措辞：影响模型是否会主动调 `get_image`，需实测。
