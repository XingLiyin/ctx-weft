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

  > **勘误（评审 2026-08-23 fix wave，I3）**：这四处读的是 **memory 记录**（用于生成
  > 摘要文本 / token 估算），**不在 LLM prompt 路径上**。Phase 2 实测（探针驱动
  > `act` / `compact` / `observe` / `recognize_intent` / `background_observe` 五个
  > compose purpose）确认：**真正发给 LLM 的 prompt 由 composer 构建，五个 purpose
  > 现在全部携带 inline base64 parts**，包括本条意在覆盖的 compact/recognize_intent/
  > background_observe 三个 facet purpose 自身的 prompt——这条拍扁点没有覆盖到它本想
  > 覆盖的路径。后果与准入条件见 §13。

**③ ref 最晚 rehydrate。** core 全程只见 blob ref，只有 LLM adapter 拼 wire payload
时才换回 base64。memory / 事件 / 装配链搬的都是几十字节字符串。

> **订正 + 已兑现（Phase 3b Task 3，2026-08-25，架构裁定 T0）**：上面这句
> 「**只有 LLM adapter 拼 wire payload 时才换回 base64**」**字面上做不到**，实现落点
> 与之偏离一层，特此订正（原文保留以存上下文）。
>
> 核实结论：adapter 的整条序列化链——`_build_payload` / `_serialize_messages` /
> `_parts_to_blocks`——**全是同步函数**，而 `BlobStore.get` 是 **async**，同步函数里
> 没法 await。
>
> **裁定：rehydrate 落在 `core/loop/llm_gateway.py` 的 `stream_llm`**——出网前最后一个
> async 关口、已在跑 `legalize_messages`，且**一处覆盖三家 adapter**（放 adapter 里要
> 写三遍）。落点在 `legalize_messages` 之后、`llm.complete` 之前。
>
> 这偏离 §3③ 的字面（gateway 属 core），但**保住了它的实质**：core 的 memory / 事件 /
> 装配链全程只见 ref，只有出网前那一瞬间内存里才存在 base64。
>
> **代价（已实测在生产路径上不成立）**：若将来出现绕过 gateway 直调 adapter 的路径，
> 那条路上的 ref 不会被还原。Task 5 的端到端测试从**真实 wire payload** 断言
> （`tests/integration/test_multimodal_end_to_end.py::`
> `test_ref_externalized_in_memory_but_full_base64_on_the_wire`），已确认本仓真实
> `start_session` 的两次 LLM 调用（`recognize_intent` + `act`）都经 `stream_llm`。
>
> **⚠️ 附带的静默损坏风险（Task 5 实测）**：`base64.b64decode("blob:<sha>")`
> **不抛异常**——默认 `validate=False` 会静默跳过非字母表字符，解出一串垃圾字节：
>
> ```
> base64.b64decode('blob:d4735e3a265e16ee')                -> b'nZw÷åí...'  # 垃圾，不报错
> base64.b64decode('blob:d4735e3a265e16ee', validate=True) -> binascii.Error
> ```
>
> 即：**ref 检测一旦失败，后果是「发出一堆垃圾图片字节」而不是报错**——静默损坏而非
> 响亮失败。两个直接推论，务必保留：
> 1. `_is_ref_part`（`core/content.py`）里那条「`data` 以 `blob:` 开头即判为 ref、
>    **无视**声明的 `source_type`」的兜底，是**防静默损坏的安全护栏**，不是整洁性
>    代码。该判据零假阳性：base64 字母表不含 `:`。
> 2. 任何端到端断言都**必须校验「解码回原始字节」**，不能用「不以 `blob:` 开头」这种
>    弱判据——后者杀不掉「还原出别的字节」这类损坏（Task 5 的 M2 变异体正是靠解码
>    断言才被抓住的）。

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

> **订正（Phase 3b Task 2）**：`NullBlobStore.put` 实际是 `raise NotImplementedError`，
> **不是**「原样退回 base64」——Phase 1 终审刻意如此，让接线错误立刻暴露，而不是静默
> 产出一个假 ref。「行为完全不变」这个结论仍然成立，但成立的机制是
> **调用方先探询 `can_externalize` 属性再决定是否 put**（见 §6.1 已兑现说明），
> 而不是靠 put 返回原值。

### 5.4 已兑现（Phase 3b Task 1，commit `490af28` + `2951d7c`）

`FilesystemToolsProvider`（`providers/capability_filesystem/provider.py`）新增第四个
契约实现 `BlobStore`（此前已实现 `ToolCapabilityProvider` / `SpillSink` /
`SessionScopedCapabilityProvider`）。

- **内容寻址**：`sha256(data).hexdigest()`，ref 形态 `blob:<sha>`。
- **幂等**：`_write_blob_if_absent` —— `content_path.exists()` 即直接返回，不重写。
- **落盘布局**：`<workspace>/blobs/<sha[:2]>/<sha[2:4]>/<sha>`，`media_type` 存在伴生
  文件 `<sha>.meta`（两级目录分片，避免单目录文件数爆炸）。
- **IO 走 `asyncio.to_thread`**，不阻塞事件循环。
- **`media_type` 冲突取「先写入者胜」**：`put` 本就是「已存在则跳过」的幂等写，
  `media_type` 沿用同一条规则最省心——不必新增分支决定「谁能覆盖谁」，也避免
  「同一份数据的 media_type 取决于调用顺序」这种难复现的行为。
- **`get` 恒不抛**（这条契约是 gateway rehydrate 的前提——rehydrate 在出网主路径上，
  抛异常会掀掉整个 LLM 请求）。三条返回 `None` 的分支：workspace 未登记 /
  ref 前缀不是 `blob:` / 内容文件不存在。`.meta` 缺失时 `media_type` 兜底为
  `application/octet-stream`。
- **安全发现（变异验证暴露）**：去掉「ref 前缀必须是 `blob:`」的检查后，
  `http://example.com/x.png` 会被当作 sha 去拼路径，**Windows 的 `pathlib` 把它解释成
  UNC 路径并真的发起了网络访问**（`WinError 64`）。这条前缀检查是**安全护栏**而非
  整洁性代码，已记入测试 docstring。
- 测试：`tests/unit/test_filesystem_blob_store.py`。

**GC / 生命周期：本 Phase 只做「不删」（用户裁定 D3）。** blob 追加写、永不回收，
清理责任留给宿主。§5.2 提到的 side index 与 `deregister_session` 挂钩**未实现**，
本 Phase 不做任何 GC / 引用计数 / TTL。

### 5.5 ⚠️ 宿主接线的隐式契约（Phase 3b Task 5 实测，最容易踩的一条）

**接了真 BlobStore 的宿主，必须显式传 `SessionStartParams.session_id`，并在调用
`start_session` 之前用 `register_session(session_id, workspace)` 预先登记 workspace。**

原因链：`FilesystemToolsProvider.put` 通过 `workspace_for(ctx)` 定位落盘根目录，
该映射由 `register_session` 建立；而 `session_id` 默认是在 `start_session` **内部**
生成的——宿主不显式传，就没有机会提前登记。

不这么做的后果：`put` 抛 `RuntimeError: no workspace registered for session ...`。
它是在**入口归一层**抛的，会让 `start_session` 整个失败。

这条契约此前**只在 `SessionStartParams` 的 docstring 里被暗示过**（「so the host can
pre-register session-scoped resources, e.g. the filesystem provider's workspace」），
没有任何文档把它和 BlobStore 串起来。Task 5 的端到端测试按简报字面写会直接
`RuntimeError`，正是踩到了这一条。**这是「距离真正接线还差什么」的直接答案之一。**

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

> **已兑现（Task 3，Phase 3a，2026-08-24）**：`validate_content`（`content.py:196`）
> 落地上述三项格式校验——`media_type` 白名单（`ALLOWED_IMAGE_MEDIA_TYPES`，Anthropic
> 与 OpenAI 都接受的交集）、base64 合法性（`base64.b64decode(..., validate=True)`）、
> 单图字节上限 `_MAX_IMAGE_BYTES = 5 * 1024 * 1024`（Anthropic 单图约 5MB 上限）；
> 并在含图时接上 §6.7 的视觉能力门控。接在 `start_session`
> （`runtime.py:302` 附近）与 `run_single_task`（`runtime.py:665` 附近）两个入口，
> **入口即拒、不落库**——校验失败不产生任何 Session/Task/事件记录，测试见
> `tests/unit/test_content_validation.py::
> test_run_single_task_rejects_image_before_persisting_anything`。
>
> **刻意不做 token 总量准入**，理由有二：其一如上文所述，超预算由装配期
> `ContextOverflowError` 兜底，入口层重复判定只会引入两套阈值互相打架的风险；
> 其二是纯文本路径的不变量——`start_session` 在改造前从不同步解析 LLM
> （解析推迟到任务真正执行时才异步发生），若为了做 token 准入而提前调用
> `_resolve_llm`，会把"LLM 解析失败"从"任务执行时才失败"变成
> "`start_session` 里同步失败"，这是本 Phase 明令禁止的行为变化（见 §13 新增条目）。
> `validate_content` 因此只在**内容含图**时才需要 `llm` 参数（`content_has_image`
> 判定，`content.py:47`），纯文本路径完全不触碰 LLM 解析。

> **已兑现（Task 2，Phase 3b，2026-08-25，commit `5573272`）**：入口外部化落地为
> `core/content.py` 的 `normalize_content(content, *, blob_store, ctx)`——把
> `source_type == "base64"` 的 `ImagePart` 写进 BlobStore、换成
> `ImagePart(data="blob:<sha>", source_type="ref")`；文本 / `url` / 已是 `ref` 的 part
> 原样保留（不重复外部化）。**不改原对象**，返回新列表。
>
> **先探询 `can_externalize`，再决定——不 try/except（Phase 1 终审契约）。**
> `BlobStore.can_externalize` 是新增的只读属性，基类默认 `True`，只有 `NullBlobStore`
> 覆写为 `False`；`NullBlobStore.put` 的 `raise NotImplementedError` **保持不动**。
> 若改成「调用 put 再捕获 NotImplementedError」，会把「响亮失败」降级成控制流——真正的
> 接线错误（宿主注册了一个尚未实现 put 的 store）也会被静默吞掉。不能外部化时
> `normalize_content` **原样返回同一对象**（`is` 相同），这是「不接 BlobStore 逐字节
> 不变」这条硬约束的直接实现手段。
>
> **顺序：`validate_content` 严格先于 `normalize_content`。** 两个理由：
> (a) 被拒的内容不该在 blob store 里留下垃圾——校验失败必须发生在任何 `put` 之前；
> (b) `normalize_content` 里的 `base64.b64decode(..., validate=True)` **刻意不加
> try/except**——validate 先行已把畸形 base64 拦成 `InvalidContentError`，此处再包一层
> 只会造出一条永不执行、也永不被测试的分支；顺序若被后来者接反，这里抛出的
> `binascii.Error` 正好是响亮的信号（变异验证实证：反转顺序后
> `test_rejected_content_never_reaches_blob_store` 因 `binascii.Error` 转红）。
>
> 接线在两个入口：`start_session` 与 `run_single_task`，均为 **validate → normalize**。
> `start_session` 在**能外部化时**把 `session_id` 提前定下并透传给 `create_session`
> ——否则外部化所锚定的 session 与真正创建出来的 session 会是两个不同 id；不能外部化时
> 整段是 no-op（不提前生成 id、不替换 params），行为逐字节不变。
>
> **M5 遗留已兑现**：`validate_content` 对 `source_type == "ref"` 的处理从 Phase 3a 的
> 无差别 `raise` 改为——**跳过 base64 解码与单图尺寸校验**（那两项在 `put` 之前的那次
> validate 里已经把过关，且此时 `data` 是 `blob:<sha>` 而非 base64，再解码必然失败），
> 但**仍逐条校验 `media_type` 白名单与视觉门控**（ref 可能来自记录回放或宿主构造，
> 不能假定必然合法）。`url` 形态维持 `raise`（本 Phase 不支持）。
> 测试：`tests/unit/test_normalize_content.py`。
>
> **⚠️ 两层互相遮蔽的冗余守卫（已知，刻意保留）**：`start_session`
> （`runtime.py:781` 附近）外层有一个 `if blob_store.can_externalize:`，
> `normalize_content` 内部还有一个同样的守卫。`NullBlobStore` 时外层已经短路、
> 内层根本不被调用，因此**任一单独删除都不可观测**——Task 2 的变异 C（删外层）与
> Task 5 的变异 M3（删内层）双双存活，互为镜像实证，且已查明**不是测试薄弱**：
> 真正承重的语义由「改判据源头」的变异（`NullBlobStore.can_externalize → True`）覆盖，
> 那类变异会让端到端测试立刻转红。**裁定保留两层**（防御性、零成本）。
> 写在这里是为了防止后来者发现「删掉其中一个没有任何测试转红」而误以为它是死代码。

### 6.2 持久化：事件与投影

全部走 `content_to_jsonable` / `content_from_jsonable`。

| 位置 | |
|---|---|
| `SESSION_CREATED` / `SESSION_RESUMED` payload | `session_manager.py:74,142` |
| `TASK_CREATED` / `TASK_REQUEUED` payload | `task_manager.py:1167,563` |
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

> **已兑现（Task 7，2026-08-23）**：`tests/integration/test_multimodal_end_to_end.py::
> test_assembled_token_count_higher_with_image_than_text_only` 驱动完整
> `start_session` → PrepareStep → `ContextAssembler.assemble` → `DefaultComposer.compose`
> 全链路，从生产事件 `CONTEXT_ASSEMBLED.token_count` 取真实值，断言含图会话
> 严格大于同等文本会话（差值 = `_IMAGE_PART_TOKENS`）。变异验证：把
> `composer.py:420` 的 `image_tokens(m.content)` 项改成常数 0，该测试即失败，
> 证明其确有拦截力。

`_history.py:111` 一处还有一条独立的数据源错位：Task 2 的 `token_estimate` 用
`record.content`（未拍扁）算图片项，而 `budget.py` 只能读 `ContextBlock.content`。
`_history.py` 目前设 `content=text`（已拍扁），因此 `budget.py` 的图片计数**按构造恒为
0**——不只是「暂时没有图片」，而是即使 Phase 1 把真实 `ImagePart` 写入 memory 也依然
为 0，直到 `record_to_history_block` 本身停止拍扁。Phase 2 须一并对齐这两处的数据源。

> **已兑现（Task 7，2026-08-23）**：`tests/unit/test_budget_strategy.py::
> test_overflow_image_count_from_real_assembled_history_block` 不手工构造
> `ContextBlock`——用 `record_to_history_block`（Task 2 之后的真实装配产物）
> 产出含 3 个 `ImagePart` 的 block，喂给 `PriorityBudgetStrategy.apply` 驱动溢出路径，
> 断言 `ContextOverflowError.image_count == 3`，证明装配链真的把 parts 送到了
> `budget.py:89` 的 `image_part_count(b.content)`。变异验证：把
> `utils.py` 的 `image_part_count` 改成恒返回 0，该测试即失败。

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

> **订正（Phase 3b Task 3，2026-08-25）**：上面这组条目里有两条已被实现推翻，
> 原文保留以存上下文：
>
> 1. 「`source_type=="ref"` **在此处**（adapter）rehydrate」——**订正为：rehydrate 落在
>    `llm_gateway.stream_llm`**，理由见 §3③ 的架构裁定 T0（adapter 序列化链全是同步
>    函数，`BlobStore.get` 是 async）。降级语义不变且已兑现：`get()` 返回 `None` 时换成
>    文本 part `[image unavailable: <media_type>]`，**不抛**。
> 2. 「`llm_gateway.py` 不改」——**订正为：改了**。`stream_llm` 新增两个**带默认值**的
>    kwarg（`blob_store=None`、`provider_ctx=None`），在 `legalize_messages` 之后、
>    `llm.complete` 之前对每条消息 `dataclasses.replace(m, content=await
>    rehydrate_content(...))`。不传时整段不执行——既有调用方零影响。接线：
>    `LoopContext.blob_store`（默认 `None`）由 `runtime._build_loop_ctx` 从
>    `providers.get_blob_store()` 注入，两个调用方（`stream_llm_resilient` /
>    `recognize_intent`）用 `getattr(ctx, "blob_store", None)` 取值——既有测试里大量
>    ctx 桩不是真正的 `LoopContext`，没有这些字段。
>
> **两家 adapter 里「`source_type` 恒为 `base64`」的陈述同样已订正**（Task 3 已在
> `anthropic.py` / `openai.py` 的 `_parts_to_blocks` docstring 里改掉，此处同步）：
> 到达 `_parts_to_blocks` 时 `source_type` 确实恒为 `"base64"`，但这
> **不再是数据模型的固有属性，而是来自上游 gateway 的保证**——Phase 3b 起图片在入口
> 就被外部化成 ref，是 `stream_llm` 在出网前还原回 base64 的。
> **绕过 gateway 直调 adapter 的路径会丢掉这个保证**，那条路上的 ref 会被当 base64
> 写进 payload（且不会报错，见 §3③ 的 `b64decode` 静默损坏）。
>
> **`rehydrate_content` 的三条零开销短路**（都**原样返回同一对象**）：纯文本
> （`str` / `None` / 空）、`can_externalize` 为假的 store、内容里根本没有 ref。
> dict 形态的 part 也被正确处理（**dict 进 dict 出**）——`getattr(dict, "source_type",
> "base64")` 在 dict 上取不到属性、会落回默认值 `"base64"`，于是 dict 形态的 ref 会被
> 静默当 base64 塞进 wire payload。这是三种结局里最差的一种（不可观测的损坏 vs
> 可观测的降级），故 rehydrate 路径专用的字段读取器 `_part_field` 是 dict-aware 的。
> **被冻结的判据 `not hasattr(p, "text")` 未被改动**（`core/utils.py` 两处原封不动）。
>
> **⚠️ 已知的语义借用：`rehydrate_content` 用 `can_externalize`（一个「**写**」能力）
> 来决定是否「**读**」。** 对当前仅有的两个实现是正确的（`NullBlobStore` 是唯一返回
> `False` 的，而它的 `get` 恒返回 `None`），并且这正是保住「不接 BlobStore 时逐字节
> 不变」的手段——若走 `get → None → 降级` 的路，不接 blob 的宿主会把 inline base64 图
> 降级成 `[image unavailable]`，那就是行为变化。**但一个假想中的「只读 store」
> （能 get、不能 put）会被这条短路误伤：它的图永远取不回来。** 已在代码里内联注明；
> 若将来真的出现只读 store，须把判据换成一个独立的「可读」能力位，而不是删掉短路。
>
> 测试：`tests/unit/test_gateway_rehydrate.py`（14 测），变异验证 6 个变异体全部被杀死。

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

> **未兑现（评审 2026-08-23 fix wave，I2）**：上面这条 OpenAI 重定位**Phase 2 没有实现**。
> `openai.py:372-413` 的 `_serialize_messages` 保留了原有的 `for m in messages` 循环，
> 没有改成与 Anthropic 同构的 `while` 分组循环去定位 tool 消息组的组尾。实际行为是：
> `role="tool"` 的消息经 `_parts_to_text` 拍扁成纯文本发送（见本文件同段），其中的
> **非文本 part（图片）被静默丢弃，不产出任何占位符**——既不报错也不提示，图片就是
> 消失了。
>
> Phase 2 里 tool result 恒为文本（没有 capability provider 会通过 tool result 返图），
> 所以今日无实际损失。但 Phase 4 实现 `media:get_image` 后，`InvocationResult.content`
> 会真的携带 `ImagePart`（见 §9），届时 tool 消息就会带图——**在着手 Phase 4 之前必须
> 补上这条重定位**，否则 `media:get_image` 取回的图片会在 OpenAI 侧原样丢失。
>
> 顺带记录 `_parts_to_text`（`openai.py:416` 与 `anthropic.py:390` 各一份、逻辑同构）
> 现在的不变式，Phase 4 补重定位时会依赖它：**非文本 part 一律跳过，不产出占位符**——
> 调用方（当前是 `role="tool"` 的拍扁路径）若需要占位符（如「[image unavailable]」
> 或「[见下条工具结果]」之类的用户可见提示），需在调用方自己加，`_parts_to_text`
> 本身不做。

> **Phase 2 端到端出网验证（Task 7，2026-08-23）**：
> `tests/integration/test_multimodal_end_to_end.py::
> test_multimodal_prompt_reaches_wire_payload_as_image_block` 驱动
> `start_session(user_prompt=[TextPart, ImagePart])` 走完一个 actor 回合，用一个
> 子类化 `AnthropicAdapter`（复用真实 `_build_payload`/`_serialize_messages`，只是
> 不做真实网络请求）捕获实际 wire payload，断言其中含 `{"type": "image", ...}`
> block——证明图片真的从 memory 一路打到出网 payload，而不只是停在
> `AssembledPrompt.messages` 里。变异验证：把 `anthropic.py` 里 `_parts_to_blocks`
> 的图片分支改成退化成文本，该测试与 `test_adapter_multimodal_wire.py` 的两条
> 既有测试同时失败。

### 6.7 模型能力门控

`session.llm_model` 不支持视觉时，入口拒绝或降级成文本占位，不能让请求打到 provider
才 400。能力信息取自 `LLMClientResolver` 解析出的 client（`runtime.py:471 _resolve_llm`）。

> **已兑现（Task 2/3，Phase 3a，2026-08-24）**：能力字段落在
> `ModelConfig.supports_vision: bool = False`（`providers/llm/provider.py`）——
> **per-model 而非 per-provider**：同一账号下 `gpt-4o` 支持视觉而 `gpt-3.5-turbo`
> 不支持，声明必须挂在单个模型配置上，挂在 adapter/provider 级会让该账号下所有
> 模型被一并放行。**严格默认 `False` 是破坏性变更**——未显式配置
> `supports_vision=True` 的模型一律被视为无视觉能力，宁可入口报错也不让
> image block 打到纯文本模型后被 provider 400（`errors.py:136`）。
>
> `LLMProvider.get_client`（`provider.py:254-257`）把 `model_cfg.supports_vision`
> 透传给 `_FixedModelClient`，后者以 `supports_vision` property 暴露
> （`provider.py:104-105`）。core 侧统一约定 `getattr(llm, "supports_vision", False)`
> 读取（`content.py:216`、`protocols/llm.py:263-266`）——duck-typed、非协议必需字段，
> 缺省地对旧 client（没有该属性）也拿到严格默认 `False`。
>
> 门控接在 `validate_content`（`content.py:196`）里，`start_session` 与
> `run_single_task` 两个入口调用（见 §6.1 已兑现说明）。
>
> **本仓两家 adapter（`AnthropicAdapter` / `OpenAIAdapter`）不声明 `supports_vision`**——
> 判断依据见 Task 4 报告：本仓的真实解析路径（`LLMProvider.get_client` →
> `_FixedModelClient`）已从 `ModelConfig` 透传该字段，adapter 本身从不被 core 直接
> 持有。但 `_resolve_llm`（`runtime.py:501-514`）绕过 `_FixedModelClient` 拿到裸
> `LLMClient` 的路径**截至本 Phase 审计到两条**（措辞留白：不排除今后出现第三条），
> 均不是本仓 adapter 需要为此改动的理由：
>
> 1. `CtxWeftRuntime.__init__` 的 `llm=` 兜底参数（`runtime.py:433`，注释明写
>    "fallback for backward compat / tests"）——`_resolve_llm` 在未注册 `LLMProvider`
>    时原样返回它。
> 2. **自定义 `LLMClientResolver`**（`protocols/llm.py:298-310`，一个 `Protocol`）经
>    `register_llm_provider`（`runtime.py:288`）注册——这是本 SDK 的**主要多账号扩展
>    点**，接受任何实现了 `get_client(account, model) -> LLMClient` 的对象，*不*要求
>    经过 `_FixedModelClient`。本仓 `LLMProvider.get_client` 只是这个 Protocol 的一种
>    实现；第三方宿主注册自己的 resolver、其 `get_client()` 直接返回裸 adapter，是
>    预期用法而非边缘情况，比 llm= 兜底更容易在实际集成中出现。
>
> **两条路径都 fail-closed**：裸 adapter 没有 `supports_vision` 属性，
> `getattr(llm, "supports_vision", False)`（`content.py:216`）取到严格默认 `False`，
> 图片照样被拒——这正是「未显式配置视觉能力就整体拒绝」的预期行为，不是漏洞。
>
> 两条路径都没有 per-model 配置（`context_limit` 等窗口参数同样由调用方在自己的
> `get_client()` 实现里决定，不是本仓 `ModelConfig` 管），所以走这两条路径、且需要
> 开放视觉能力的宿主，应在自己返回的 client 对象上按需声明该 duck-typed
> `supports_vision` 属性（自定义 resolver 里包一层最直接），而不是让本仓 adapter 类
> 硬编码 `supports_vision = True`——那会让该 provider 的所有模型（含纯文本模型）在
> **本仓的主路径**上也被一并放行，绕过 per-model 的严格默认，与本节开头的破坏性
> 默认精神相悖。

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

> **已兑现（2026-08-25）**：Phase 3 实际分两批落地。
> **Phase 3a**（`c9fa1c6`）：防 400 护栏——`validate_content` 格式校验 + §6.7 视觉能力
> 门控 + 两家 adapter 的空白文本块修复。
> **Phase 3b**（`490af28` → `02fdfc6`）：真 `BlobStore` 实现（§5.4）+ 入口
> `normalize_content` 外部化（§6.1）+ **gateway**（不是 adapter，见 §3③ 裁定 T0）
> rehydrate + per-purpose 图片降级（§13 附表）。
> 与原文的两处偏差：rehydrate 落点是 gateway 而非 adapter；§5.2 的 side index / GC
> **未实现**（用户裁定 D3：本 Phase 只做「不删」）。

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

**Phase 3a 终审复审遗留（2026-08-25 裁定）：**

> **已兑现（2026-08-25，commit `06a2795`）**：下述第一条「dict 形态纯文本会翻转
> `start_session` 的失败类型」已修复，**且刻意未改被冻结的判据**。修法是调整顺序 +
> 惰性解析：`validate_content` 改为「格式校验先行 → 才轮到视觉门控」，并新增
> `llm_resolver` 惰性参数；`start_session` 改传 resolver、不再用 `content_has_image`
> 做闸。三条路径现均正确：纯文本早返回（resolver 不被调用）／dict 内容报
> `InvalidContentError`（resolver 不被调用）／合法图片才解析并门控。
> 顺带修掉一个同源缺陷：原实现**先门控后校验格式**，导致畸形内容被报成
> `VisionNotSupportedError`、掩盖真正的问题。
> 守卫：`tests/unit/test_content_validation.py`（32 passed，含两条 resolver 计数断言）。
> **判据本身（`not hasattr(p, "text")`）仍冻结**——若 Phase 3b 要让归一层认识 dict，
> 仍须同时修 `core/utils.py` 的 `content_to_text` / `image_part_count`。


- **dict 形态纯文本会翻转 `start_session` 的失败类型。** I3 选定方案 (B)（入口只接受
  dataclass 形态 part）后，`content_has_image([{"type":"text","text":"hello"}])` 仍返回
  `True`——因为 `_is_text_part` 用 `hasattr(part,"text")`，dict 永不满足。后果：**dict 形态
  的纯文本内容会触发 `start_session` 的提前 `_resolve_llm`**，而 S2 的修复本意正是让纯文本
  不走这条路。已实测：无 LLM 注册时，`start_session(user_prompt=[{"type":"text","text":"hello"}])`
  抛 `RuntimeError: No LLM available` 而非 `InvalidContentError`。
  内容最终都会被拒（两种错误都是拒绝），但**错误类型对宿主不可预期**。
  Phase 3b 若让归一层认识 dict（方案 A），须同时修正 `core/utils.py` 的
  `content_to_text` / `image_part_count`——三者共用同一判据字面量，只改一处会制造新分歧
  （这正是 Phase 3a 选 (B) 而非 (A) 的理由）。

- **中文注释持续增加 `RUF002/003` 噪音。** 本仓的 ruff 完整口径（含 RUF00x）在本 Phase
  触及的三个文件上从 271 → 334（+63），全部来自新增中文注释里的全角括号。
  各 Phase 的核验口径一直是窄 select（`I001,F401,F811`），在该口径下始终零新增——
  两个数字都对，只是量的不是一回事。若将来要把 RUF00x 纳入 CI 门禁，需先做一次全仓清理，
  否则新代码会被既有的 ~1855 条同类噪音淹没。

**Phase 3 的强制前置（Phase 2 终审后裁定，2026-08-24）：**

- **纯空白文本块仍会出网。** Phase 2 的 I1 修复只跳过了 falsy 文本（`if text:`），
  而 `TextPart("   ")` 是 truthy——它挨着 `ImagePart` 时仍产出
  `{"type":"text","text":"   "}`。Anthropic 对空/**纯空白**文本块同样返回 400。
  gateway 的 `_is_empty_content` 拦不住（图片确是内容，消息被正确保留）。
  实测确认。修法：两家 adapter 的跳过条件改为 `if text.strip():`。
  安全性：只含空白 `TextPart` 而无图的消息在上游已被 `_is_empty_content`（它 strip）
  丢弃，故不会产出空 block 列表。**Phase 3 的第一件事。**
- **`anthropic.py` assistant 分支的注释已失效。** 其陈述的「`_parts_to_blocks` 对每个
  part 恰好产出一个 block」在 I1 修复后不再成立（falsy 文本 part 会被跳过）。
  代码本身仍正确（`content_blocks or ""` 的兜底行为不变，因为 content_blocks 为空
  仍蕴含消息为空），但注释会误导后来者——它正是 Task 5 删除死代码时所依据的不变式。
  Phase 3 顺带更正。

> **已兑现（Task 1，Phase 3a，2026-08-24）**：上述两条均已修复。
> 跳过条件改为 `if text.strip():`（两家 adapter，共四处：`anthropic.py` 的
> user/assistant/tool_result 三处 + `openai.py` 的对应处），纯空白 `TextPart`
> 挨着 `ImagePart` 时不再产出空白文本 block。测试：
> `tests/unit/test_adapter_multimodal_wire.py::
> test_anthropic_whitespace_text_part_next_to_image_dropped` 与
> `test_openai_whitespace_text_part_next_to_image_dropped`（两家 adapter 各一条同构
> 用例）。`anthropic.py` assistant 分支的失效注释已同步更正为准确描述
> `_parts_to_blocks` 会跳过纯空白文本 part 的行为。

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
- **dict-aware 分歧在 Phase 2 变宽了（评审 2026-08-23 fix wave，M2）**：两家 adapter 的
  `_parts_to_blocks` 现在是 dict-aware 的（`p.get("type") == "image"` 分支），而
  `core/utils.py` 的 `content_to_text` / `image_part_count` 仍是 dict-blind（只认
  `hasattr(p, "text")`，dict 形态的图片 part 会被误判成文本 part）。上面那条已经点出
  这个盲点是「既有的」，但 Phase 2 之前只有 adapter 与 utils 两边都 dict-blind，行为
  至少对称；现在 adapter 侧已经会正确处理 dict-shaped part、utils 侧仍不会，
  同一个 `ImagePart` 在 wire 转换与 token 估算两条路径上可能被区别对待。Phase 3/4
  触碰 `core/utils.py` 或引入会返回 dict 形态 part 的 memory provider 前，应一并
  收敛这个分歧（而不是照抄 adapter 的 dict-aware 写法只加一半）。
- **facet purpose 现已携带 inline base64（评审 2026-08-23 fix wave，I3）**：见 §3②
  勘误。三条后果，作为 Phase 3/4 的准入条件：
  - **(a) compaction 会重发所有图片。** compaction 恰在上下文超预算时触发，而它
    现在的摘要 prompt（由 composer 构建）会带着触发它的那些图片一起发出去——这与
    「compaction 是为了省 token」的目标相悖，且在图片本身就是超预算主因时可能
    无法收敛。Phase 3/4 落地前须决定：compaction 输入是否该在拍扁前先过 L0.5
    降级（§6.9），还是走独立的、真正拍扁图片的摘要输入路径。

    > **已兑现（Task 4，Phase 3b，2026-08-25，commit `c00283d`）**：选了第三条路——
    > **不改摘要输入路径，改 composer 的出口**。`compose` 在
    > `return AssembledPrompt(...)` 之前、**`token_count` 计算之前**，对
    > `purpose not in _IMAGE_BEARING_PURPOSES`（= `frozenset({"act"})`）的请求统一调
    > `downgrade_images_to_text`，把 `ImagePart` 换成确定性文本占位
    > `[image {media_type}]`。五条 purpose 分支都汇到这一个出口，故只需一处。
    > 详见下方「§13 附：per-purpose 图片策略（Phase 3b 落地）」。
    > 测试：`tests/unit/test_purpose_image_policy.py`（35 测）与
    > `tests/integration/test_multimodal_end_to_end.py::
    > test_compact_assembly_carries_no_image_while_act_still_does`。
  - **(b) 视觉能力门控未生效。** §6.7 的门控是 Phase 3 交付物，**当前没有任何东西
    阻止 image block 进入纯文本模型**的请求——纯文本模型收到 image block 大概率
    直接 400（或更差，静默忽略图片内容）。Phase 3 门控上线前，这是一个已知但
    未修复的失败模式，不是「设计遗漏」而是「按阶段划分排到了 Phase 3」，写在此处
    防止被当成新发现重复报告。
  - **(c) 字节上限无约束。** 现有 token 估算按 `_IMAGE_PART_TOKENS = 1600`/图算，
    20 张 5MB 图只算约 32k tokens、在多数 context budget 下能通过，但原始字节
    早已超过 provider 的单请求体积上限（Anthropic 约 32MB/请求）——请求会在
    provider 侧因体积被拒，而不是被 budget 提前拦下。Phase 3/4（尤其是引入真
    `BlobStore` 外部化、或允许更大 `media_type` 白名单）落地前，需要在 token
    估算之外补一条独立的字节预算，或在 §6.1 的单图字节上限之上再加一条
    「单请求总字节上限」。

**已知缺口（Task 4，Phase 3a，2026-08-24，刻意范围划定）：**

- **HITL 应答与 `reopen_task` 两个入口未接校验与门控。** §6.1 列出的四个入口
  （`SessionStartParams.user_prompt` / `run_single_task` / `HitlManager.answer/reject`
  / `TaskManager.reopen_task`）里，本 Phase 只在前两个（`start_session` /
  `run_single_task`）接了 `validate_content` 与视觉门控。`HitlManager.answer(text)` /
  `reject(message)` 与 `TaskManager.reopen_task(new_prompt)` 仍未调用
  `validate_content`——若这两个入口将来放宽为接受 `list[ContentPart]`
  （见 §6.1 表格，本 Phase 尚未实现该放宽，签名仍是 `str`），畸形/超限图片或
  纯文本模型收图会绕过入口校验直接进入 memory。这是刻意的范围划定，不是遗漏：
  Phase 3a 的目标是防 400 护栏，优先覆盖两个主入口；HITL/reopen 的校验接入
  留给后续 Phase。

- **`start_session` 的纯文本路径刻意不提前解析 LLM。** Task 3 fix round 2 的裁定：
  `validate_content` 需要 `llm` 参数才能做视觉门控，而获取 `llm` 需要调用
  `_resolve_llm`——但改造前 `start_session` 从不同步解析 LLM（解析完全推迟到任务
  真正执行时，由 `_make_task_runner` → `_SessionTaskRunner` 异步触发）。若为了给
  `validate_content` 传 `llm` 而在 `start_session` 里无条件调用 `_resolve_llm`，
  会把"没有可用 LLM"这件事从"任务执行时才失败"变成"`start_session` 里同步失败"，
  违反本 Phase 的硬约束「纯文本行为逐字节不变」——哪怕 `_resolve_llm` 本身是无副作用
  的纯查表也不行，因为可观察的失败时机变了。
  裁定：`start_session` 先用 `content_has_image`（`content.py:47`）判断内容是否
  含图，**只有含图时才调用 `_resolve_llm`** 传给 `validate_content` 做视觉门控；
  纯文本内容走 `validate_content(content)`（不传 `llm`，只做格式校验，天然
  no-op）。`run_single_task`（`runtime.py:648`）不受此约束——它改造前就已经
  无条件同步调用 `_resolve_llm`（`runtime.py:665`），所以本 Phase 直接在该处
  先行调用 `_resolve_llm` 并把 `llm` 传给 `validate_content`（`runtime.py:667-668`
  注释：「`_resolve_llm` 是纯查表，此处先行调用安全」），不需要 `content_has_image`
  分支——纯文本路径在这里本就已经解析 LLM，不存在"提前解析"的问题。
  这条裁定记在此处供 Phase 3b/4 参考：任何新入口
  校验若需要 `llm`，都必须先判断"是否真的需要它"，不能为了校验方便而破坏
  既有的惰性解析时机。测试：
  `tests/unit/test_content_validation.py::
  test_start_session_plain_text_does_not_eagerly_resolve_llm`。

**Phase 3a 防 400 护栏复审 fix wave（2026-08-25）新增缺口/裁定：**

- **工具产出的图片绕过全部三道护栏（I4，仅记录，不实现）。** §6.6 明文设计
  `LLMMessage(role="tool", content=[TextPart, ImagePart])`（Phase 4 的
  `media:get_image` 会产出这种形态）。但当前没有任何入口对工具结果调用
  `validate_content`——`start_session` / `run_single_task` 只校验的是**发起**请求的
  `user_prompt`，工具执行产出的 `LLMMessage(role="tool", ...)` 从工具 handler 直接
  进入消息序列，不经过这两个入口。也就是说：即便本 Phase 把 `user_prompt` 的图片
  护栏做得再严格，一个 text-only 模型仍可能在工具循环中间被喂 image block → 400，
  这正是本 Phase 要防的失效模式，只是从"用户输入"这扇门换到了"工具输出"这扇门。
  已记的缺口清单（本节上方，Task 4）目前只列了 HITL 与 `reopen_task`，未覆盖这条，
  在此补记。
  **门控该放在哪里，两个候选：**
  - 工具结果路径（工具 handler 产出 `LLMMessage(role="tool", ...)` 之后、拼入历史
    之前）——离"数据源头"最近，能在落库前就拒绝，但工具产出点分散（凡是能返回
    `ImagePart` 的 capability 都要接一遍），覆盖面随 capability 数量线性增长。
  - `stream_llm`（`core/loop/llm_gateway.py`，出网前最后一道关口）——是本模块
    docstring 定义的"发送前合法化"唯一关口，天然覆盖所有来源（用户输入、工具输出、
    历史回放）的图片，一处生效、不随 capability 数量增长；代价是校验发生得晚
    （消息已落库/已进入历史后才拒绝，不是"入口即拒、不落库"）。
  **倾向**：放在 `stream_llm`。理由：它已经是"发送前合法化"的唯一关口
  （`legalize_messages` 六条不变式全在这里做），新增"逐条 tool-result 图片过
  `validate_content` 视觉门控"是同一关口职责的自然延伸，且不需要在 Phase 4 每新增
  一个可能产图的 capability 时都记得补校验——这正是本 Phase 反复强调的"防 400 是
  唯一关口"架构原则（模块 docstring 开篇）在工具结果场景下的对应延伸。代价（校验晚、
  已落库）需要 Phase 4 落地时与"入口即拒不落库"的既有语义做取舍，留给到时决定。
  本条只记录判断倾向，不在本 fix wave 实现（超出 Phase 3a 范围，需要 Phase 4 的
  `media:get_image` 落地后才有真实调用点可测）。

- **dict 形态 part 被入口全数拒绝，且把纯文本拖进提前解析分支（I3，评审 2026-08-24
  fix wave，裁定 B）。** `content.py` 的 `_is_text_part`（`hasattr(part, "text")`）
  对 dict 形态 part 恒为 `False`——纯文本 dict `{"type":"text","text":"hello"}`
  会被 `content_has_image` 误判为"含图"，进而在 `start_session` 触发提前
  `_resolve_llm`（§13 上一条裁定要防的行为，从另一扇门回来了）；被 `validate_content`
  当成"无 `media_type` 的图片"直接 `InvalidContentError` 拒绝。两家 adapter
  （`anthropic.py` / `openai.py`）的 `_parts_to_blocks` 原生支持 dict 形态 part，
  但入口把它们全拒了——入口比 adapter 更严格，形成一个不对称的硬限制。
  评审给出两个选项：(A) 让归一层认识 dict（`_is_text_part` / `validate_content`
  都支持 Mapping 取值）；(B) 保持现状，钉住这个限制并更新 spec，要求宿主在
  ingest 前把 dict 形态 rehydrate 成 `ContentPart` 对象。**裁定：选 (B)。**
  理由：`_is_text_part` 的判据字面量（`hasattr(part, "text")`）与
  `core/utils.py` 的 `content_to_text` / `image_part_count` 共享——本节上方
  （评审 2026-08-23 fix wave，M2）已经点出两边一度存在 dict-aware 分歧、需要
  "一并收敛"而非"照抄一半"；`content_to_text` 还被
  `tests/unit/test_content_module.py::test_content_to_text_reexported` 钉死为
  `utils.content_to_text` 的同一个对象引用。若只在 `content.py` 本地给
  `_is_text_part` / `validate_content` 加 Mapping 支持，会立刻制造一个新的、
  比现状更难追踪的分歧：`content_has_image` / `validate_content` 认得
  dict-text，`content_to_text` / `image_part_count` 仍不认得，同一份 dict 内容
  在"是否含图判断"与"拍扁成文本"两条路径上给出不同答案。这比现状"两处对称地
  都不认识 dict"更危险，不满足"不改判据本身"的约束（哪怕只改
  `content.py` 一处，也会在两处判据之间制造语义分叉，等价于事实上改了判据的
  可观察行为）。故选 (B)：`_is_text_part` 判据字面量不变，只把这个已知限制
  钉成回归测试
  （`tests/unit/test_content_validation.py::
  test_dict_text_part_is_misclassified_as_image_known_limitation` /
  `test_dict_text_part_rejected_by_validate_content_known_limitation`），并在此
  记录：**入口目前只接受 dataclass 形态的 `ContentPart`（`TextPart`/
  `ImagePart`）**——dict-shaped part（含纯文本 dict）会被入口硬拒绝或误判，宿主
  必须在把内容喂给 `start_session`/`run_single_task` 之前，把从 JSON 往返/
  memory provider 召回的 dict 形态内容 rehydrate 成 `ContentPart` 对象
  （`content_from_jsonable` 即为此设计）。这把上面 M2 记录的"token 误计"级别的
  隐患，在入口这一层升级成了"硬拒绝"——两者同源（同一判据、同一盲点），只是
  暴露的层不同。真正收敛（选项 A，含 `core/utils.py` 一并改成 Mapping-aware）
  留给 Phase 3b/4，届时若要做，须同步改 `_is_text_part` / `content_to_text` /
  `image_part_count` 三处，不能只改一处。


---

## §13 附：per-purpose 图片策略（Phase 3b Task 4 落地，用户裁定 D2）

用户裁定原话：「observe 也不是很需要看图。除了 act 以外的步骤对图片没有需求」。

**当前取值表**（`core/assembler/composer.py`，`_IMAGE_BEARING_PURPOSES = frozenset({"act"})`）：

| compose purpose | 携带真实图片 | 理由 |
|---|---|---|
| `act` | ✅ 是 | 唯一需要模型真看图的步骤 |
| `compact` | ❌ 降级 | 恰在超预算时触发；产出按 §8 恒为纯文本 |
| `observe` | ❌ 降级 | 判任务成败靠 actor 产出与工具结果（**见下方功能回退风险**） |
| `background_observe` | ❌ 降级 | 同上 |
| `recognize_intent` | ❌ 降级 | 只是填元数据 |

**若将来要让某个 purpose 重新看图，改这一处**：`composer.py` 的
`_IMAGE_BEARING_PURPOSES` 集合加上该 purpose 名即可，别处无需改动——五条 purpose 分支
都汇到 `compose` 末尾同一个出口。（例如要让 observe 看图：
`frozenset({"act", "observe"})`。）

**降级实现**：`core/content.py` 的 `downgrade_images_to_text` 把图片 part 换成
`[image {media_type}]`。放在归一层而非 composer 内联，遵 §3① 单一归一层；纯文本
（`str` / `None` / 空 / 全文本 part）**原样返回同一对象**。

### 落点必须在 `token_count` 之前（架构裁定 T1）

降级插在 `compose` 的 `token_count` 计算**之前**。若挪到之后，报出的 token 数含图、
实际发出的 prompt 已无图，**budget 与 compact 会基于错误的数字判断**。
两层测试各钉一次：
`tests/unit/test_purpose_image_policy.py::test_token_count_excludes_image_tokens_for_downgraded_purposes`
与端到端的
`tests/integration/test_multimodal_end_to_end.py::test_compact_assembly_carries_no_image_while_act_still_does`
（两处变异体「把降级挪到 token_count 之后」均被杀死）。

**策略为什么不能放 gateway**：`LLMRequest` **没有 `purpose` 字段**（已核实），
gateway 无从区分；composer 知道 purpose。这与 rehydrate 落 gateway 并不矛盾——
rehydrate 是 purpose-无关的形态还原，降级是 purpose-相关的策略。

### 降级占位必须逐字节确定性（用户裁定 D2 的附带硬约束）

占位文本对同一张图**必须恒定**：`[image {media_type}]`，
**不得含 blob sha / 随机 id / 时间戳 / 计数器**；同一条消息里多张图共用同一占位、
**不加序号**。`rehydrate_content` 取不到图时的
`[image unavailable: {media_type}]` 受同一条约束。

理由（controller 核实、用户认可）：五种 purpose 各自发送**不同的 tools 集合**，而
tools 排在缓存前缀最前面，所以各 purpose 之间本就从不共享缓存条目——降级伤不到别的
purpose。但若占位文本每次不同，**该 purpose 自己的前缀每次都变**，会砸掉它自己的
自动前缀缓存；而 compact 恰在上下文超预算时触发，正是最需要命中缓存的时刻。

（核实附注：本仓当前**未启用** Anthropic 显式缓存——全仓无 `cache_control` /
`anthropic-beta` 头，只有 DeepSeek 方言读 `prompt_cache_hit_tokens` 做统计。
故此约束今日不影响行为，是为将来启用时的正确性兜底。变异「占位掺 sha」杀死 9 条测试。）

### ⚠️ compact 的两套口径（设计意图，不是 bug）

`_active_memory_tokens`（`core/loop/steps/compact.py:205`）读的是 **memory 记录**、
**不经 composer**，因此 compact 的**折叠层级判断仍按「含图」口径**（memory 里图还在，
每张按 `_IMAGE_PART_TOKENS = 1600` 计）；而 compact **自身发出去的 prompt 已不含图**
（经上表降级）。

**两套数字并存是设计意图**：折叠判断要反映「memory 里实际压着多少东西」（图还在那儿，
Phase 4 的 L0.5 才会真正折走它），而 prompt 只需要文字。**明写在此以免后来者把它当
bug「修」成一套口径**——统一到「不含图」会让折叠层级低估 memory 的真实体积，
统一到「含图」会让 compact 自己的 prompt 白白带上图（正是本 Phase 刚消除的问题）。

### ⚠️ 冻结判据对 dict 形态**文本** part 的误判（Task 4 实测）

§13 上方已记「dict 形态 part 会被 `image_part_count` 全数计成图片」。Task 4 实测把它
钉成逐字确凿的一对数字：

```
image_part_count([{"type": "text", "text": "hello world"}])  ->  1    # 被计成 1600 token
content_to_text([{"type": "text", "text": "hello world"}])   ->  ''   # 摘要器完全看不见
```

即：**同一个 dict 形态的纯文本 part，在 token 估算里被当成图片多算 1600，在拍扁成
文本时又被当成图片跳过、渲染成空串。** 两个盲点同源（同一判据
`not hasattr(p, "text")`），方向相反，叠加起来是「既多算 token 又丢内容」。

**判据仍冻结**（改它必须同时改 `core/content.py::_is_text_part` /
`core/utils.py::content_to_text` / `core/utils.py::image_part_count` 三处，
`test_content_module.py::test_content_to_text_reexported` 还把 `content_to_text` 钉死为
同一对象引用）。此处只是把盲点写明。

**这个盲点正是 Task 3 与 Task 4 在 dict 形态上刻意分歧的原因**，两者都是对的：

- `rehydrate_content`（Task 3）：**dict 进 dict 出**——它只是把 ref 换回 base64，形态
  不变，adapter 的 dict 分支照常工作。若改成「判不出形态就降级成占位」，会把**今天
  工作正常的 dict 形态 base64 图片**也一并降级，对已接 BlobStore 的宿主构成新的能力回退。
- `downgrade_images_to_text`（Task 4）：**dict 进、dataclass `TextPart` 出**——因为它
  把图**转成文本**，若回吐 dict 文本，占位会被摘要器渲染成空串（信息白留）**且**仍被
  计成 1600 token，本任务的两个目的双双落空。
- 两者判 dict 形态是不是图片都走显式的 `part.get("type") == "image"`（与两家 adapter
  的 `_parts_to_blocks` 一致），**不套冻结判据**——dict 上永远取不到 `.text`，套冻结
  判据会把 `{"type":"text","text":"hi"}` 换成 `[image image]`，属实打实的内容损坏。

### ⚠️ observe 看不到图：本次降级面里唯一的真实功能回退风险

裁定 D2 原话是「observe 也不是很需要看图」，实现照做无误。但若某个 task 的成败判据
**就是**「图里有没有那个东西」（例如 actor 的产出本身是一张渲染图），observer 只会
看到 `[image image/png]`，只能靠 actor 的文字自述来判定。

四个被降级的 purpose 里，只有 `observe` / `background_observe` 存在这个风险
（`compact` 产出恒为纯文本、`recognize_intent` 只填元数据）。缓解手段已就位：改
`_IMAGE_BEARING_PURPOSES` 一处即可放开（见上表）。Phase 4 的 `media:get_image` 落地后
还有第二条路——让 observer 主动取回它需要看的那张图。
