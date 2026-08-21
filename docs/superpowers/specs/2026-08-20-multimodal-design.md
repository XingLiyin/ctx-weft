# 多模态（multimodal）整体设计

日期：2026-08-20
状态：设计已评审，待实现
子设计：[图片折叠与回放](2026-08-20-image-fold-replay-design.md)

## 1. 目标

让所有能开启 agent loop 的接口都接受多模态消息（文本 + 图片），并让图片在
memory、事件、装配、出网、折叠、恢复的全链路上保持完整、可估算、可回收、可取回。

**本期范围**：图片（`ImagePart`）。用户侧输入方向。

**本期不做**（§9 详述）：工具返图、音视频、摘要内含图。

## 2. 现状判断

**类型层已经就绪，搬运层全部拍扁。**

`ContentPart = TextPart | ImagePart`（`protocols/context.py:35`）已贯穿三个关键契约：

- `MemoryEvent.content` / `MemoryRecord.content`：`str | list[ContentPart]`（`memory.py:153,212`）
- `LLMMessage.content`：`str | list[ContentPart]`（`llm.py:107`）
- `ContextBlock.content`：`str | list[Any]`（`assembler.py:112`）

但每一个真正搬运数据的地方都无条件调用 `content_to_text`（`utils.py:134`，注释明写
"images skipped"）把它拍成字符串。所以工作量不在"加类型"，在"拆掉那一串拍扁点 +
补齐持久化序列化"。

### 2.1 数据流与拍扁点

```
host
 └─ SessionStartParams.user_prompt: str                    ← 入口①（runtime.py:302）
    ├─ run_single_task(user_prompt: str)                   ← 入口②（runtime.py:618）
    ├─ HitlManager.answer(text) / reject(message)          ← 入口③（hitl_manager.py:180,193）
    └─ TaskManager.reopen_task(new_prompt)                 ← 入口④（task_manager.py:498）
         ↓
SessionManager.create_session / resume_session（session_manager.py:35,96）
 ├─ Session.user_prompt / Task.user_prompt
 └─ SESSION_CREATED / SESSION_RESUMED / TASK_CREATED payload   ★ 事件持久化，dataclass 不可 json
         ↓
StepDriver.run → _persist_user_prompt（driver.py:167）          ★★ 拍扁：图第一次消失
         ↓  MemoryEvent(content=text) → memory provider
Assembler
 ├─ AgentRecallSource（agent_recall.py:94）                      ★★ 拍扁
 ├─ record_to_history_block（_history.py:67）                    ★★ 拍扁
 │    └─ token_estimate = token_counter(text)（_history.py:111）  ★ 图算 0 token
 ├─ TaskSpec / Knowledge / LongMemory sources                    ○ 拍扁是**正确**的（检索 query）
 ├─ budget.py 裁剪                                               ○ 按 metadata 判，不碰内容
 └─ Composer
      ├─ _history_to_messages_with_sources（composer.py:889）     ★★ 拍扁
      ├─ 当前消息框重建（composer.py:571-616）                     ★★ 拍扁后重建为纯 str
      ├─ 三个拼接器（composer.py:816,829,854）                     ★★ 拍扁
      └─ token 计数（composer.py:400）                            ★ 图算 0 token
         ↓
llm_gateway.py                                                   ✓ 已 parts-aware
         ↓
providers/llm/anthropic.py:343,361,377 · openai.py:381,398,405   ★★ 出网前丢图
```

## 3. 设计原则

**① 单一归一层。** 所有内容形态转换收在一个模块里，其余地方只调它，不各自写
`isinstance`。这是把改动从"N 处散点"收成"1 个模块 + N 处替换"的关键。

**② 只允许两类地方拍扁。** 其余全链路保 `list[ContentPart]`：

- **语义检索 query** —— `knowledge.py:39`、`long_memory.py:34`、`task_spec.py:35`
- **摘要输入** —— `compact.py:126,205`、`finalize.py:415`、`background_observe.py:100`、
  `recognize_intent.py:127`

**③ ref 最晚 rehydrate。** core 全程只见 blob ref，只有 LLM adapter 拼 wire payload
时才换回 base64。memory / 事件 / 装配链搬的都是几十字节字符串。

## 4. 内容模型与归一层

### 4.1 `ImagePart` 扩一个 source_type

```python
@dataclass
class ImagePart:
    data: str            # base64 / URL / blob ref，按 source_type 解释
    media_type: str
    source_type: Literal["base64", "url", "ref"] = "base64"
    type: Literal["image"] = "image"
```

只扩取值，不加字段（`protocols/context.py:26` 改一行）。`ContentPart` 联合类型与
所有下游签名不变。

### 4.2 `core/content.py`

```python
def normalize_content(x) -> str | list[ContentPart]
    """归一入口内容。base64 图片经 BlobStore 外部化成 source_type="ref"。"""

def content_to_text(x) -> str
    """保留在 utils.py:134 原位，语义不变，由本模块 re-export。
    与同在 utils 的 estimate_content_tokens 是一对，不拆开。"""

def content_with_prefix(content, text) -> str | list[ContentPart]
def content_with_suffix(content, text) -> str | list[ContentPart]
    """保 parts 的拼接：str 走原逻辑，list 则拼进首/尾个 TextPart（没有就新插一个）。"""

def content_to_jsonable(x) -> str | list[dict]
def content_from_jsonable(x) -> str | list[ContentPart]
    """事件 / 投影的规范 JSON 形态。ContentPart 是普通 dataclass，json.dumps 会直接炸。"""

def redact_content_for_event(x) -> str
    """把 ImagePart 渲染成 [image image/png ref:ab12…]，供事件 payload 用。"""
```

`estimate_content_tokens` 与图片 token 口径**留在 `utils.py:168`**（评审决定）。

## 5. Blob 存储

### 5.1 协议：并入 `protocols/filesystem.py`

与 `SpillSink`（`filesystem.py:34`）同处。两者形状一致——"core 不直接碰存储，只知道
有个 sink"——且宿主侧实现落点相同（`FilesystemToolsProvider` 已持有 per-session
workspace）。

```python
BLOB_REF_PREFIX = "blob:"

class BlobStore(ABC):
    """core 的二进制 sink 契约。

    put 必须内容寻址且幂等：同样的 data 返回同样的 ref，重复调用不重复存。
    get 对不存在 / 已回收的 ref 返回 None（**不得 raise**）——调用方据此降级为
    文本占位，绝不因取图失败中断 loop。
    """
    @abstractmethod
    async def put(self, data: bytes, media_type: str, ctx: ProviderContext) -> str: ...
    @abstractmethod
    async def get(self, ref: str, ctx: ProviderContext) -> tuple[bytes, str] | None: ...
```

注册走现有 `ProviderRegistry`（`runtime.py:187`），加一对
`register_blob_store` / `get_blob_store`。

### 5.2 内容寻址 + side index

blob 本体按 `sha256` 存；另存 `(tenant_id, session_id, sha)` 索引用于 GC。

选内容寻址而非作用域 key，是因为它同时给到三件事：写入端去重（同一张图反复引用只存
一份）、幂等（重放安全）、以及**rehydrate 字节稳定**——同一 ref 每次还原出的 base64
完全一致，Anthropic 的 prompt cache 前缀不会被打碎。带签名的临时 URL 做不到这点。

GC 挂现有 session 生命周期钩子（`SessionScopedCapabilityProvider.deregister_session`，
`runtime.py:697` 的 finally 块）。跨 session 继承（`_copy_memory_for_inherit:100`）只
复制 ref、不复制本体，仅补一行索引。

### 5.3 `NullBlobStore`

未注册时的默认实现：`put` 原样退回 base64（`source_type` 保持 `"base64"`），`get`
返回 `None`。**保证不接 blob store 的宿主行为完全不变**，也让 §10 的分阶段推进成立。

## 6. 分层改动

### 6.1 入口签名放宽（`str` → `str | list[ContentPart]`）

| 位置 | 备注 |
|---|---|
| `SessionStartParams.user_prompt` + `.create()`（`runtime.py:302,313`） | |
| `run_single_task(user_prompt=)`（`runtime.py:618`） | 内部 `description=user_prompt[:200]` 会在 list 上切片出错，改走 `content_to_text` |
| `SessionManager.create_session / resume_session / _make_root_task_manager`（`session_manager.py:35,96,155`） | |
| `HitlManager.answer(text) / reject(message) / resolve_*` + `HitlRequest.message`（`models.py:308`） | |
| `TaskManager.reopen_task` 的 `new_prompt` / `original_user_prompt`（`task_manager.py:520-550`） | `line 522` 现为 `if isinstance(str) else ""`，多模态直接丢空 |

**`Session.user_prompt` 保持 `str`**（`models.py:141`）：只存 `content_to_text` 摘要，
全量只放 `Task.user_prompt`，避免两处真源。

**LLM 产出的 prompt 不改**：`control_capability` 的 `create_task` / `dispatch` 参数
本就是模型写的文本。

**入口只做格式校验，不做 token 准入**：校验 `media_type` 白名单、base64 合法性、
单图字节上限（防畸形/恶意输入）。**不**校验 token 总量——单条消息塞太多图的情形由
装配期 `ContextOverflowError` 兜底，理由见子设计 §3。

### 6.2 持久化：事件与投影

全部走 `content_to_jsonable` / `content_from_jsonable`。

| 位置 | |
|---|---|
| `SESSION_CREATED` / `SESSION_RESUMED` payload | `session_manager.py:74,142` |
| `TASK_CREATED` / `TASK_REOPENED` payload | `task_manager.py:1167,563` |
| HITL `payload["message"]` | `hitl_manager.py:343` |
| 投影字段类型 | `control/types.py:20,47,48` |
| 反序列化 | `reducers.py:231,257,409,427,485,508-513` + `110-117`（HITL） |
| 投影 → 运行时模型 | `converters.py:25,54,55` |

不改这一组，重启后图必丢。

### 6.3 落库路径

- **`driver.py:167-186 _persist_user_prompt`** —— 去掉 `content_to_text`，原样传
  `task.user_prompt`。这是图第一次消失的地方，最关键的一处。
- `runtime.py:1402 _inject_user_reply` —— `f"Human declined: {req.message}"` 与
  `interrupt_edit_note(prev, content)` 两处拼接改用 `content_with_prefix`。
- `runtime.py:1460 _last_user_prompt` —— `ups[-1].content or ""` 可能已是 list，
  显式 `content_to_text`。
- `protocols/memory.py` 契约文档写明：宿主 memory provider 必须能持久化
  `list[ContentPart]`（本仓 `in_memory.py:384` 只是原样持引用）。

### 6.4 装配链保 parts

- `_history.py:67` `record_to_history_block` —— 保 parts；其中
  `wrap_compact_summary` / `PROGRESS_SO_FAR_HEADING` / `annotate_assistant_summary`
  三处拼接改 `content_with_prefix/suffix`
- `agent_recall.py:94` —— 同上
- `composer.py:889` `_history_to_messages_with_sources` —— 保 parts；注意
  `if not content and not tool_calls: continue` 的空判断要 parts-aware，否则
  **纯图消息会被当空消息丢弃**
- `composer.py:571-616` 当前消息框 —— 现在是拍扁后重建纯 str 消息，改成
  "文本前缀 + 保留原 parts"
- `composer.py:816,829,854` 三个拼接器 —— 换 `content_with_*`

**不改**：`task_spec.py:35`、`knowledge.py:39`、`long_memory.py:34`（原则②的
检索 query 例外）。

### 6.5 token 估算（图片感知）

**不能直接改用 `estimate_content_tokens`。** 它在 `utils.py:176` 无条件加
`_MSG_FRAMING_TOKENS = 4`，对纯文本不是恒等变换：五个改造点会各自凭空多 4 token/条，
其中 `is_short_segment` 与 `_is_short_leaf` 现在是「整段拼成一个字符串数一次」，
换过去变成 4×N，短段阈值与 short task 判定会实质漂移；`_active_memory_tokens` 同理会
移动 compact 的升级阈值。

改为在 `utils.py` 新增一个只补图片、不碰文本与 framing 的函数：

```python
def image_tokens(content: "str | list[ContentPart] | None") -> int:
    """content 中图片 part 的 token 补偿（不含文本、不含 framing）。"""
    if not content or isinstance(content, str):
        return 0
    return _IMAGE_PART_TOKENS * sum(1 for p in content if not hasattr(p, "text"))
```

五处一律改成 `既有的文本计数 + image_tokens(content)`，**纯文本逐字节恒等**。
`estimate_content_tokens` 重构成复用 `image_tokens`，使 `_IMAGE_PART_TOKENS` 保持
单一真源（`prepare.py:74,95` / `llm_gateway.py:281` 的既有行为不变）。

五处及其不改的后果：

| 位置 | 不改的后果 |
|---|---|
| `_history.py:111` | `token_estimate` 为 0 → budget 认为没超、一条不裁 → 整份 prompt 打到 provider 400 |
| `composer.py:400` | 装配期总量失真 |
| `compact.py:205` `_active_memory_tokens` | 折叠层级测出 `freed_tokens == 0`，编排误判 |
| `background_observe.py:99` `is_short_segment` | 图片密集段误判"短段免折" → 该段 raw 永久保留 |
| `finalize.py:415` `_is_short_leaf` | 图片密集 task 误判 short |

这五处不依赖本设计其余部分，可以独立先行（见 §10 Phase 0）。

**准确地说它们是「潜伏的」而非「已在发作的」bug**：当前没有任何入口能让 `ImagePart`
进入 memory，因此五处补充项在 Phase 0 内恒为 0。Phase 0 的意义是**先把地基修对**——
它的测试以合成记录验证补充项算得准，等 Phase 1-2 打通输入后，五处同时转为生效，
无需再回头改。

其中 `composer.py:400` 一处还额外依赖 §6.4 令 composer 保 parts：**Phase 2 的计划须
显式验证该项确实从恒 0 转为生效**，否则它会永久是死代码。

`_history.py:111` 一处还有一条独立的数据源错位：Task 2 的 `token_estimate` 用
`record.content`（未拍扁）算图片项，而 `budget.py` 只能读 `ContextBlock.content`。
`_history.py` 目前设 `content=text`（已拍扁），因此 `budget.py` 的图片计数**按构造恒为
0**——不只是「暂时没有图片」，而是即使 Phase 1 把真实 `ImagePart` 写入 memory 也依然
为 0，直到 `record_to_history_block` 本身停止拍扁。Phase 2 须一并对齐这两处的数据源。

`ContextOverflowError`（`errors.py:76`）默认文案补图片维度，让用户知道该删图而非删字。

### 6.6 LLM 出网

- `providers/llm/anthropic.py:343,361,377` —— `_parts_to_text` 换成真 blocks：
  `{"type":"image","source":{"type":"base64","media_type":…,"data":…}}`；
  `tool_result.content` 同样支持 blocks
- `providers/llm/openai.py:381,398,405` —— 换
  `[{"type":"text",…},{"type":"image_url","image_url":{"url":"data:…"}}]`
- `providers/llm/mock.py` 跟进，否则测试全挂
- `source_type=="ref"` 在此处 rehydrate（原则③）；`blob.get()` 返 `None` 时降级成
  `{"type":"text","text":"[image unavailable]"}`，**不抛**
- `llm_gateway.py` 不改：`_is_empty_content:125` / `_merge_message_content:172` /
  估算 `:281` 已 parts-aware

#### provider 不对称由 adapter 吸收

Anthropic 的 `tool_result` 支持 image block；OpenAI chat completions 的
`role="tool"` 消息**只接受文本**。

core 侧统一表达为 `LLMMessage(role="tool", content=[TextPart, ImagePart])`，差异全部
关在 adapter 内：

- **Anthropic**（`anthropic.py:356-375`）：`tool_result.content` 从字符串改为 block
  列表，原生承载图片
- **OpenAI**（`openai.py:376-402`）：tool 消息只发文本，**在该组连续 tool 消息之后
  追加一条 `user` 消息**承载图片。需把现有的 `for m in messages` 改成与 anthropic
  同构的 `while` 分组循环，才能定位组尾——同批多个 tool call 时，追加的消息必须在
  整组之后，夹在中间会触发 `insufficient tool messages following tool_calls message`

**这条 user 消息只存在于 wire payload，不落 memory。** 落库会制造出一个假的段边界
（`segment_fold.py:47`、`finalize.py:466`、`background_observe.py:87` 三处都用
「最后一条 role=user 回合」划段），打乱段折叠与胶囊范围。

与 `reorder_tool_results_after_calls`（`llm_gateway.py:198`）无冲突：那一步在 core
执行，adapter 在其之后，追加的消息不会再被搬动。

### 6.7 模型能力门控

`session.llm_model` 不支持视觉时，入口拒绝或降级成文本占位，不能让请求打到 provider
才 400。能力信息取自 `LLMClientResolver` 解析出的 client（`runtime.py:471 _resolve_llm`）。

### 6.8 可观测性脱敏

- `act.py:221` 与 `observe.py:121` 的 `str(m.content)` 会把整块 base64 dump 进
  `LLM_PROMPT_SENT` payload → 改 `redact_content_for_event`
- `recognize_intent.py:127` 非 str 直接 `else ""`，整条消息内容丢失 → 改
  `content_to_text`

外部化成 ref 后，`ImagePart` 的 `data` 只有几十字节，`content_to_jsonable` 直接进
事件 payload 即可，不需为 base64 单设截断。

### 6.9 折叠与回放

见子设计 [图片折叠与回放](2026-08-20-image-fold-replay-design.md)。摘要如下：

- compact 新增 **L0.5**（插在 L1 之前）：把 `ImagePart` 就地降级成文本占位，
  无 LLM、可逆、单位收益最高
- 模型经 `media:get_image(ref)` 取回，**图随 tool result 回到对话尾部**，附带
  「原本属于第几条用户消息」的位置信息
- 尾部追加是 append-only，**KV cache 前缀不动**。这是否决「装配期在历史原位还原」
  的决定性理由——原位还原会使 cache 前缀从该记录起全部失效，而被折的图往往位置很靠前
- 取回结果落在普通 `TOOL_RESULT` 记录里：跨重启天然成立、下一轮装配天然包含、
  段边界到来时自动被折走。**不需要任何额外状态或回收机制**
- `InvocationResult.content` 随之放宽为 `str | list[ContentPart]`（见 §9）
- 新模块 `core/media/`，占位格式 / blob ref 交互 / 位置描述全在墙内

## 7. 模块归属总表

| 关注点 | 归属 |
|---|---|
| `ContentPart` 类型 | `protocols/context.py` |
| 内容形态转换、拼接、序列化、脱敏 | `core/content.py` |
| 图片 token 口径 | `utils.py` |
| `BlobStore` 协议 | `protocols/filesystem.py`（与 `SpillSink` 同处） |
| 图片折叠 / 占位 / 取回 / 还原 | `core/media/` |
| wire 格式转换与 rehydrate | `providers/llm/*` |
| blob 本体存储与 GC | 宿主（`FilesystemToolsProvider` 或对象存储） |

## 8. 摘要恒为纯文本

明确约定：`MemoryKind.SUMMARY` 的 content **恒为 `str`**。

这条约束让 `_history.py:67` 那一堆 summary 包装逻辑（`wrap_compact_summary` /
`PROGRESS_SO_FAR_HEADING` / `annotate_assistant_summary`）完全不必改成 parts-aware，
省掉一大块改造面。代价是摘要里的图只能以 ref 占位的文本形式存在——与子设计 §6 的
"两段衰减"一致。

## 9. 不在本期范围

- **通用的工具返图**（浏览器截图、图表生成）：Phase 4 为 `media:get_image` 打开了
  接缝——`InvocationResult.content` 放宽为 `str | list[ContentPart]`，非文本部分经
  `metadata["content_parts"]` 由 provider 贡献，§6.6 的 adapter 改造对任意工具通用。
  **机制到位，但本期只有 `media:get_image` 使用**；让第三方 capability provider 用上
  它，需要额外的协议文档、大小限流与授权审查，独立立项。
- **音频 / 视频**：`ContentPart` 联合类型可扩，但 token 口径、折叠策略、provider
  支持面都是另一套问题。
- **摘要内含图**：见 §8，本期明确排除。

## 10. 阶段划分

依赖顺序如下，每阶段可独立发布：

**Phase 0 — token 估算修复**（§6.5）
五处换 `estimate_content_tokens`。修的是既有潜伏 bug，不依赖其余任何部分，
且是后续所有预算判断的地基。

**Phase 1 — 内容骨架**（§4、§5.1、§5.3、§6.1、§6.2）
`core/content.py` + `BlobStore` 协议 + `NullBlobStore` + 入口签名 + 事件/投影序列化。
此阶段结束时多模态内容能进能出、能持久化能重启，但还到不了模型。

**Phase 2 — 端到端打通**（§6.3、§6.4、§6.6、§6.8）
落库保 parts + 装配链保 parts + adapter wire 转换 + 事件脱敏。
**此阶段结束即多模态可用**（以 inline base64 形态，未外部化）。

**Phase 3 — 外部化**（§5.2、§6.7）
真 `BlobStore` 实现 + 入口 base64 → ref + adapter rehydrate + 能力门控。
此阶段把内存与事件体积降下来，是 Phase 4 的前提。

**Phase 4 — 折叠与回放**（子设计全文）
`core/media/` + L0.5 + `media:get_image`。

## 11. 回归保证

每个阶段都必须满足：**纯文本会话的行为逐字节不变。**

具体到实现上：

- 所有 `content_with_*` 在 `str` 输入时走原字符串拼接路径，产物与改造前一致
- `NullBlobStore` 未注册时，`normalize_content` 对纯文本是恒等变换
- 五个估算点对纯文本与改造前**逐字节同值**（`image_tokens` 对 `str` 输入恒返 0，
  见 §6.5；这是不能直接套用 `estimate_content_tokens` 的原因——它带 4 token 的
  framing 补偿，会静默移动裁剪与 compact 阈值）
- 未注册 `BlobStore` 时，L0.5 返回 0（不降级），adapter 侧无 ref 可 rehydrate，
  图保持 inline base64——即 Phase 2 的形态

## 12. 测试策略

- **归一层**：`content_with_*` / `*_jsonable` / `redact_*` 的往返与纯文本恒等
- **入口**：四个入口各自接受 `list[ContentPart]` 并落到 `Task.user_prompt`
- **持久化**：写事件 → `rebuild_view` → 图完整还原（跨重启核心保证）
- **装配**：纯图消息不被当空消息丢弃；当前消息框保留 parts；检索 query 仍是文本
- **出网**：两家 adapter 的 blocks 形态；`blob.get()` 返 `None` 时降级不抛
- **估算**：图片计入 token；budget 能因图片触发裁剪；`ContextOverflowError` 文案含图片数
- **回归**：全量既有测试在每个 Phase 后保持绿

## 13. 未决项

- 单图字节上限与 `media_type` 白名单的具体取值（§6.1）
- 视觉能力信息从 `LLMClient` 的哪个字段读（§6.7）——现有 duck-type 约定里
  没有对应字段，可能需要在 `protocols/llm.py` 补一个可选属性
- `_IMAGE_PART_TOKENS = 1600` 是否够保守（`utils.py:154`），需按真实计费校准
- `image_tokens` / `image_part_count` 以 `not hasattr(p, "text")` 判定非文本 part。若某个
  memory provider 把 content 作 JSON 往返后返回 `list[dict]`，则**每个** part（含文本）
  都会被计为图片。`content_to_text` 有对称的盲点（会把这类列表渲染成空串），故该失效
  模式是既有的；但方向变了——此前静默少算为 0，此后变为多算，会导致过度裁剪与虚假
  `ContextOverflowError`。今日潜伏（`in_memory.py` 存对象引用，`src/` 内无 JSON 往返），
  但 ctx-weft 是 SDK、memory 协议可插拔，第三方 provider 正是它出现的地方。**Phase 1
  须保证召回内容 rehydrate 成 `ContentPart` 对象**，或在归一层令判据 dict-aware。本
  Phase 不改判据。
