# 双 blob store 解耦 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `MemoryBlobStore` 与 `EventBlobStore` 各自只为自己那一侧服务——删除入口双写，两侧从同一份原始字节各自 put、各自拿 ref，core 不再假设两个 ref 相同。

**Architecture:** 三处改动闭合整个链路。(1) `normalize_content` 退回纯 memory 侧，删掉第二次 put 与 ref 比对；(2) `content_to_event_jsonable` 改为只接受**归一化之前**的原始 content，自己把字节 put 进 event blob，不再依赖「ref part 原样透传」；(3) 事件 payload 里恒为 event ref，重放之后在恢复路径上做一次 `event_blob.get → normalize_content` 转回 memory ref。task 另挂一份 event jsonable，使 `reopen_task` 发 `TASK_REQUEUED` 时零 blob IO。

**Tech Stack:** Python 3.11 / asyncio / SQLAlchemy async / pytest + pytest-asyncio

**Spec:** `docs/superpowers/specs/2026-08-27-dual-blob-store-design.md`（本方案**推翻其 §5「入口双写」与 §3「两边 sha 口径必须逐字节一致」**，其余各节仍然有效；§5.1 当初被以成本为由否决的备选，正是本方案采用的方向）

## Global Constraints

- **两个契约保持独立、类型无关。** 不引入公共基类 `BlobStore`，不让 `EventBlobStore` 成为 `MemoryBlobStore` 的子类型。形状相似是实现层可以偷懒的**基础**，不是协议层可以合并的**理由**。
- **core 不得假设两个 store 返回同一个 ref。** 任何「memory ref 拿去 event 侧解」或反之的代码都是缺陷。测试桩必须用**互不相同的 ref 方案**来钉死这一条。
- **事件库恒不含字节。** 事件 payload 里只能出现 `blob:<...>` ref 或文本，永不出现 base64。
- **纯文本路径逐字节不变。** `str` / 无图内容在所有新增分支之前返回，零额外 IO。
- **不接 blob 的宿主行为不变。** `can_externalize` 为 False 时原样返回同一对象。
- **占位文本逐字节确定。** 不得含随机 id / 时间戳 / 计数器（裁定 D2）。
- ref 前缀一律取 `protocols.context.BLOB_REF_PREFIX`，不硬编码 `"blob:"`。
- 注释与 docstring 用中文，与仓内既有风格一致。

---

### Task 1: `normalize_content` 退回纯 memory 侧

删掉入口双写。这是整个耦合的源头。

**Files:**
- Modify: `src/ctx_weft/core/content.py:449-529`（`normalize_content` 签名与循环体）
- Modify: `src/ctx_weft/core/runtime.py:578-611`（`_validate_and_normalize_content` 不再传 event store 给 normalize）
- Test: `tests/unit/test_normalize_content.py`

**Interfaces:**
- Produces: `async def normalize_content(content, *, blob_store, ctx) -> str | list[ContentPart]` —— **不再有 `event_blob_store` 参数**。行为：`base64` 图 → `blob_store.put` → `ImagePart(data=ref, source_type="ref", byte_size=len(raw))`；文本 / `url` / 已是 `ref` → 原样；`blob_store.can_externalize` 为 False → 原样返回同一对象。

- [ ] **Step 1: 写失败测试——normalize 不碰 event store**

```python
# tests/unit/test_normalize_content.py
import base64
import pytest
from ctx_weft.core.content import normalize_content
from ctx_weft.protocols import BLOB_REF_PREFIX, ImagePart, ProviderContext

_PNG = base64.b64encode(b"\x89PNG_fake_bytes").decode()


class _RecordingStore:
    """记录 put 次数的 memory blob 桩。"""

    can_externalize = True

    def __init__(self) -> None:
        self.puts: list[bytes] = []

    async def put(self, data, media_type, ctx):
        self.puts.append(data)
        return f"{BLOB_REF_PREFIX}mem-{len(self.puts)}"

    async def get(self, ref, ctx):
        return None


@pytest.mark.asyncio
async def test_normalize_content_takes_no_event_blob_store():
    """normalize_content 只认 memory 侧——多传 event_blob_store 必须 TypeError。"""
    store = _RecordingStore()
    content = [ImagePart(data=_PNG, media_type="image/png")]
    with pytest.raises(TypeError):
        await normalize_content(
            content,
            blob_store=store,
            event_blob_store=store,        # 已删除的参数
            ctx=ProviderContext(session_id="s1"),
        )


@pytest.mark.asyncio
async def test_normalize_content_puts_once_into_memory_only():
    """一张图只 put 一次，产出的 ref 就是 memory store 给的那个。"""
    store = _RecordingStore()
    out = await normalize_content(
        [ImagePart(data=_PNG, media_type="image/png")],
        blob_store=store,
        ctx=ProviderContext(session_id="s1"),
    )
    assert len(store.puts) == 1
    assert out[0].data == f"{BLOB_REF_PREFIX}mem-1"
    assert out[0].source_type == "ref"
    assert out[0].byte_size == len(base64.b64decode(_PNG))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/unit/test_normalize_content.py::test_normalize_content_takes_no_event_blob_store -v`
Expected: FAIL —— 当前签名仍接受 `event_blob_store`，不抛 `TypeError`。

- [ ] **Step 3: 改实现**

把 `src/ctx_weft/core/content.py` 的 `normalize_content` 签名改成：

```python
async def normalize_content(
    content: "str | list[ContentPart] | None",
    *,
    blob_store: "Any",
    ctx: "Any",
) -> "str | list[ContentPart] | None":
```

删掉循环体里从 `# 双写（spec §5）` 到 `logger.error(...)` 收尾的整段（原 `content.py:498-522`），只留：

```python
        raw = base64.b64decode(getattr(part, "data", "") or "", validate=True)
        ref = await blob_store.put(raw, getattr(part, "media_type", ""), ctx)
        # byte_size 必须在这里记下来（Phase 3c Task D）：外部化之后 data 是
        # "blob:<sha>"（长度恒约 69），体积信息就此丢失，而 image_tokens 是同步的、
        # 不能回 MemoryBlobStore 做 IO 取回来。这是最后一个还握着 raw bytes 的地方。
        out.append(dataclasses.replace(
            part, data=ref, source_type="ref", byte_size=len(raw)))
```

同时把函数 docstring 里关于双写的整段（「双写（spec §5）：…」与「⚠️ 函数开头的短路对 event 侧同样生效…」）替换为：

```
    **只写 memory 侧。** event 侧的外部化由 `content_to_event_jsonable` 在事件发射点
    独立完成，两者从同一份原始 content 各自取字节、各自 put、各自拿 ref，**core 不假设
    两个 ref 相同**（两个契约独立，ref 相同只在 host 偷懒用同一实例时才成立，那是实现
    层的巧合，不是协议层的前提）。
```

`runtime.py:606-611` 的调用去掉 `event_blob_store=event_blob_store,` 一行（`event_blob_store` 局部变量仍被 `validate_content` 门控使用，保留）。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/unit/test_normalize_content.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/content.py src/ctx_weft/core/runtime.py tests/unit/test_normalize_content.py
git commit -m "refactor(blob): normalize_content 退回纯 memory 侧——删除入口双写"
```

---

### Task 2: `content_to_event_jsonable` 只接受原始 content

切断「ref part 原样透传」，让 event 侧自给自足。

**Files:**
- Modify: `src/ctx_weft/core/content.py:150-225`
- Test: `tests/unit/test_event_content_externalization.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `normalize_content`（签名已无 event 参数）
- Produces: `async def content_to_event_jsonable(content, *, event_blob_store, ctx) -> str | list[dict] | None` —— 签名不变，**语义变**：`base64` 图 → `event_blob_store.put` → ref；`url` / 未知 → `[image {media_type}]` 文本占位（不变）；**`source_type == "ref"` 的图 → 降级成 `[image {media_type}]` 文本占位并记 `logger.warning`**，因为一个已经是 ref 的 part 意味着调用方喂进来的是归一化之后的内容——那份字节属于 memory 侧，event 侧无权也无法解读。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_event_content_externalization.py
import base64
import pytest
from ctx_weft.core.content import content_to_event_jsonable
from ctx_weft.protocols import BLOB_REF_PREFIX, ImagePart, ProviderContext, TextPart

_PNG = base64.b64encode(b"\x89PNG_fake_bytes").decode()


class _EventStore:
    """event 侧桩。**刻意用与 memory 侧完全不同的 ref 方案**——core 若还偷偷假设
    两边 ref 相同，任何一条链路都会立刻暴露。"""

    can_externalize = True

    def __init__(self) -> None:
        self.blobs: dict[str, tuple[bytes, str]] = {}

    async def put(self, data, media_type, ctx):
        ref = f"{BLOB_REF_PREFIX}evt-{len(self.blobs) + 1}"
        self.blobs[ref] = (data, media_type)
        return ref

    async def get(self, ref, ctx):
        return self.blobs.get(ref)


@pytest.mark.asyncio
async def test_base64_part_goes_into_event_store():
    store = _EventStore()
    out = await content_to_event_jsonable(
        [TextPart(text="hi"), ImagePart(data=_PNG, media_type="image/png")],
        event_blob_store=store,
        ctx=ProviderContext(session_id="s1"),
    )
    assert out[0] == {"type": "text", "text": "hi"}
    assert out[1]["data"] == f"{BLOB_REF_PREFIX}evt-1"
    assert out[1]["source_type"] == "ref"
    assert store.blobs[f"{BLOB_REF_PREFIX}evt-1"][0] == base64.b64decode(_PNG)


@pytest.mark.asyncio
async def test_foreign_ref_part_is_demoted_not_passed_through(caplog):
    """喂进来一个 memory ref = 调用方给错了内容。降级成占位，绝不透传。

    透传会让事件 payload 里出现一个 event store 解不开的 ref——正是本次解耦要消灭的
    跨命名空间引用。
    """
    store = _EventStore()
    out = await content_to_event_jsonable(
        [ImagePart(data=f"{BLOB_REF_PREFIX}mem-1", media_type="image/png",
                   source_type="ref")],
        event_blob_store=store,
        ctx=ProviderContext(session_id="s1"),
    )
    assert out == [{"type": "text", "text": "[image image/png]"}]
    assert store.blobs == {}
    assert "ref" in caplog.text.lower()


@pytest.mark.asyncio
async def test_plain_text_is_zero_cost():
    store = _EventStore()
    assert await content_to_event_jsonable(
        "纯文本", event_blob_store=store, ctx=ProviderContext(session_id="s1")) == "纯文本"
    assert store.blobs == {}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/unit/test_event_content_externalization.py -v`
Expected: `test_foreign_ref_part_is_demoted_not_passed_through` FAIL —— 当前实现原样透传 ref part，产出的是 image dict 而非文本占位。

- [ ] **Step 3: 改实现**

`content.py` 的 `content_to_event_jsonable` 循环体中，把

```python
        if source_type == "ref":
            prepared.append(part)                                    # ref → 原样，不重复 put
```

改成

```python
        if source_type == "ref":
            # 已是 ref = 调用方喂的是**归一化之后**的内容，那份字节属于 memory 侧，
            # event 侧既无权解读、也解不开（两个契约独立，ref 命名空间互不相通）。
            # 透传会在事件 payload 里留下一个 event store 永远打不开的 ref，故降级。
            # 正确的喂法是把**归一化之前**的原始 content 递进来（见 Task 3）。
            media_type = str(_part_field(part, "media_type", "") or "") or "image"
            logger.warning(
                "content_to_event_jsonable 收到 source_type='ref' 的图片 part（ref=%r）："
                "调用方应递入归一化之前的原始 content。本 part 已降级为文本占位。",
                _part_field(part, "data", ""),
            )
            prepared.append(TextPart(text=_IMAGE_PLACEHOLDER_TMPL.format(
                media_type=media_type)))
```

并把 docstring 里「**ref part 原样**：入口双写已保证 event store 持有这份字节」那条整体换成上述新语义的说明，同时删掉 docstring 中所有提到「入口双写」的句子。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/unit/test_event_content_externalization.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/content.py tests/unit/test_event_content_externalization.py
git commit -m "refactor(blob): event 外部化拒收外来 ref——不再依赖入口双写"
```

---

### Task 3: 三个入口改喂原始 content

让 event 侧在发射点拿到字节。

**Files:**
- Modify: `src/ctx_weft/core/runtime.py:578-611`（`_validate_and_normalize_content` 同时产出两份）
- Modify: `src/ctx_weft/core/runtime.py:826-831`、`:926-940`（两个 session 入口）
- Modify: `src/ctx_weft/core/orchestrator/session_manager.py:87-92`、`:163-168`
- Modify: `src/ctx_weft/core/orchestrator/task_manager.py:290-300`
- Modify: `src/ctx_weft/core/orchestrator/hitl_manager.py:425-445`
- Test: `tests/integration/test_multimodal_end_to_end.py`

**Interfaces:**
- Consumes: Task 1 的 `normalize_content`、Task 2 的 `content_to_event_jsonable`
- Produces: `Runtime._validate_and_normalize_content(content, session_id, *, tenant_id) -> tuple[str | list[ContentPart], str | list[dict] | None]` —— 返回 `(normalized_for_memory, event_jsonable)`。两个产物都由**同一份原始 content** 派生，各自只碰一个 store。`SessionManager.create_session` / `resume_session` / `TaskManager.push_task` / `HitlManager` 的应答发射点改为接收现成的 `event_jsonable`，**不再自己调 `content_to_event_jsonable`**。

- [ ] **Step 1: 写失败测试——两侧 ref 不同也能各自工作**

```python
# tests/integration/test_multimodal_end_to_end.py 追加
@pytest.mark.asyncio
async def test_event_refs_and_memory_refs_are_independent(runtime_with_images):
    """两个 store 用互不相同的 ref 方案：memory 记录与事件 payload 各自可解。

    这是解耦的验收条件——今天双写让两边 ref 恰好相同，任何隐藏的跨命名空间引用
    都被掩盖；ref 方案一分开，掩盖不住。
    """
    runtime, mem_store, evt_store = runtime_with_images
    session = await runtime.start_session(_params_with_image())

    events = await runtime.event_store.read_by_session(session.id)
    created = next(e for e in events if e.type == "SessionCreated")
    parts = created.payload["user_prompt"]
    img = next(p for p in parts if p["type"] == "image")
    # 事件里的 ref 归 event store，且必须真能取回字节
    assert img["source_type"] == "ref"
    assert await evt_store.get(img["data"], _ctx()) is not None
    # 且它**不是** memory 侧的 ref
    assert await mem_store.get(img["data"], _ctx()) is None
    # 事件 payload 恒不含字节
    assert _PNG not in json.dumps(created.payload)
```

（`runtime_with_images` fixture 需在同文件内新建：注册一个 sha256 方案的 memory store 与一个 `blob:evt-<n>` 方案的 event store，两者是**不同实例、不同 ref 方案**。`_params_with_image` / `_ctx` 沿用文件内既有 helper。）

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/integration/test_multimodal_end_to_end.py::test_event_refs_and_memory_refs_are_independent -v`
Expected: FAIL —— 当前事件里的 ref 来自 memory store（入口双写产出），`evt_store.get` 返回 None。

- [ ] **Step 3: 改实现**

`runtime.py` 的 `_validate_and_normalize_content` 改为：

```python
    async def _validate_and_normalize_content(
        self, content, session_id: str, *, tenant_id: str = "default",
    ) -> "tuple[str | list[ContentPart], str | list[dict] | None]":
        """三入口共用的校验 + 双侧外部化。返回 `(memory 侧归一化内容, event 侧 jsonable)`。

        两个产物都从**同一份原始 content** 派生，各自只碰一个 store：memory 侧走
        `normalize_content`，event 侧走 `content_to_event_jsonable`。两个 ref 不必
        相同，core 也不比较它们——契约独立（见 2026-08-28 解耦方案 Global Constraints）。

        顺序恒为 validate → 两侧外部化：被拒的内容不该在任何 store 留垃圾。
        """
        from ctx_weft.core.content import (
            content_to_event_jsonable, normalize_content, validate_content,
        )

        event_blob_store = self.providers.get_event_blob_store()
        validate_content(content, event_blob_store=event_blob_store)
        ctx = ProviderContext(session_id=session_id, tenant_id=tenant_id)
        # event 侧先做：它要的是**原始**字节，必须在 normalize 改写 part 之前取。
        event_jsonable = await content_to_event_jsonable(
            content, event_blob_store=event_blob_store, ctx=ctx)
        blob_store = self.providers.get_memory_blob_store()
        if not blob_store.can_externalize:
            return content, event_jsonable
        normalized = await normalize_content(content, blob_store=blob_store, ctx=ctx)
        return normalized, event_jsonable
```

三个调用点相应解包（`runtime.py:830`、`:926`、`:673`），并把 `event_jsonable` 顺着已有的参数链传下去：

- `SessionManager` 新增 `user_prompt_event_jsonable` 参数（`create_session` / `resume_session`），把 `session_manager.py:87-92` 与 `:163-168` 那两段 `await content_to_event_jsonable(...)` **整段删掉**，payload 直接用传进来的值。
- `TaskManager.push_task` 同理新增参数，删掉 `task_manager.py:295` 的调用。
- `HitlManager`：`_normalize_hitl_content` 回调（`runtime.py:645-676`）改为返回二元组，`hitl_manager.py:425-445` 用返回的 `event_jsonable` 直接发事件，删掉自己那次 `content_to_event_jsonable`。`set_event_blob_store_resolver` 与 `_event_blob_store_resolver` 随之删除（不再有调用方）。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/integration/test_multimodal_end_to_end.py tests/unit/test_hitl_multimodal_validation.py tests/unit/test_multimodal_entry.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/runtime.py src/ctx_weft/core/orchestrator/ tests/integration/test_multimodal_end_to_end.py
git commit -m "refactor(blob): 三入口从原始 content 分别产出 memory/event 两份外部化结果"
```

---

### Task 4: 恢复路径把 event ref 转回 memory ref

事件 payload 里恒为 event ref；重放出来的内容要进 memory，必须先过桥。

**Files:**
- Modify: `src/ctx_weft/core/content.py`（新增 `hydrate_event_content`）
- Modify: `src/ctx_weft/core/runtime.py:1310`（`_recover_session_locked`）
- Test: `tests/integration/test_media_fold_replay_e2e.py`

**Interfaces:**
- Consumes: Task 2 的事件外部化产物（event ref）
- Produces: `async def hydrate_event_content(content, *, event_blob_store, ctx) -> str | list[ContentPart] | None` —— 把 event ref 图还原成 `source_type="base64"` 的 `ImagePart`（`byte_size` 一并填上）。取不回字节 → `[image unavailable: {media_type}]` 文本占位（复用既有 `_unavailable_part`），**不抛**。`can_externalize` 为 False、`str` / `None` / 无 ref → 原样返回同一对象。

- [ ] **Step 1: 写失败测试**

```python
# tests/integration/test_media_fold_replay_e2e.py 追加
@pytest.mark.asyncio
async def test_recovery_converts_event_refs_into_memory_refs(runtime_with_images):
    """崩溃恢复后 task.user_prompt 必须是 **memory** ref——它会被 driver ingest 进 memory。

    重放直接拿到的是 event ref；不转换就等于把一个 memory 解不开的 ref 落进记忆。
    """
    runtime, mem_store, evt_store = runtime_with_images
    session = await runtime.start_session(_params_with_image())
    await runtime.recover_session(session.id)

    task = runtime.get_task_manager(session.id).get_task(_root_task_id(session))
    img = next(p for p in task.user_prompt if getattr(p, "source_type", "") == "ref")
    assert await mem_store.get(img.data, _ctx()) is not None      # memory 解得开
    assert await evt_store.get(img.data, _ctx()) is None          # 不是 event 的 ref
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/integration/test_media_fold_replay_e2e.py::test_recovery_converts_event_refs_into_memory_refs -v`
Expected: FAIL —— `task.user_prompt` 里是 event ref，`mem_store.get` 返回 None。

- [ ] **Step 3: 写 `hydrate_event_content`**

在 `content.py` 的 `rehydrate_content` 旁边新增（两者同为「ref → 字节」，但**取自不同的 store、服务不同的下游**，故不合并）：

```python
async def hydrate_event_content(
    content: "str | list[ContentPart] | None",
    *,
    event_blob_store: "Any",
    ctx: "Any",
) -> "str | list[ContentPart] | None":
    """把事件里的 event ref 还原成 base64 part，供恢复路径重新走 memory 侧归一化。

    这是两个 blob 世界之间**唯一**的桥，且方向单一：event → 字节 → 调用方自己决定
    要不要再 put 进 memory。桥架在恢复路径这个交界处、由 event 侧发起，而不是藏在
    memory 的写路径里替 event 代劳（那正是本次解耦拆掉的入口双写）。

    与 `rehydrate_content` 的分工：那个取 `MemoryBlobStore`、产出给 adapter 拼 wire
    payload；本函数取 `EventBlobStore`、产出给 `normalize_content` 重新落 memory。
    两者取的 store 不同、ref 命名空间不同，**不可互换**。

    取不回字节 → `[image unavailable: {media_type}]` 文本占位，**不抛**：event blob
    的保留策略归 host（spec §9），取不到是预期内的正常降级，不该让恢复整个失败。
    """
    if not content or isinstance(content, str):
        return content
    if not event_blob_store.can_externalize:
        return content
    from ctx_weft.protocols import ImagePart
    out: list[Any] = []
    for part in content:
        if not _is_ref_part(part):
            out.append(part)
            continue
        ref = str(_part_field(part, "data", "") or "")
        media_type = str(_part_field(part, "media_type", "") or "")
        got = await event_blob_store.get(ref, ctx)
        if got is None:
            logger.warning("hydrate_event_content: event blob 取不回 %r，降级为占位", ref)
            out.append(_unavailable_part(part, media_type))
            continue
        raw, got_media_type = got
        out.append(ImagePart(
            data=base64.b64encode(raw).decode(),
            media_type=media_type or got_media_type,
            source_type="base64",
            byte_size=len(raw),
        ))
    return out
```

`_is_ref_part` 复用 `content.py:551` 既有判据（该函数已同时认 `source_type == "ref"` 与 `data` 以 `blob:` 开头）。

- [ ] **Step 4: 接进恢复路径**

`runtime.py:1310` 那行之后插入：

```python
        all_tasks = [task_from_projection(tp) for tp in view.tasks.values()]
        # 重放出来的 prompt 带的是 **event ref**（事件 payload 的口径），而它下游要被
        # driver ingest 进 memory。两个 ref 命名空间互不相通，故必须在此过桥：
        # event_blob 取字节 → memory 侧重新归一化。每一步只碰一个 store。
        await self._restore_task_prompts(all_tasks, session_id, sess_proj.tenant_id)
```

并新增方法：

```python
    async def _restore_task_prompts(
        self, tasks: "list[Task]", session_id: str, tenant_id: str,
    ) -> None:
        """恢复态的 prompt 从 event ref 转回 memory ref。**逐 task 独立降级，不整体失败。**

        转换前先把事件侧的原样形态快照到 `user_prompt_event_jsonable`（见 Task 5）：
        `reopen_task` 要用它发 TASK_REQUEUED，此时它就是从事件里读来的那一份，
        零成本、且与首次发射逐字节相同。
        """
        from ctx_weft.core.content import (
            content_to_jsonable, hydrate_event_content, normalize_content,
        )

        event_blob_store = self.providers.get_event_blob_store()
        blob_store = self.providers.get_memory_blob_store()
        ctx = ProviderContext(session_id=session_id, tenant_id=tenant_id)
        for task in tasks:
            for field_name in ("user_prompt", "original_user_prompt"):
                content = getattr(task, field_name)
                if not content or isinstance(content, str):
                    continue
                setattr(task, f"{field_name}_event_jsonable", content_to_jsonable(content))
                hydrated = await hydrate_event_content(
                    content, event_blob_store=event_blob_store, ctx=ctx)
                if blob_store.can_externalize:
                    hydrated = await normalize_content(
                        hydrated, blob_store=blob_store, ctx=ctx)
                setattr(task, field_name, hydrated)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `pytest tests/integration/test_media_fold_replay_e2e.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/core/content.py src/ctx_weft/core/runtime.py tests/integration/test_media_fold_replay_e2e.py
git commit -m "feat(blob): 恢复路径把 event ref 过桥转回 memory ref"
```

---

### Task 5: task 挂 event jsonable，reopen 零 blob IO

`reopen_task` 不引入任何新图片，故不该为它付一次外部化。

**Files:**
- Modify: `src/ctx_weft/core/state/models.py:176`（`Task` 新增两个字段）
- Modify: `src/ctx_weft/core/orchestrator/task_manager.py:552-618`
- Test: `tests/unit/test_task_reopen_multimodal.py`（新建）

**Interfaces:**
- Consumes: Task 3 的 `push_task(user_prompt_event_jsonable=...)`、Task 4 的 `_restore_task_prompts`
- Produces: `Task.user_prompt_event_jsonable: str | list[dict] | None = None` 与 `Task.original_user_prompt_event_jsonable: str | list[dict] | None = None`。**纯瞬态、不进 `TaskProjection`、不进快照**——恢复时由 `_restore_task_prompts` 从事件 payload 直接重填（那本来就是事件侧的原样形态），故不需要第二条持久化路径。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_task_reopen_multimodal.py
import pytest
from ctx_weft.protocols import BLOB_REF_PREFIX


class _ExplodingEventStore:
    """reopen 期间任何 put 都是缺陷——reopen 不可能引入新图片。"""

    can_externalize = True

    async def put(self, data, media_type, ctx):
        raise AssertionError("reopen_task 不该调用 event blob put")

    async def get(self, ref, ctx):
        raise AssertionError("reopen_task 不该调用 event blob get")


@pytest.mark.asyncio
async def test_reopen_reuses_carried_event_jsonable(task_manager_with_image_task):
    """TASK_REQUEUED 的 payload 由 task 上挂的 event jsonable + 追加文本拼出，零 blob IO。"""
    tm, task_id, emitted = task_manager_with_image_task
    tm.set_event_blob_store(_ExplodingEventStore())

    assert await tm.reopen_task(task_id, reason="重做") is True

    requeued = next(e for e in emitted if e.type == "TaskRequeued")
    img = next(p for p in requeued.payload["user_prompt"] if p["type"] == "image")
    assert img["data"].startswith(f"{BLOB_REF_PREFIX}evt-")   # 仍是首次发射的那个 event ref
    tail = requeued.payload["user_prompt"][-1]
    assert tail["type"] == "text" and "重做" in tail["text"]
```

（`task_manager_with_image_task` fixture 新建于同文件：用 Task 3 的新签名 `push_task(..., user_prompt_event_jsonable=[{"type": "image", "data": "blob:evt-1", ...}])` 造一个携图 task，并收集 emit 出的事件。）

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/unit/test_task_reopen_multimodal.py -v`
Expected: FAIL —— 当前 `reopen_task` 调 `content_to_event_jsonable`，桩抛 `AssertionError`。

- [ ] **Step 3: 改实现**

`models.py` 的 `Task` 加两个字段：

```python
    # 事件侧的 prompt 形态（event ref，`content_to_event_jsonable` 的产物）。
    # **纯瞬态**：不进 TaskProjection / 快照——恢复时由 Runtime._restore_task_prompts
    # 从事件 payload 直接重填，那本来就是这份数据的原样形态，没必要再持久化第二遍。
    # reopen_task 据此发 TASK_REQUEUED，零 blob IO：reopen 只追加文本，不可能引入新图。
    user_prompt_event_jsonable: "str | list[dict] | None" = None
    original_user_prompt_event_jsonable: "str | list[dict] | None" = None
```

`task_manager.py` 的 `reopen_task`：`original_user_prompt` 首次快照那段同步快照事件形态——

```python
        if task.original_user_prompt is None:
            task.original_user_prompt = task.user_prompt or ""
            task.original_user_prompt_event_jsonable = task.user_prompt_event_jsonable
```

把 `:604-609` 的两次 `await content_to_event_jsonable(...)` 换成纯结构拼接：

```python
        # reopen 只在 prompt 尾部追加**文本** section（见上方 new_prompt 构造），
        # 不可能引入事件流没见过的图。故事件形态直接由首次发射那份 + 文本拼出，
        # 零 blob IO，且同一张图的 event ref 跨 reopen 逐字节相同（重放确定性）。
        original_user_prompt_jsonable = task.original_user_prompt_event_jsonable
        user_prompt_jsonable = _append_text_sections(
            original_user_prompt_jsonable, sections)
        task.user_prompt_event_jsonable = user_prompt_jsonable
```

同文件新增模块级 helper：

```python
def _append_text_sections(
    jsonable: "str | list[dict] | None", sections: "list[str]",
) -> "str | list[dict] | None":
    """把 reopen 的文本 section 追加到事件侧 jsonable 尾部，与 `content_with_suffix`
    对 content 的处理同构（str 直接接、list 追加 TextPart dict）。"""
    if not sections:
        return jsonable
    suffix = "".join(f"\n\n{sec}" for sec in sections)
    if jsonable is None or isinstance(jsonable, str):
        return (jsonable or "") + suffix
    return [*jsonable, {"type": "text", "text": suffix}]
```

`set_event_blob_store` / `self._event_blob_store` 在 `reopen_task` 里不再被用到；`push_task` 若已按 Task 3 改为接收现成 jsonable，则整个 `TaskManager` 都不再需要 event blob store——一并删除 `set_event_blob_store`、`_event_blob_store`、`_event_ctx` 以及 `session_manager.py:211` / `runtime.py:1016` 的两处调用点。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/unit/test_task_reopen_multimodal.py tests/unit/test_task_manager.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/state/models.py src/ctx_weft/core/orchestrator/task_manager.py tests/unit/test_task_reopen_multimodal.py
git commit -m "feat(blob): task 挂 event jsonable——reopen 发事件零 blob IO"
```

---

### Task 6: 示例 blob provider

给宿主一份可直接用、也可直接抄的实现，并**在代码里示范「偷懒」的正确形态**。

**Files:**
- Create: `src/ctx_weft/providers/blob_fs/__init__.py`
- Create: `src/ctx_weft/providers/blob_fs/store.py`
- Test: `tests/unit/test_blob_fs_store.py`

**Interfaces:**
- Produces: `class FsBlobStore(MemoryBlobStore, EventBlobStore)` —— 文件系统内容寻址实现。`__init__(self, root: Path, *, grace_period: timedelta = timedelta(hours=24))`。`put` 写 `root/<sha[:2]>/<sha>`（sidecar `.meta` 存 media_type），幂等且刷新 mtime；`get` 读回 `(bytes, media_type)`，缺文件返回 `None`。`collect(live_refs: set[str], *, now=None) -> int` 供 host 自行定义活性后回收。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_blob_fs_store.py
import pytest
from ctx_weft.protocols import (
    BLOB_REF_PREFIX, EventBlobStore, MemoryBlobStore, ProviderContext,
)
from ctx_weft.providers.blob_fs import FsBlobStore


def _ctx():
    return ProviderContext(session_id="s1")


def test_one_class_satisfies_both_contracts(tmp_path):
    """两个契约独立定义、形状相似 → 实现时可以偷懒，一个类同时满足、注册两次。"""
    store = FsBlobStore(tmp_path)
    assert isinstance(store, MemoryBlobStore)
    assert isinstance(store, EventBlobStore)
    assert store.can_externalize is True


@pytest.mark.asyncio
async def test_put_is_content_addressed_and_idempotent(tmp_path):
    store = FsBlobStore(tmp_path)
    ref1 = await store.put(b"same-bytes", "image/png", _ctx())
    ref2 = await store.put(b"same-bytes", "image/png", _ctx())
    assert ref1 == ref2
    assert ref1.startswith(BLOB_REF_PREFIX)
    assert await store.get(ref1, _ctx()) == (b"same-bytes", "image/png")


@pytest.mark.asyncio
async def test_get_returns_none_and_never_raises(tmp_path):
    store = FsBlobStore(tmp_path)
    assert await store.get(f"{BLOB_REF_PREFIX}deadbeef", _ctx()) is None
    assert await store.get("http://example.com/x.png", _ctx()) is None   # 非 blob: 前缀
    assert await store.get(BLOB_REF_PREFIX, _ctx()) is None              # 空 sha
    assert await store.get(f"{BLOB_REF_PREFIX}../../etc/passwd", _ctx()) is None


@pytest.mark.asyncio
async def test_separate_instances_are_truly_independent(tmp_path):
    """分开部署时两个实例互不可见——core 不得依赖任何一侧解得开对方的 ref。"""
    mem = FsBlobStore(tmp_path / "mem")
    evt = FsBlobStore(tmp_path / "evt")
    ref = await mem.put(b"only-in-memory", "image/png", _ctx())
    assert await evt.get(ref, _ctx()) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/unit/test_blob_fs_store.py -v`
Expected: FAIL with `ModuleNotFoundError: ctx_weft.providers.blob_fs`

- [ ] **Step 3: 实现**

`store.py` 要点（完整实现按下述骨架补齐）：

```python
class FsBlobStore(MemoryBlobStore, EventBlobStore):
    """文件系统内容寻址 blob 实现，**同时满足两个契约**——这正是「实现可以偷懒」的形态。

    ⚠️ 一个类满足两个契约，不等于两个契约可以合并。它们各自定义、类型无关，语义会
    各自演进（最明显的是回收锚点：memory 侧是记录 is_superseded，event 侧是事件保留
    策略）。本类只是**碰巧**两边都能用。

    ⚠️ **共用一个实例时，`collect` 的 live_refs 必须同时含两侧的活引用**（spec §9）。
    只喂 memory 侧的活引用会删掉事件流仍需要的字节。这是共用实现自身的责任，core
    不代管——想省心就分开部署两个实例，各按各的策略回收。

    路径安全：sha 经 `_safe_sha` 校验（仅 64 位十六进制）后才拼进路径，`..` / 绝对路径
    / UNC 一律在此被拒，`get` 返回 None。
    """
```

`put`：`sha = hashlib.sha256(data).hexdigest()` → 目标目录 `root/sha[:2]/` → 已存在则 `os.utime` 刷新 mtime（语义同 `SqlMemoryProvider.put`：「最后一次有人声称要用它」），否则**先写临时文件再 `os.replace`**（原子落盘，避免半截文件被读到）→ 同名 `.meta` 写 media_type → 返回 `f"{BLOB_REF_PREFIX}{sha}"`。所有阻塞 IO 经 `asyncio.to_thread` 执行。

`get`：前缀检查 → `_safe_sha` 校验 → 读文件与 `.meta`，缺失返回 `None`；`media_type` 缺失回落 `"application/octet-stream"`。

`collect(live_refs, *, now=None)`：遍历 `root` 下所有 blob 文件，删除「不在 `live_refs` 中」且「mtime 早于 `now - grace_period`」的，返回删除数。宽限期理由与 `SqlMemoryProvider.collect_blobs` 同（put→ingest 之间必然存在未被引用的窗口）。

`__init__.py` 只 `from .store import FsBlobStore` 并 `__all__ = ["FsBlobStore"]`，与 `providers/memory_sql/__init__.py` 同风格。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/unit/test_blob_fs_store.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/providers/blob_fs/ tests/unit/test_blob_fs_store.py
git commit -m "feat(providers): 新增 FsBlobStore 示例——一个类满足两个独立契约"
```

---

### Task 7: 收口——协议措辞、spec、README

代码里的跨命名空间假设已经拆干净，文档里的还在。

**Files:**
- Modify: `src/ctx_weft/protocols/events.py:299-320`（`EventBlobStore` docstring）
- Modify: `src/ctx_weft/providers/memory_sql/provider.py:264-280`（类 docstring）
- Modify: `docs/superpowers/specs/2026-08-27-dual-blob-store-design.md`（§3 / §5 / §5.1 / §6）
- Modify: `README.md`

- [ ] **Step 1: 删掉协议里的跨命名空间要求**

`protocols/events.py` 的 `EventBlobStore` docstring 里，把

> **ref 前缀取自 `protocols.context.BLOB_REF_PREFIX`**，与 memory 侧同一个常量——内容寻址的 sha 口径两边必须逐字节一致，入口双写才能得到同一个 ref。

改为

> **ref 前缀取自 `protocols.context.BLOB_REF_PREFIX`**，与 memory 侧同一个常量——但**仅此而已**：两侧的 ref 是**两个独立的命名空间**，core 从不比较、也从不拿一侧的 ref 去另一侧解。host 用同一实例时两个 ref 恰好相同，那是实现层的巧合，不是任何代码可以依赖的前提。

- [ ] **Step 2: 更新 spec**

在 spec 顶部状态行下追加：

```markdown
> ⚠️ **§3 的「两边 sha 口径必须逐字节一致」与 §5「入口双写」已于 2026-08-28 被推翻**，
> 见 `docs/superpowers/plans/2026-08-28-blob-store-decoupling.md`。§5.1 当初以成本为由
> 否决的「各自 put + 跨界搬运」正是现行方案：跨界只发生在恢复路径这一个交界处，由
> event 侧发起（`hydrate_event_content`），memory 的写路径不再替 event 代劳。
> 其余各节（§4 注册面、§7 严格门控、§8 条件可见、§9 生命周期归 host）仍然有效。
```

- [ ] **Step 3: 更新 `SqlMemoryProvider` 类 docstring**

【为什么也继承 EventBlobStore】那段保留（理由不变：让本仓自带实现能过 §7 门控），但补一句：

```
    ⚠️ 它同时满足两个契约，**不代表 core 可以假设两边 ref 相同**。解耦后（2026-08-28）
    memory 与 event 的 ref 是两个独立命名空间；本类共用一张 memory_blobs 表使它们碰巧
    一致，那是本实现的选择。共用时 `collect_blobs` 只看 memory 侧活引用，会删掉事件流
    仍需要的字节——分开部署（如两个 `FsBlobStore` 实例）可回避该陷阱。
```

- [ ] **Step 4: README 迁移说明**

在既有 blob 段落追加：宿主自 2026-08-28 起可分开注册两个实现；`providers/blob_fs/FsBlobStore` 是可直接用的示例；共用一个实例仍受 `collect_blobs` 陷阱影响。

- [ ] **Step 5: 全量回归**

Run: `pytest tests/ -x -q`
Expected: PASS（重点看 7 个携图测试文件与 `test_sql_blob_store.py`）

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/protocols/events.py src/ctx_weft/providers/memory_sql/provider.py docs/ README.md
git commit -m "docs(blob): 收口解耦——删除协议与文档里的跨命名空间假设"
```

---

## 遗留与已知取舍

- **恢复路径新增 async blob IO**（Task 4）。这是 spec §5.1 以「恢复路径此前完全不碰 blob」为由否决过的代价，本次明确接受：它是冷路径，换来的是 core 不再依赖任何跨实现假设。
- **`_restore_task_prompts` 逐 task 串行**。恢复态 task 数通常个位数，暂不并发；若实测成为瓶颈，改 `asyncio.gather` 即可（各 task 之间无共享状态）。
- **event blob 取不回时恢复降级为占位**，图在恢复后的 memory 里永久变成文本。这与 spec §9「字节能否取回是 host 的存储策略问题」一致，core 不兜底。
