# 多模态 Phase 0：token 估算图片感知 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让全仓五个 token 估算点把 `ImagePart` 计入，同时保证纯文本会话的估算值与改造前逐字节相同。

**Architecture:** 在 `utils.py` 新增 `image_tokens(content)`——只统计非文本 part，对 `str` 输入恒返 0。五个估算点改成「既有的文本计数 + `image_tokens(content)`」。刻意**不**直接套用既有的 `estimate_content_tokens`：它无条件加 `_MSG_FRAMING_TOKENS = 4`，对纯文本非恒等，会静默移动 budget 裁剪与 compact 升级的阈值。

**Tech Stack:** Python 3.11+，pytest + pytest-asyncio，`HeuristicTokenizer` 作测试用 tokenizer。

**Spec:** `docs/superpowers/specs/2026-08-20-multimodal-design.md`（§6.5、§11），
子设计 `docs/superpowers/specs/2026-08-20-image-fold-replay-design.md`（§2）

## Global Constraints

- **纯文本行为逐字节不变。** 每个任务都必须有一条断言证明：`str` 输入下新旧估算值相等。这是本 Phase 唯一的硬约束，任何任务违反它即为失败。
- `_IMAGE_PART_TOKENS = 1600` 保持单一真源，定义在 `src/ctx_weft/core/utils.py`，不得在别处复制该常量。
- 「非文本 part」的判据统一为 `not hasattr(p, "text")`，与既有 `content_to_text`（`utils.py:145-147`）和 `estimate_content_tokens`（`utils.py:179`）一致，不得改用 `isinstance(p, ImagePart)`（会与 duck-type 的测试桩不兼容）。
- 测试运行器：`uv run pytest`（若无 uv 则 `python -m pytest`）。测试根目录 `tests/`。
- 本 Phase **不**引入任何新的多模态功能，不改任何签名的类型标注，不碰 `providers/llm/*`。

---

### Task 1: `utils.image_tokens` 与 `estimate_content_tokens` 复用

**Files:**
- Modify: `src/ctx_weft/core/utils.py:168-181`
- Test: `tests/unit/test_image_tokens.py`（新建）

**Interfaces:**
- Consumes: 无（本 Phase 的第一个任务）
- Produces: `ctx_weft.core.utils.image_tokens(content: "str | list[ContentPart] | None") -> int`。后续所有任务都 import 它。对 `None` / `""` / `str` 一律返回 `0`；对 part 列表返回 `1600 * 非文本 part 数`。不接受 `count` 回调（图片常数不过 tokenizer）。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_image_tokens.py`：

```python
from ctx_weft.core.utils import estimate_content_tokens, image_tokens
from ctx_weft.protocols import ImagePart, TextPart


def _img():
    return ImagePart(data="ZGF0YQ==", media_type="image/png")


def test_image_tokens_zero_for_plain_text():
    assert image_tokens("hello world") == 0


def test_image_tokens_zero_for_none_and_empty():
    assert image_tokens(None) == 0
    assert image_tokens("") == 0
    assert image_tokens([]) == 0


def test_image_tokens_zero_for_text_parts_only():
    assert image_tokens([TextPart(text="a"), TextPart(text="b")]) == 0


def test_image_tokens_counts_each_image():
    assert image_tokens([_img()]) == 1600
    assert image_tokens([TextPart(text="a"), _img(), _img()]) == 3200


def test_estimate_content_tokens_unchanged_for_plain_text():
    """重构 estimate_content_tokens 复用 image_tokens 后，纯文本口径必须不变。"""
    count = len  # 确定性计数，隔离 tokenizer 启发式
    assert estimate_content_tokens("hello", count=count) == 4 + 5


def test_estimate_content_tokens_still_counts_images():
    count = len
    got = estimate_content_tokens([TextPart(text="hi"), _img()], count=count)
    assert got == 4 + 2 + 1600
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_image_tokens.py -v`
Expected: FAIL，`ImportError: cannot import name 'image_tokens' from 'ctx_weft.core.utils'`

- [ ] **Step 3: 实现 `image_tokens` 并让 `estimate_content_tokens` 复用它**

在 `src/ctx_weft/core/utils.py` 中，把现有的 `estimate_content_tokens`（第 168-181 行）替换为：

```python
def image_tokens(content: "str | list[ContentPart] | None") -> int:
    """content 中图片（非文本 part）的 token 补偿。不含文本、不含 framing。

    对 str / None / 空一律返回 0——这保证调用方在纯文本路径上是恒等变换，
    可以安全地加在既有的文本计数之后而不改变既有口径。

    刻意不接受 count 回调：图片按固定常数计（见 _IMAGE_PART_TOKENS 的说明），
    不过 tokenizer。
    """
    if not content or isinstance(content, str):
        return 0
    return _IMAGE_PART_TOKENS * sum(1 for p in content if not hasattr(p, "text"))


def estimate_content_tokens(content: "str | list[ContentPart] | None", *, count: Callable[[str], int] | None = None) -> int:
    """一条 content 的估算：文本 + 图片 part 固定常数 + 每条 framing 开销。往大了估。

    None/空同样容错（与 content_to_text 对齐）：契约上 content 应为 str | list，但个别路径
    （旧/导入的 memory 记录、None 工具结果等）可能透传 None，估算期须容错而非迭代 None 崩溃。

    count：文本费率经 count 回调走 tokenizer；None 回退未校准启发式（纯单测/无 llm 场景）。
    framing 常数不过回调。

    注意：本函数带 _MSG_FRAMING_TOKENS 补偿，**不是**纯文本恒等的。只在本就计入
    framing 的路径（prepare / llm_gateway）使用；装配与 compact 的估算点请改用
    「既有文本计数 + image_tokens(content)」，见 spec 2026-08-20-multimodal-design §6.5。
    """
    count = count or estimate_tokens
    return _MSG_FRAMING_TOKENS + count(content_to_text(content)) + image_tokens(content)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_image_tokens.py -v`
Expected: 6 passed

- [ ] **Step 5: 运行既有的估算相关回归**

Run: `uv run pytest tests/unit/test_estimate_tokens.py tests/unit/test_prepare_estimate.py tests/unit/test_heuristic_tokenizer.py -v`
Expected: 全部 PASS（`estimate_content_tokens` 对既有输入口径不变）

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/core/utils.py tests/unit/test_image_tokens.py
git commit -m "feat(utils): image_tokens——只补图片、不碰文本与 framing 的估算项"
```

---

### Task 2: 装配侧两处估算（`_history` block + `composer` 总量）

**Files:**
- Modify: `src/ctx_weft/core/assembler/sources/_history.py:111`
- Modify: `src/ctx_weft/core/assembler/composer.py:399-401`
- Test: `tests/unit/test_history_block_image_tokens.py`（新建）

**Interfaces:**
- Consumes: `ctx_weft.core.utils.image_tokens`（Task 1）
- Produces: 无新接口。副作用是 `ContextBlock.token_estimate` 与 `AssembledPrompt.token_count` 开始计入图片，`PriorityBudgetStrategy` 因而能对图片触发裁剪。

**为什么两处放在一起：** 它们服务同一个消费者（budget 裁剪 + 溢出判定），回归风险同源，一个 reviewer 会一并看。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_history_block_image_tokens.py`：

```python
from datetime import datetime, UTC
from types import SimpleNamespace

from ctx_weft.core.assembler.sources._history import record_to_history_block
from ctx_weft.protocols import ImagePart, MemoryAddress, TextPart
from ctx_weft.protocols.memory import MemoryKind, MemoryRecord, MemoryScope

_BASE = datetime(2026, 8, 1, tzinfo=UTC)


def _record(content):
    return MemoryRecord(
        id="mem_1",
        kind=MemoryKind.CONVERSATION_TURN,
        scope=MemoryScope.TASK,
        address=MemoryAddress(session_id="s", task_id="t1", agent_id="a"),
        content=content,
        timestamp=_BASE,
        role="user",
        metadata={"task_id": "t1"},
    )


def _request():
    return SimpleNamespace(
        token_counter=len,
        task=SimpleNamespace(id="t1"),
    )


def test_plain_text_block_estimate_unchanged():
    """纯文本：估算值 == token_counter(text)，与改造前逐字节相同。"""
    blk = record_to_history_block(
        _record("hello"), "task_conversation", 0,
        request=_request(), current_task_id="t1",
    )
    assert blk.token_estimate == len("hello")


def test_image_part_counted_in_block_estimate():
    content = [TextPart(text="look"), ImagePart(data="ZGF0YQ==", media_type="image/png")]
    blk = record_to_history_block(
        _record(content), "task_conversation", 0,
        request=_request(), current_task_id="t1",
    )
    assert blk.token_estimate == len("look") + 1600


def test_stored_token_count_still_gets_image_supplement():
    """metadata 里存量的 token_count 是改造前写的（纯文本口径），仍须补图片。"""
    rec = _record([TextPart(text="look"), ImagePart(data="ZGF0YQ==", media_type="image/png")])
    rec.metadata["token_count"] = 7
    blk = record_to_history_block(
        rec, "task_conversation", 0, request=_request(), current_task_id="t1",
    )
    assert blk.token_estimate == 7 + 1600
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_history_block_image_tokens.py -v`
Expected: `test_plain_text_block_estimate_unchanged` PASS（改造前行为已正确），
另两条 FAIL —— 断言得到 `len("look")` / `7`，缺 1600

- [ ] **Step 3: 改 `_history.py`**

在 `src/ctx_weft/core/assembler/sources/_history.py` 的 import 段（第 16 行附近，
现为 `from ctx_weft.core.utils import (PROGRESS_SO_FAR_HEADING, content_to_text, generate_id,)`）
加入 `image_tokens`：

```python
from ctx_weft.core.utils import (
    PROGRESS_SO_FAR_HEADING, content_to_text, generate_id, image_tokens,
)
```

把第 111 行：

```python
        token_estimate=record.metadata.get("token_count") or request.token_counter(text),
```

改为：

```python
        # 文本计数沿用既有口径（含存量 metadata['token_count']），图片另行补齐——
        # 存量 token_count 是改造前按纯文本写的，不含图片，故补充项恒须相加。
        token_estimate=(
            (record.metadata.get("token_count") or request.token_counter(text))
            + image_tokens(record.content)
        ),
```

- [ ] **Step 4: 改 `composer.py`**

在 `src/ctx_weft/core/assembler/composer.py` 第 104 行的 import：

```python
from ctx_weft.core.utils import SUBTASKS_REVIEW_HEADING, content_to_text
```

改为：

```python
from ctx_weft.core.utils import SUBTASKS_REVIEW_HEADING, content_to_text, image_tokens
```

把第 399-401 行：

```python
        token_count = request.token_counter(system) + sum(
            request.token_counter(content_to_text(m.content)) for m in messages
        )
```

改为：

```python
        token_count = request.token_counter(system) + sum(
            request.token_counter(content_to_text(m.content)) + image_tokens(m.content)
            for m in messages
        )
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_history_block_image_tokens.py -v`
Expected: 3 passed

- [ ] **Step 6: 运行装配与预算的既有回归**

Run: `uv run pytest tests/unit/test_budget_strategy.py tests/unit/test_assembler_token_counter.py tests/unit/test_assembler_effective_limit.py tests/unit/test_assembler_reconstruction.py tests/unit/test_agent_recall_source.py -v`
Expected: 全部 PASS（纯文本用例的估算值未变）

- [ ] **Step 7: 提交**

```bash
git add src/ctx_weft/core/assembler/sources/_history.py src/ctx_weft/core/assembler/composer.py tests/unit/test_history_block_image_tokens.py
git commit -m "fix(assembler): block 与 prompt 总量估算计入图片，budget 因而能对图片裁剪"
```

---

### Task 3: compact 编排的活跃记忆估算

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/compact.py:196-206`
- Test: `tests/unit/test_active_memory_tokens.py`（在既有文件追加）

**Interfaces:**
- Consumes: `ctx_weft.core.utils.image_tokens`（Task 1）
- Produces: 无新接口。副作用是 `escalating_compact` 的 `_apply` 能测出图片降级带来的 `freed_tokens`——这是后续 Phase 4 的 L0.5 能被编排正确感知的前提。

- [ ] **Step 1: 写失败测试**

在 `tests/unit/test_active_memory_tokens.py` 末尾追加：

```python
async def test_active_tokens_counts_image_parts():
    from ctx_weft.protocols import ImagePart, TextPart
    mem = InMemoryMemoryProvider()
    scope = MemoryAddress(session_id="s", task_id="t1", agent_id="a")
    await mem.ingest(MemoryEvent(type=T.USER_PROMPT, address=scope,
                                 content=[TextPart(text="look"),
                                          ImagePart(data="ZGF0YQ==", media_type="image/png")],
                                 timestamp=_BASE, role="user",
                                 metadata={"task_id": "t1"}), _pctx())
    state = SimpleNamespace(scope=scope, agent=SimpleNamespace())
    ctx = SimpleNamespace(memory=mem, provider_ctx=_pctx(),
                          llm=SimpleNamespace(tokenizer=HeuristicTokenizer()))
    total = await _active_memory_tokens(state, ctx)
    text_only = HeuristicTokenizer().count("look")
    assert total == text_only + 1600


async def test_active_tokens_plain_text_unchanged():
    """纯文本口径不得漂移：值恒等于逐条 tokenizer.count(text) 之和。"""
    mem = InMemoryMemoryProvider()
    scope = MemoryAddress(session_id="s", task_id="t1", agent_id="a")
    for i in range(3):
        await mem.ingest(MemoryEvent(type=T.LLM_RESPONSE, address=scope, content="x" * 400,
                                     timestamp=_BASE + timedelta(seconds=i), role="assistant",
                                     metadata={"task_id": "t1"}), _pctx())
    state = SimpleNamespace(scope=scope, agent=SimpleNamespace())
    ctx = SimpleNamespace(memory=mem, provider_ctx=_pctx(),
                          llm=SimpleNamespace(tokenizer=HeuristicTokenizer()))
    total = await _active_memory_tokens(state, ctx)
    assert total == 3 * HeuristicTokenizer().count("x" * 400)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_active_memory_tokens.py -v`
Expected: `test_active_tokens_counts_image_parts` FAIL（少 1600），
`test_active_tokens_plain_text_unchanged` PASS（改造前已正确，作为守卫）

- [ ] **Step 3: 改 `compact.py`**

在 `src/ctx_weft/core/loop/steps/compact.py` 第 27 行的 import：

```python
from ctx_weft.core.utils import content_to_text, effective_limit, now_utc
```

改为：

```python
from ctx_weft.core.utils import content_to_text, effective_limit, image_tokens, now_utc
```

把第 204-206 行：

```python
    for r in [*body, *agent_recs]:
        text = r.content if isinstance(r.content, str) else content_to_text(r.content)
        total += ctx.llm.tokenizer.count(text)
    return total
```

改为：

```python
    for r in [*body, *agent_recs]:
        text = r.content if isinstance(r.content, str) else content_to_text(r.content)
        # 图片另计：不计入则降级图片的 freed_tokens 恒为 0，escalating_compact 的
        # est 不减、误判该级白跑而继续升级（见 spec 2026-08-20-multimodal-design §6.5）。
        total += ctx.llm.tokenizer.count(text) + image_tokens(r.content)
    return total
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_active_memory_tokens.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 运行 compact 既有回归**

Run: `uv run pytest tests/unit -k "compact or escalat" -v`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/core/loop/steps/compact.py tests/unit/test_active_memory_tokens.py
git commit -m "fix(compact): _active_memory_tokens 计入图片，使降级级的 freed_tokens 可测"
```

---

### Task 4: 短段免折与 short task 两处判定

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/background_observe.py:99-103`
- Modify: `src/ctx_weft/core/loop/steps/finalize.py:415-419`
- Test: `tests/unit/test_segment_scoped_fold.py`（在既有文件的第 2 节末尾追加）
- Test: `tests/unit/test_short_leaf_image_tokens.py`（新建）

**Interfaces:**
- Consumes: `ctx_weft.core.utils.image_tokens`（Task 1）
- Produces: 无新接口。

**为什么两处放在一起：** 语义同族——都是「内容够短 → 免折/免压」，都用「整段拼成一个字符串数一次」的写法，改法完全一致，回归风险同源。

**改法要点：** 两处现在都是先 `" ".join(...)` 再数一次。**不要**改成逐条 `estimate_content_tokens`（那会引入 4×N 的 framing 漂移）。保留 join + 单次 count，另外把各条的 `image_tokens` 求和加上去。

**签名事实（已核实，勿改）：** `is_short_segment(state, ctx)` **没有** `threshold`
参数——阈值取自 `state.agent.loop_config.short_segment_token_threshold`，`<= 0` 时
直接返回 `False`。另外段内 assistant 回合 **≤ 1 条时直接返回 `True`、根本不看 token**
（`background_observe.py:96-98`），所以图片用例必须凑够 **2 条** assistant 回合，
否则单回复门会先短路、测不到 token 口径。

- [ ] **Step 1: 写失败测试（其一）——段免折**

在 `tests/unit/test_segment_scoped_fold.py` 的第 2 节（`is_short_segment` 那一组）
末尾追加。复用该文件既有的 `_seed_two_segments` / `_short_seg_state_ctx` / `_ts` /
`_SCOPE` / `_PCTX` / `MT`，并在文件顶部的 `from ctx_weft.protocols import (...)`
里补上 `ImagePart` 与 `TextPart`：

```python
async def test_is_short_segment_counts_image_parts():
    """当前段文本极短但带图 → 图片 token 使其超阈值，必须判非 short。

    _seed_two_segments 的当前段是 A2a(assistant) + A2b(tool)，只有 1 条 assistant
    回合会命中单回复门直接 True；故再补一条带图的 assistant 回合凑够 2 条。
    """
    mem = InMemoryMemoryProvider()
    await _seed_two_segments(mem)
    await mem.ingest(MemoryEvent(
        type=MT.LLM_RESPONSE, address=_SCOPE,
        content=[TextPart(text="ok"), ImagePart(data="ZGF0YQ==", media_type="image/png")],
        timestamp=_ts(55), role="assistant"), _PCTX)
    state, ctx = _short_seg_state_ctx(mem, threshold=400)

    assert await bo.is_short_segment(state, ctx) is False, \
        "一张图 1600 token 已超阈值 400，不得因图算 0 而误判短段免折"
```

- [ ] **Step 2: 写失败测试（其二）——short leaf**

新建 `tests/unit/test_short_leaf_image_tokens.py`：

```python
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ctx_weft.core.loop.steps.finalize import _is_short_leaf
from ctx_weft.protocols import (
    ImagePart, MemoryAddress, MemoryEvent, MemoryEventType as MT, ProviderContext, TextPart,
)
from ctx_weft.providers.llm.tokenizer import HeuristicTokenizer
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

pytestmark = pytest.mark.asyncio

_PCTX = ProviderContext(session_id="s1", tenant_id="default", task_id="t1", agent_id="a1")
_SCOPE = MemoryAddress(session_id="s1", task_id="t1", agent_id="a1")
_BASE = datetime(2026, 8, 1, tzinfo=UTC)


def _ctx(mem):
    return SimpleNamespace(memory=mem, provider_ctx=_PCTX,
                           llm=SimpleNamespace(tokenizer=HeuristicTokenizer()))


def _loop_config():
    return SimpleNamespace(short_task_turn_cap=10, short_task_token_threshold=400)


async def _seed(mem, contents):
    for i, c in enumerate(contents):
        await mem.ingest(MemoryEvent(
            type=MT.LLM_RESPONSE, address=_SCOPE, content=c,
            timestamp=_BASE + timedelta(seconds=i), role="assistant"), _PCTX)


async def test_short_leaf_true_for_small_text():
    """纯文本短任务仍判 short——改造前行为不得漂移。"""
    mem = InMemoryMemoryProvider()
    await _seed(mem, ["ok", "done"])
    got = await _is_short_leaf(
        mem, _SCOPE, SimpleNamespace(id="t1"), _loop_config(), _ctx(mem), False)
    assert got is True


async def test_short_leaf_false_when_images_exceed_threshold():
    """一张图 1600 token 已超阈值 400，不得因图算 0 而误判 short。"""
    mem = InMemoryMemoryProvider()
    await _seed(mem, ["ok", [TextPart(text="done"),
                             ImagePart(data="ZGF0YQ==", media_type="image/png")]])
    got = await _is_short_leaf(
        mem, _SCOPE, SimpleNamespace(id="t1"), _loop_config(), _ctx(mem), False)
    assert got is False
```

- [ ] **Step 3: 运行两组测试确认失败**

Run: `uv run pytest tests/unit/test_segment_scoped_fold.py::test_is_short_segment_counts_image_parts tests/unit/test_short_leaf_image_tokens.py -v`
Expected: `test_short_leaf_true_for_small_text` PASS（守卫），
另两条 FAIL —— 图算 0 token，均被误判为 short

- [ ] **Step 4: 改 `background_observe.py`**

在 `src/ctx_weft/core/loop/steps/background_observe.py` 第 30 行的 import：

```python
from ctx_weft.core.utils import content_to_text
```

改为：

```python
from ctx_weft.core.utils import content_to_text, image_tokens
```

把第 99-103 行：

```python
    seg_text = " ".join(
        r.content if isinstance(r.content, str) else content_to_text(r.content)
        for r in seg_records
    )
    return ctx.llm.tokenizer.count(seg_text) <= threshold
```

改为：

```python
    seg_text = " ".join(
        r.content if isinstance(r.content, str) else content_to_text(r.content)
        for r in seg_records
    )
    # 保留「join 后数一次」的文本口径（逐条估算会引入 4×N 的 framing 漂移），
    # 图片另行求和补上——不补则图片密集段被误判短段免折、该段 raw 永久保留。
    seg_images = sum(image_tokens(r.content) for r in seg_records)
    return ctx.llm.tokenizer.count(seg_text) + seg_images <= threshold
```

- [ ] **Step 5: 改 `finalize.py`**

在 `src/ctx_weft/core/loop/steps/finalize.py` 的 import 段（第 41 行附近，
现含 `from ctx_weft.core.utils import as_utc, content_to_text`——以源码实际为准）
加入 `image_tokens`：

```python
from ctx_weft.core.utils import as_utc, content_to_text, image_tokens
```

把第 415-419 行：

```python
    text = " ".join(
        content_to_text(r.content) if not isinstance(r.content, str) else r.content
        for r in records
    )
    return ctx.llm.tokenizer.count(text) <= loop_config.short_task_token_threshold
```

改为：

```python
    text = " ".join(
        content_to_text(r.content) if not isinstance(r.content, str) else r.content
        for r in records
    )
    # 同 background_observe.is_short_segment：保留 join 后数一次的文本口径，图片另计。
    images = sum(image_tokens(r.content) for r in records)
    return (ctx.llm.tokenizer.count(text) + images) <= loop_config.short_task_token_threshold
```

- [ ] **Step 6: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_segment_scoped_fold.py::test_is_short_segment_counts_image_parts tests/unit/test_short_leaf_image_tokens.py -v`
Expected: 3 passed

- [ ] **Step 7: 运行段折叠与 finalize 既有回归**

Run: `uv run pytest tests/unit/test_segment_scoped_fold.py -v && uv run pytest tests/unit -k "background_observe or finalize or short" -v`
Expected: 全部 PASS。特别确认 `test_is_short_segment_counts_only_current_segment`
与 `test_is_short_segment_long_current_segment_not_short` 仍通过——它们是纯文本
口径的守卫。

- [ ] **Step 8: 提交**

```bash
git add src/ctx_weft/core/loop/steps/background_observe.py src/ctx_weft/core/loop/steps/finalize.py tests/unit/test_segment_scoped_fold.py tests/unit/test_short_leaf_image_tokens.py
git commit -m "fix(compact): 短段免折与 short task 判定计入图片，避免图片密集段永久保 raw"
```

---

### Task 5: `ContextOverflowError` 文案补图片维度

**Files:**
- Modify: `src/ctx_weft/core/errors.py:76-101`
- Modify: `src/ctx_weft/core/assembler/budget.py:82-89`
- Test: `tests/unit/test_context_overflow_message.py`（新建）

**Interfaces:**
- Consumes: 无（纯文案与一个新的可选构造参数）
- Produces: `ContextOverflowError.__init__` 新增关键字参数 `image_count: int = 0`；实例属性同名。默认 0 时文案与改造前**逐字符相同**。

**为什么需要：** 修完 Task 2 之后，「单条消息塞太多图」会在装配期抛此错误（spec §3 决定不做入口准入，就靠这条兜底）。但现有文案只说「缩短当前消息 / 任务描述」，用户会去删字而不是删图。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_context_overflow_message.py`：

```python
from ctx_weft.core.errors import ContextOverflowError


def test_message_unchanged_when_no_images():
    """image_count 缺省时文案与改造前逐字符相同。"""
    err = ContextOverflowError(
        required=200_000, effective_limit=120_000,
        context_limit=128_000, reserved_output_tokens=8_192,
    )
    msg = str(err)
    assert "请改用更大上下文窗口的模型，或缩短当前消息 / 任务描述。" in msg
    assert "图片" not in msg


def test_message_mentions_images_when_present():
    err = ContextOverflowError(
        required=200_000, effective_limit=120_000,
        context_limit=128_000, reserved_output_tokens=8_192,
        image_count=12,
    )
    msg = str(err)
    assert "12 张图片" in msg
    assert "19200" in msg  # 12 * 1600
    assert err.image_count == 12


def test_explicit_message_still_wins():
    err = ContextOverflowError("custom", context_limit=128_000, image_count=3)
    assert str(err) == "custom"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_context_overflow_message.py -v`
Expected: `test_message_unchanged_when_no_images` PASS，
另两条 FAIL —— `TypeError: unexpected keyword argument 'image_count'`

- [ ] **Step 3: 改 `errors.py`**

把 `src/ctx_weft/core/errors.py` 第 80-101 行替换为：

```python
    def __init__(
        self,
        message: str = "",
        *,
        code: str | None = None,
        required: int = 0,
        effective_limit: int = 0,
        context_limit: int = 0,
        reserved_output_tokens: int = 0,
        image_count: int = 0,
    ) -> None:
        self.required = required
        self.effective_limit = effective_limit
        self.context_limit = context_limit
        self.reserved_output_tokens = reserved_output_tokens
        self.image_count = image_count
        if not message and context_limit:
            message = (
                f"上下文超出模型可用窗口：保护槽位（角色设定 + 当前任务/消息）约 {required} tokens，"
                f"已超过为输出预留后的可用窗口 effective_limit={effective_limit}"
                f"（= 模型窗口 {context_limit} − 输出预留 {reserved_output_tokens}）。"
            )
            if image_count:
                from ctx_weft.core.utils import _IMAGE_PART_TOKENS
                message += (
                    f"其中不可裁的当前消息含 {image_count} 张图片，"
                    f"约占 {image_count * _IMAGE_PART_TOKENS} tokens。"
                    "请先减少图片数量，或改用更大上下文窗口的模型。"
                )
            else:
                message += "请改用更大上下文窗口的模型，或缩短当前消息 / 任务描述。"
        super().__init__(message, code=code)
```

- [ ] **Step 4: 让 `budget.py` 传入图片数**

在 `src/ctx_weft/core/assembler/budget.py` 第 11 行的 import 之后加入：

```python
from ctx_weft.core.utils import image_tokens, _IMAGE_PART_TOKENS
```

把第 82-89 行：

```python
        if total > token_limit:
            required = sum(b.token_estimate for b in blocks if eff_prio[b.id] == 0)
            sess = getattr(request, "session", None)
            raise ContextOverflowError(
                required=required,
                effective_limit=token_limit,
                context_limit=getattr(sess, "context_limit", 0),
                reserved_output_tokens=getattr(sess, "reserved_output_tokens", 0),
            )
```

改为：

```python
        if total > token_limit:
            floor = [b for b in blocks if eff_prio[b.id] == 0]
            required = sum(b.token_estimate for b in floor)
            # 地板（pin 住的当前消息）里的图片数——它们不可裁，是溢出的直接成因时
            # 用户该做的是删图而非删字，故单独报出（见 spec §3）。
            n_images = sum(image_tokens(b.content) for b in floor) // _IMAGE_PART_TOKENS
            sess = getattr(request, "session", None)
            raise ContextOverflowError(
                required=required,
                effective_limit=token_limit,
                context_limit=getattr(sess, "context_limit", 0),
                reserved_output_tokens=getattr(sess, "reserved_output_tokens", 0),
                image_count=n_images,
            )
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_context_overflow_message.py -v`
Expected: 3 passed

- [ ] **Step 6: 运行 budget 与错误相关既有回归**

Run: `uv run pytest tests/unit/test_budget_strategy.py -v && uv run pytest tests/unit -k "overflow or error" -v`
Expected: 全部 PASS

- [ ] **Step 7: 提交**

```bash
git add src/ctx_weft/core/errors.py src/ctx_weft/core/assembler/budget.py tests/unit/test_context_overflow_message.py
git commit -m "feat(errors): 上下文溢出文案报出不可裁的图片数，引导删图而非删字"
```

---

### Task 6: Phase 0 全量回归与收尾

**Files:**
- Test: 全仓

**Interfaces:**
- Consumes: Task 1-5 的全部产出
- Produces: 无

- [ ] **Step 1: 全量测试**

Run: `uv run pytest tests/ -q`
Expected: 全部 PASS，且**失败数与本 Phase 开始前相同**（应为 0）

- [ ] **Step 2: lint**

Run: `uv run ruff check src/ tests/`
Expected: 无新增告警。（已核实 `pyproject.toml` 的
`[tool.ruff.lint] select = ["E","W","F","I","B","UP","RUF"]` 不含 pylint 的私有
导入规则，故 `budget.py` / `errors.py` 里 import `_IMAGE_PART_TOKENS` 不会被标记。
无论如何**不要**在别处复制字面量 1600——违反 Global Constraints 的单一真源约束。）

- [ ] **Step 3: 人工确认纯文本恒等**

逐条核对本 Phase 的五个改造点，确认每处都是「既有表达式 + `image_tokens(...)`」的
形式，没有任何一处把既有的文本计数表达式本身改掉了。这是 Global Constraints 的
唯一硬约束，代码审阅比测试更可靠。

- [ ] **Step 4: 提交**

```bash
git commit --allow-empty -m "chore: Phase 0 完成——五个 token 估算点计入图片，纯文本口径不变"
```

---

## Phase 0 完成标准

- `image_tokens` 存在于 `utils.py`，`_IMAGE_PART_TOKENS` 仍是单一真源
- 五个估算点全部计入图片：`_history.py:111`、`composer.py:399`、`compact.py:205`、
  `background_observe.py:103`、`finalize.py:419`
- 纯文本会话的估算值与 Phase 0 之前逐字节相同（每个任务都有对应守卫测试）
- `ContextOverflowError` 在地板含图片时报出图片数
- 全量测试通过

## 后续 Phase

Phase 1（内容骨架）的计划在 Phase 0 落地后单独编写——它的归一层签名依赖
`image_tokens` 的最终形态与 Task 6 Step 2 对常量导出方式的处置。
