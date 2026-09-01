# HITL 契约两维化 + 多模态出口贯通 · 设计

> 状态：设计已批准（2026-09-01），待实施
> 相关：`2026-08-29-authz-hitl-layering-design.md` —— 已实施。本设计是其 D3/D4 的**延续与订正**：
> 那次把 `HitlRequest` / `Authorizer` 提到 protocols 时是**逐字上抬**（`2bc7295`），`status` 的词汇
> 与开闭方向没有跟着重审。
> 相关：`2026-08-20-multimodal-design.md` §3 / `2026-08-27-dual-blob-store-design.md` §6 ——
> 本设计把 HITL 这条入口的**出口侧**补齐，两侧 ref 命名空间独立这条前提不变。
> 受影响的权威 spec：`docs/spec/05-authz-and-hitl.md`（§1 / §3 状态机 / §5 跨语言清单）、
> `docs/spec/07-hitl-suspend-resume.md`（§7 park 管线）。

---

## 1. 问题

四件事咬在同一条链上，一起改比分四次改省，且其中两件互为前提（见 P3）。

### P1 · `status` 的词汇是 approval 长出来的，开闭方向是反的

`HitlStatus = Literal["pending","accepted","rejected","cancelled"]`。三个终态词是按「批准一次
工具调用」造的，但同一个字段要服务三种 form —— 对 `question`「accepted」意思是「人答了」，
对 `wait`「accepted」意思是「人发了条消息」，都不是「接受」了什么。

词汇比 form 的三分更老：form 的三值是 spec 2026-07-05 引入的（替代旧 `kind` + `capability_id`
sentinel 拼判），而 `status` 从那之前就在。`2bc7295` 把两者一起搬进 protocols 时，只放宽了
`HitlForm`（`Literal` → `str`），`status` 原样照搬。

于是**开放的是 core 不解释的那一维，封闭的是 host 最可能想扩展的那一维**：host 可以定义
`form="form_fill"`，却只能用 accepted/rejected/cancelled 描述它的结局。

代码里 `status` 的真实读者分布（全仓）：

| 读者 | 处数 | 读什么 |
|------|------|--------|
| `HitlManager` 内部 | 6（`:104 :175 :346 :354 :419 :453`） | **只比 `"pending"`** —— 它只需要一个 bool |
| `human.py:47` | 1 | `.accepted` —— 该属性的唯一消费者 |
| `control_capability.py:729` / `runtime.py:1770` | 2 | `== "rejected"` —— 只用来挑前缀字符串 |
| `"cancelled"` | **0** | 只在 `hitl_manager.py:251` 被写，从未被读 |

四值枚举承载的实际信息量是两个正交的 bit，却挤在一个字段里。

附带一处文档错误：`protocols/hitl.py` 模块 docstring 称「`status` 是闭集 —— 新增状态会破坏
reducer 投影」。**因果反了**：reducer 不消费 `status`，reducer **生产**它（`fold_pending_hitl`
只认 EventType；`fold_cold_hitl_decision` 按 EventType 反推 status）。真正封闭的是 `EventType`
—— `_emit`（`hitl_manager.py:465`）对它做运行期校验并抛 `ValueError`；`Literal` 运行期零约束。

### P2 · 三个 form 的多模态出口只通了一个

入口侧是好的：`set_content_normalizer` 一个注入点覆盖 `answer`/`approve`/`reject`/`cancel`
四条 resolve 路径，顺序恒为 validate → 双侧外部化，且元组赋值在 `await` 之后 —— 校验抛错时
`req.message` 未改、事件未发、状态未推进。事件侧载荷顺参数递进 `_resolve`，不拿已归一化的
`req.message` 回算。这些都不动。

出口侧三条路各行其是：

| form | 出口 | 图片命运 |
|------|------|----------|
| `wait` | `runtime._inject_user_reply:1769-1791`，`content_with_prefix` + `ingest(content=parts)` | **完整保留** |
| `question` | `control_capability.py:728` `content_to_text(approval.message)`，且 `metadata` 硬写 `{}` | **静默丢弃** |
| `approval` | `human.py:50,54` `content_to_text(approval.message)` | **静默丢弃** |

`content_to_text`（`core/utils.py:134-148`）的实现是 `if hasattr(item, "text")` 才收 —— 非文本
part 直接跳过，**连占位都不留**。它本是给「估 token / 取摘要输入」用的纯文本渲染器，被借来
当出口渲染器了。

三条连带后果：

1. `question` 恰恰是「向人要信息」的那个 form。人贴张截图回答 `ask_user`，模型收不到，也不会
   知道有东西丢了。
2. `_normalize_hitl_content` 照样把图 `put` 进 memory blob store 并换成 ref，但没有任何 memory
   记录引用它 → 永远不出现在 `live_blob_refs` → 宽限期一过被回收。写盘、算 sha、解 tenant
   （`_tenant_for_session` 是冷路径、要读事件日志）全是纯浪费。
3. 通道就在旁边：`CONTENT_PARTS_KEY`（`capability_gateway.py:263`）会把 `metadata` 里的 part
   拼成 `[TextPart(text), *parts]`。`media:get_image` 走的正是它，`ask_user` 走的也正是同一个
   `yield CapabilityEvent(kind="result")` —— 只是没接。

### P3 · 冷恢复还原的是 event 侧 ref（P2 的前提）

`fold_cold_hitl_decision`（`reducers.py:131-139`）用 `content_from_jsonable(p["message"])` 还原
`req.message`，而事件 payload 里存的是 **event 侧的 ref**（`EventBlobStore` 的命名空间，与
memory 侧互不相通 —— 这是 `2026-08-27-dual-blob-store-design.md` 的既定前提）。

这个 req 经 `find_resolved_for_tool_call` 交给 `human.py` 与 `control_capability.py`。**今天没事，
正因为这两处都把图展平了** —— 即 P2 的 bug 正在遮蔽 P3 的雷。

所以 P2 不能单独修：直接把 `content_to_text` 换成透传 parts，冷恢复路径会立刻把一个 memory
侧永远打不开的 ref 写进工具结果，再随 `TOOL_RESULT` 落进 memory。**P2 与 P3 必须同批。**

### P4 · 两处文档与代码不符（其一有风险）

- `protocols/hitl.py:67` 称 `modified_arguments`「暂仅记录，**不生效**」。实际经
  `human.py:51` → `AuthorizationDecision.modified_arguments` → `capability_gateway.py:201`
  `effective_args = decision.modified_arguments if ... is not None else arguments` → `_coerce_args`
  → provider。**它生效，且直接决定工具拿到什么参数。** 这条注释会让人以为人工改参是安全的
  空操作。
- `hitl_manager._normalize_message` docstring 称未注入 normalizer 时「携图内容则**响亮抛错**」。
  实测四种输入：合法 base64 / 白名单外 media_type / 超 5 MiB 三种抛 `NotImplementedError`
  （均来自 `NullEventBlobStore.put`，**不是**格式校验 —— 白名单与尺寸上限在这条路上一次都
  没跑），而 `source_type="ref"` **不抛**，静默降级成 `[image {mt}]` + 一条 warning。

---

## 2. 目标

1. `HitlRequest` 的状态两维化，开闭方向与 `form` 对齐。
2. 三个 form 的多模态出口行为一致：图能进 prompt，进不去时留占位、不静默。
3. 冷恢复路径不把 event 侧 ref 泄进 memory 侧。
4. 把 `Authorizer` 这个 host 扩展点的挂起语义收回协议内（`defer`），不要求 host 抛 core 内部异常。
5. **事件 payload / 快照格式零变更** —— 旧日志、旧快照、golden 全部原样可读。

## 3. 不做什么

- 不动 `HitlManager` 的机制（幂等、热冷分流、三层决定缓存、锁粒度、`_gc_resolved`）。这些读下来是完整自洽的。
- 不动入口侧归一（`set_content_normalizer` → `_validate_and_normalize_content`）。
- 不动 `HitlPark` 本身，也不动 `ask_user` 的异常 park 路线（见 D2 的理由）。
- 不动事件类型、payload 形状、reducer 的折叠语义。
- 不改 `HitlManager` 未接线时的兜底策略（P4 只订正文档，行为另议）。

---

## 4. D1 · `status` → 单字段 `outcome` + 两个推导属性

```python
# protocols/hitl.py
HitlOutcome = str                       # 开放，与 HitlForm 对称
HITL_OUTCOME_ACCEPTED  = "accepted"
HITL_OUTCOME_REJECTED  = "rejected"
HITL_OUTCOME_CANCELLED = "cancelled"

@dataclass
class HitlRequest:
    ...
    outcome: HitlOutcome = ""           # "" = 未决；非空 = 终局（host 可自定义非空值）

    @property
    def resolved(self) -> bool:
        return bool(self.outcome)

    @property
    def accepted(self) -> bool:         # approval 便利属性；唯一消费者 human.py
        return self.outcome == HITL_OUTCOME_ACCEPTED

    def resolve(self, outcome: HitlOutcome) -> None:
        """终局的唯一写入点。"""
        self.outcome = outcome
```

`HitlStatus` 删除，**不留 `status` 兼容属性** —— 只读别名会让人继续按旧词汇写，而 host 本轮
反正要改。

**为什么 `resolved` 是推导属性而不是存储字段。** 两个字段就有一条要维护的不变量
（`resolved is False` ⟺ `outcome == ""`），而它可以被推导。推导掉之后不变量不可能被违反 ——
不存在「写了 `outcome` 忘了置 `resolved`」这种静默失败，而那种失败的后果是
`find_resolved_for_tool_call` 返回 `None`、把已答过的问题重新问一遍、丢掉用户已给的回复
（正是 `fold_cold_hitl_decision` 整个存在的理由）。语义一分不少：`resolved` 给 manager，
`outcome` 给外部读语义。

**`""` 作哨兵。** host 自定义 outcome 不得用空串，写进 docstring。

### 落点

| 处 | 现 | 改 |
|----|----|----|
| `hitl_manager.py:104 :175 :346 :354 :419 :453` | `status == / != "pending"` | `req.resolved` / `not req.resolved` |
| `hitl_manager._resolve` 形参 | `status: HitlStatus` | `outcome: HitlOutcome`，锁内 `req.resolve(outcome)` |
| `resolve_answer` / `resolve_approve` | `"accepted"` | `HITL_OUTCOME_ACCEPTED` |
| `resolve_reject` | `"rejected"` | `HITL_OUTCOME_REJECTED` |
| `cancel` | `"cancelled"` | `HITL_OUTCOME_CANCELLED` |
| `control_capability.py:729`、`runtime.py:1770` | `status == "rejected"` | `outcome == HITL_OUTCOME_REJECTED` |
| `reducers.fold_cold_hitl_decision:132-139` | `req.status, req.message = ...` | `req.resolve(...)` + `req.message = ...` |
| `protocols/__init__.py:62,148` | 导出 `HitlStatus` | 导出 `HitlOutcome` + 三常量 |

`fold_pending_hitl` 无需改（构造出的就是 `outcome=""` 的未决态）。
`serialize_view` / `deserialize_view` 的 `pending_hitl` **本来就不序列化 status**
（`reducers.py:244-250` / `319-327`），**无快照迁移**。

---

## 5. D2 · 启用 `defer`，并给 authorizer 一条不抛异常的等待

### 定性（订正）

`defer` 不是「被绕过的接缝」。`docs/spec/07-hitl-suspend-resume.md` §7 同时设计了两条入口，
并明说是「同一套基础设施……合并实现」：

- **`HitlPark`** —— 卸载一个**活协程**：协程悬在 `await` 上，必须靠异常把调用栈干净 unwind。
- **`defer`** —— 一次**新的** `authorize()` 当场判定「已挂起，别 invoke」：没有栈要 unwind，
  返回值就够。

spec/07 的落地清单把 defer 列为「增（**若做** approval 冷路径）」—— 条件项。代码实际选的冷路径
方案是「`request()` 幂等命中 pending 时补一个新 future 再热等」（`hitl_manager.py:106-108`），
不是 defer。所以 `defer` 至今没有写入者（全仓唯一写它的是 `tests/unit/test_hitl_park.py:57`
的桩），但它是**为一个换了实现方案的场景预留的字段**，不是设计失误。

### 为什么仍然要启用它

`Authorizer` 是 **protocols 层的 host 扩展点**。host 自实现一个想挂起的 authorizer，目前唯一
的办法是抛 `ctx_weft.core.loop.park.HitlPark` —— 一个 core 内部的 `BaseException`。`defer` 是
协议自己文档化的方式（spec/05 §1 已写明）。让内置 authorizer 走 `defer`，是让它成为契约的
**范例**而不是反例。

`ask_user` 的 park 路线保持异常不变：它是 `ToolCapabilityProvider`，够不着 `defer` 那个接缝，
且 `_stream_tool` 只 catch `asyncio.CancelledError`，`HitlPark` 作为 `BaseException` 照常穿过。
这是 spec/07 §7「从 authorizer / control-provider 一路上抛」里 control-provider 那一半，不改。

### 落点

`providers/authorizer/human.py` 不应 import `core.loop.park`（把 provider 拽进 core 的 loop
内部）。改为在 manager 上开一个不抛的等待入口：

```python
# HitlManager
async def wait_for_decision(self, hitl_id: str) -> HitlRequest | None:
    """等到应答；热→冷驱逐返回 None，不抛。"""

async def wait(self, hitl_id: str) -> HitlRequest:
    """wait_for_decision 之上的薄层：None → raise HitlPark。ask_user 走这条。"""
```

```python
# human.py
approval = await self.hitl_manager.wait_for_decision(hitl_id)
if approval is None:
    return AuthorizationDecision(allowed=False, defer=True)
```

gateway 不改：`:185` 判 `defer` 已在 `:189` 判 `allowed` 之前；`allowed=False, defer=True` 的
组合与字段注释「不放行也不拒绝」一致，最终仍由 gateway 抛 `HitlPark(tool_call_id=...)`。

**行为等价性**：已核实 `HitlPark` 携带的 `hitl_id` / `tool_call_id` 两个 handler
（`act.py:419`、`runtime.py:2086`）**都不读** —— 它们只把 task 置 `SUSPENDED`。转换无损。

### 顺带：删除 `Authorizer.filter()`

`filter` 在 src 里**零调用点**（自身文档称「保留给装配期/外部用」）。它调 `authorize()` 只看
`.allowed` —— 对 `HumanConfirmationAuthorizer` 会**真的发出一个 HITL 请求并等人**，是个陷阱。
删掉；等真需要装配期可见性过滤时，按那时的语义重写（届时它必须显式排除会挂起的 authorizer）。

---

## 6. D3 · 三个 form 的多模态出口统一

### 共享拆分器

provider 侧要按 gateway 的 `CONTENT_PARTS_KEY` 契约回传，文本与 part 必须分开。新增到
`core/content.py`：

```python
def split_for_tool_result(
    content: "str | list[ContentPart] | None",
) -> "tuple[str, list[ContentPart]]":
    """拆成 (文本, 非文本 part)。str / None 进 → (原值 or "", [])，纯文本路径零开销。

    判据 `not hasattr(p, "text")` 与 `utils.content_to_text` / `image_part_count` 同源
    （spec 2026-08-20 §13 冻结），不在此另写一份。
    """
```

### question（ask_user）

`control_capability.py:727-733`：

```python
msg = approval.message
if approval.outcome == HITL_OUTCOME_REJECTED:
    content = content_with_prefix(msg, "Human declined: ") if msg else "Human rejected the request."
else:
    content = msg or result.content
text, parts = split_for_tool_result(content)
payload = {"content": text, "metadata": {CONTENT_PARTS_KEY: parts} if parts else {}}
yield CapabilityEvent(kind="result", payload=payload)
```

`CONTENT_PARTS_KEY` 按 `core/media/capability.py:418` 的先例做**惰性 import** ——
`core.orchestrator` 对 `core.loop` 的模块级引用会成环。

拒绝分支改用 `content_with_prefix` 而非 f-string：对 str 是逐字节原样，对 parts 会把前缀并进
首个 `TextPart`（`content.py:78-97`），图不丢。

### approval

`AuthorizationDecision.message: "str | list[ContentPart]" = ""`。

`human.py` 的两处 `content_to_text(approval.message)` 直接删掉、透传 `approval.message`。
`allow.py` 的两个 `deny_message` 是静态 str，不受影响。

gateway 两处拼接改走既有的 `content_with_prefix` / `content_with_suffix`：

- `:192` 拒绝 →
  `content_with_suffix(content_with_prefix(decision.message, "[Blocked by human: "), "]")`，
  结果交给 `_error_and_record`（它已经能收 `str | list[ContentPart]` 并原样 ingest）。
- `:258-267` 放行备注 → 文本部分照旧前置进 `text`，备注里的图片 part 与工具结果的 part 一起
  进最终 content：

```python
note_text, note_parts = split_for_tool_result(decision.message)
if note_text or note_parts:
    text = f"[Human note: {note_text}]\n{text}"
content: "str | list[ContentPart]" = text
parts = metadata.get(CONTENT_PARTS_KEY)
parts = list(parts) if isinstance(parts, (list, tuple)) else []
if note_parts or parts:
    content = normalize_content_parts([TextPart(text=text), *note_parts, *parts])
```

**顺序裁定**：备注图在工具结果图**之前** —— 与文本顺序一致（`[Human note: …]` 也在工具输出
之前）。

`decision.message` 为纯 str 且无 parts 时，`content` 仍是同一个 str，纯文本路径逐字节不变。

### wait

不动。

---

## 7. D4 · 冷恢复的 event → memory ref 转换

`fold_cold_hitl_decision` 是 `reducers` 里的**纯函数**，不得引入 blob IO。转换放在边界上 ——
`runtime._cold_hitl_decision`（`set_cold_decision_lookup` 绑的 async handler，`runtime.py:1925`）：

```python
req = fold_cold_hitl_decision(events, tool_call_id)
if req is None or not req.message or isinstance(req.message, str):
    return req                                   # 纯文本零成本直通
tenant = await self._tenant_for_session(session_id)
ctx = ProviderContext(session_id=session_id, tenant_id=tenant)
try:
    hydrated = await hydrate_event_content(
        req.message, event_blob_store=self.providers.get_event_blob_store(), ctx=ctx)
    bs = self.providers.get_memory_blob_store()
    req.message = (await normalize_content(hydrated, blob_store=bs, ctx=ctx)
                   if bs.can_externalize else hydrated)
except Exception:
    logger.error("冷 HITL 决定：event ref 转换失败，降级为文本占位 (tool_call=%s)",
                 tool_call_id, exc_info=True)
    req.message = downgrade_images_to_text(req.message)
return req
```

失败姿态与 `runtime._restore_task_prompts`（`runtime.py:1455-1513`）完全一致：**绝不让解不开的
ref 流下去**，降级成确定性占位并 `logger.error` —— 崩溃恢复是最不能再崩一次的地方，但也是最
不能静默的地方。

`put` 与「工具结果被 `_record_result` ingest」之间的窗口，正是 `FsBlobStore.collect` 宽限期
（`store.py:139-151` 判据二）存在的理由，不需要额外处理。

---

## 8. D5 · 文档订正

1. `protocols/hitl.py:67` `modified_arguments` —— 删掉「暂仅记录，不生效」，写明生效路径
   （`human.py:51` → `capability_gateway.py:201` → `_coerce_args` → provider）。
2. `protocols/hitl.py` 模块 docstring —— 「`status` 是闭集」改为：封闭的是 `EventType`
   （`_emit` 运行期校验），`outcome` 是它的**有损投影**且值域开放；5 个 resolve EventType
   映到 3 个内建 outcome。
3. `hitl_manager._normalize_message` docstring —— 订正「携图必炸」：`source_type="ref"` 不抛；
   三种会抛的情形抛的都是 `NullEventBlobStore.put` 的 `NotImplementedError`，**不是**格式
   校验（白名单与 5 MiB 上限在裸路径上一次都没跑，当前的「响亮」是巧合而非设计）。

---

## 9. 破坏性变更汇总（写进 README 升级须知）

| # | 变更 | host 影响 |
|---|------|-----------|
| 1 | `HitlRequest.status` 删除 → `outcome` 字段 + `resolved` / `accepted` 属性 | 读 status 处全改；`/hitl/pending` 若序列化 status 要改字段名 |
| 2 | `HitlStatus` 移出 protocols 导出；新增 `HitlOutcome` + `HITL_OUTCOME_*` 三常量 | import 改 |
| 3 | `AuthorizationDecision.message` 放宽为 `str \| list[ContentPart]` | 自实现 Authorizer **写** str 不受影响；**读** `decision.message` 的要处理 parts |
| 4 | `Authorizer.filter()` 删除 | 用过的自行实现（并注意排除会挂起的 authorizer） |
| 5 | `HitlManager.wait_for_decision()` 新增；`wait()` 语义不变 | 无（纯新增） |
| 6 | `ask_user` / approval 的工具结果可能是 `list[ContentPart]` | host 无感（core 内部；`InvocationResult.content` 早已声明为联合类型） |

**不变**：事件类型、事件 payload 形状、`RunSnapshot` 序列化、golden 数据。旧日志与旧快照
原样可读。

---

## 10. 测试

### 迁移

7 个文件、42 处 status/accepted 断言：`test_hitl.py`、`test_hitl_cold_decision.py`、
`test_hitl_cold_resume.py`、`test_hitl_form_extensible.py`、`test_hitl_multimodal_validation.py`、
`test_hitl_request_model.py`、`test_hitl_request_parked.py`。

### 新增（每条配一个「纯文本逐字节不变」的对照）

- `outcome == ""` ⟺ `resolved is False` 的推导性质，含 host 自定义 outcome 值
- `ask_user` 带图端到端：provider → `CONTENT_PARTS_KEY` → gateway → memory 记录里有 `ImagePart`，
  且 `collect_blob_refs` 认得它（结构化 ref，无需显式声明）
- `ask_user` 拒绝 + 带图：`Human declined: ` 并进首个 `TextPart`，图不丢
- 冷恢复转换：伪造一条带 **event ref** 的 `HITL_ANSWERED`，断言 `_cold_hitl_decision` 返回的
  message 里是 **memory ref**；event blob 取不回时降级成 `[image {mt}]` 且记 `logger.error`
- `defer` 路径：`wait_for_decision` 返回 `None` → `decision.defer is True` → gateway **不调**
  `provider.invoke`（复用 `test_hitl_park.py:27` 的结构）
- approval 备注带图：`[Human note: …]` 文本在、`ImagePart` 在、顺序为 note 图在前工具图在后
- approval 拒绝带图：`[Blocked by human: …]` 前后缀都并进文本 part，图不丢
- 事件零迁移：用**改造前**形状的 payload 喂 `fold_cold_hitl_decision`，仍折出正确 outcome

### 变异验证

每条新增测试都必须在把对应修复抽掉后转红，实测记录进实施计划。

---

## 11. 风险

| 风险 | 评估 |
|------|------|
| `status` 迁移漏改一处 | `HitlStatus` 被删除 + `status` 字段消失 → 漏改处是 `AttributeError`，不会静默。**这是删掉兼容属性的主要收益。** |
| `_cold_hitl_decision` 新增 blob IO 拖慢冷路径 | 只在 `req.message` 是 parts 时触发（纯文本 `isinstance(str)` 直通）。冷决定查询本就已在读事件日志，量级相当。 |
| `decision.message` 改联合类型后，host 自实现的 authorizer 读它时崩 | 属破坏性变更 #3，写进升级须知。core 内所有读点（gateway 两处）都改走 `content_with_prefix/suffix`，它们对 str 是恒等。 |
| 删 `Authorizer.filter()` 影响未知 host | 零内部调用点；spec/05 §5 清单里列了它，需同步删除该行。 |
| `ask_user` 结果从 str 变 list 影响下游 | `InvocationResult.content` 早已是 `str \| list[ContentPart]`，`_record_result` 已用 `redact_content_for_event` 处理联合类型（`capability_gateway.py:411-415`）。风险面已被 `media:get_image` 那条路趟过。 |

---

## 12. 与已落改动的关系

工作区已有的 `compact.py` `blob_refs=placeholder_refs(...)` 修复（L0.5 占位随 task 层坍缩幸存
时的引用边缺失）与本设计**独立、不冲突**。本设计让 HITL 带回的图进入 memory，那些是
`source_type="ref"` 的**结构化** part，`collect_blob_refs` 的第一条判据（`extract_blob_refs`）
本来就认得，不需要额外声明。
