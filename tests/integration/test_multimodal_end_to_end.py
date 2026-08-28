"""端到端：多模态 user_prompt 走完至少一个 actor 回合（评审 I3，spec 2026-08-23）。

C1 的复现条件不是理论上的：``start_session`` 派生的 root task 用 ``title=""``
（``session_manager._make_root_task_manager``），使 ``act_guidance._task_label``
必然从 ``title`` 分支落到 ``user_prompt`` 分支。任何多模态 ``user_prompt``（``list[ContentPart]``）
若不经 ``content_to_text`` 拍扁就直接 ``.strip()``，第一个 act 回合就会 ``AttributeError``。

用 ``run_single_task`` 复现不了这条——它显式给 root task 设了 ``title="User Request"``，
title 分支短路，永远走不到 user_prompt 分支。必须走 ``start_session``。
"""

from __future__ import annotations

import base64

import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.assembler.assembler import ContextRequest
from ctx_weft.core.events import EventType
from ctx_weft.core.runtime import SessionStartParams
from ctx_weft.core.utils import _IMAGE_PART_TOKENS, content_to_text
from ctx_weft.protocols import (
    ImagePart, LLMChunk, LLMUsage, MemoryEventType, ProviderContext, TextPart, ToolCall,
)
from ctx_weft.protocols.memory import BLOB_REF_PREFIX
from ctx_weft.providers.llm.anthropic import AnthropicAdapter
from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
from ctx_weft.providers.memory_blackboard import InMemoryMemoryProvider
from ctx_weft.providers.memory_sql import open_sqlite_memory
from tests.integration.test_minimal_loop import (
    InlineAgentTemplateProvider,
    make_echo_template,
    make_runtime,
)

_MULTIMODAL_PROMPT = [
    TextPart(text="describe this image"),
    ImagePart(data="ZmFrZWJhc2U2NGRhdGE=", media_type="image/png"),
]


class _StubEventBlobStore:
    """Task 4 的第三道门控（event blob 门控）要求携图会话注册 EventBlobStore。

    本文件测的是 memory 侧外部化 / 出网 rehydrate / compact 降级等与 event blob
    存储本身无关的链路，故用一个最小可外部化桩满足门控——不断言其调用细节。
    """

    can_externalize = True

    def __init__(self) -> None:
        self.blobs: dict[str, tuple[bytes, str]] = {}

    async def put(self, data: bytes, media_type: str, ctx: ProviderContext) -> str:
        import hashlib
        ref = f"{BLOB_REF_PREFIX}{hashlib.sha256(data).hexdigest()}"
        self.blobs[ref] = (data, media_type)
        return ref

    async def get(self, ref: str, ctx: ProviderContext):
        return self.blobs.get(ref)


class _RouterLLM(MockLLMAdapter):
    """按 request.tools 路由：recognize_intent → 空；act → finish_task 收尾。

    root task 无 parent → ObserveStep 走规则降级，不需要路由 report_task_outcome。
    """

    def __init__(self, **kw) -> None:
        super().__init__(responses=[], **kw)
        self._n = 0

    def _id(self) -> str:
        self._n += 1
        return f"tc{self._n}"

    def complete(self, request, stream=True):
        self.last_request = request
        names = {getattr(t, "name", "") for t in (getattr(request, "tools", None) or [])}
        if "control__update_task_metadata" in names:  # recognize_intent
            return self._stream(MockResponse(text=""), request)
        # act：finish_task 收尾（interaction_mode=interactive 的 root task 只能靠工具调用收尾，
        # 纯文本只会 park 等用户 —— 这里要让它真正跑完一个回合并终结，故显式 finish）。
        return self._stream(
            MockResponse(
                text="This looks like a fake image.",
                tool_calls=[ToolCall(
                    id=self._id(), name="control__finish_task",
                    arguments={"deliverables_summary": "described"},
                )],
            ),
            request,
        )


@pytest.mark.asyncio
async def test_multimodal_prompt_completes_one_actor_round_without_crashing() -> None:
    """start_session(user_prompt=[TextPart, ImagePart]) 走完 act 回合，不抛异常，
    memory 里的 USER_PROMPT 仍是原样的 part 列表（无损）。"""
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())

    llm = _RouterLLM()
    llm.supports_vision = True  # instance-only：declares this mock's own capability,
    # not a hardcoded class default — keeps the strict-default semantics intact.
    runtime = make_runtime(llm=llm, agent_provider=resolver)
    memory = InMemoryMemoryProvider()
    runtime.providers.register_memory(memory)
    runtime.providers.register_event_blob_store(_StubEventBlobStore())

    handle = await runtime.start_session(
        SessionStartParams.create(
            template_id="agent:tpl_echo",
            user_prompt=_MULTIMODAL_PROMPT,
            context_limit=100_000,
        )
    )
    state = await handle.wait_for_finish(timeout=5.0)

    assert state is not None
    assert state.task.status == "FINISHED", f"expected FINISHED, got {state.task.status}"
    assert state.task.user_prompt == _MULTIMODAL_PROMPT

    ctxp = ProviderContext(session_id=state.session.id, agent_id=state.agent.id)
    user_recs = await memory.recall_recent(
        state.scope, [MemoryEventType.USER_PROMPT], 10, ctxp,
    )
    assert len(user_recs) == 1
    assert user_recs[0].content == _MULTIMODAL_PROMPT


@pytest.mark.asyncio
async def test_task_label_does_not_crash_on_multimodal_user_prompt() -> None:
    """退而求其次的直接单元覆盖（防端到端 fixture 未来漂移时这条根因仍被盯住）：
    _task_label 对 title="" 的多模态-prompt task 不抛 AttributeError。"""
    from ctx_weft.core.loop.steps.act_guidance import _task_label
    from ctx_weft.core.state.models import Task

    task = Task(
        id="t1", session_id="s1", status="ACTIVE",
        title="", description="",
        user_prompt=_MULTIMODAL_PROMPT,
    )
    label = _task_label(task)
    assert label  # 不抛；取到 user_prompt 首个 TextPart 的文本
    assert "describe this image" in label


# ── Task 7：兑现 Phase 0 的两条遗留义务 + 端到端出网验证（spec §6.5）───────────

_TEXT_ONLY_PROMPT = [TextPart(text="describe this image")]


class _FinishRouterLLM(MockLLMAdapter):
    """同 _RouterLLM，抽出复用：recognize_intent → 空；act → finish_task 收尾。"""

    def __init__(self, **kw) -> None:
        super().__init__(responses=[], **kw)
        self._n = 0

    def _id(self) -> str:
        self._n += 1
        return f"tc{self._n}"

    def complete(self, request, stream=True):
        self.last_request = request
        names = {getattr(t, "name", "") for t in (getattr(request, "tools", None) or [])}
        if "control__update_task_metadata" in names:
            return self._stream(MockResponse(text=""), request)
        return self._stream(
            MockResponse(
                text="ok",
                tool_calls=[ToolCall(
                    id=self._id(), name="control__finish_task",
                    arguments={"deliverables_summary": "d"},
                )],
            ),
            request,
        )


async def _run_and_collect_context_assembled_tokens(user_prompt) -> int:
    """驱动一次完整 start_session 回合，返回 PrepareStep 真实装配（PriorityBudgetStrategy +
    DefaultComposer 全链路）产出的 CONTEXT_ASSEMBLED.token_count（生产事件，非测试自己算的）。"""
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())
    llm = _FinishRouterLLM()
    llm.supports_vision = True  # instance-only（见上一处同名注释的理由）
    runtime = make_runtime(llm=llm, agent_provider=resolver)
    memory = InMemoryMemoryProvider()
    runtime.providers.register_memory(memory)
    runtime.providers.register_event_blob_store(_StubEventBlobStore())

    handle = await runtime.start_session(
        SessionStartParams.create(
            template_id="agent:tpl_echo",
            user_prompt=user_prompt,
            context_limit=100_000,
        )
    )
    state = await handle.wait_for_finish(timeout=5.0)
    assert state.task.status == "FINISHED"

    events = await runtime.event_store.read_by_session(state.session.id)
    counts = [e.payload["token_count"] for e in events if e.type == EventType.CONTEXT_ASSEMBLED]
    assert counts, "expected at least one CONTEXT_ASSEMBLED event"
    return counts[0]


@pytest.mark.asyncio
async def test_assembled_token_count_higher_with_image_than_text_only() -> None:
    """Phase 0 遗留义务 1（spec §6.5）：composer.py:400 的 image_tokens(m.content) 曾因
    composer 一律拍扁 m.content 恒为 str 而恒返回 0。这里驱动完整装配链
    （start_session → PrepareStep → ContextAssembler.assemble → DefaultComposer.compose），
    从生产事件 CONTEXT_ASSEMBLED 里取真实 token_count，断言含图会话 > 同等文本会话
    （二者共享同一段文本 "describe this image"，唯一变量是多出的 ImagePart）。"""
    text_tokens = await _run_and_collect_context_assembled_tokens(_TEXT_ONLY_PROMPT)
    mm_tokens = await _run_and_collect_context_assembled_tokens(_MULTIMODAL_PROMPT)
    assert mm_tokens == text_tokens + _IMAGE_PART_TOKENS, (
        f"含图会话 token_count({mm_tokens}) 应恰好比同等文本会话({text_tokens}) 多"
        f" _IMAGE_PART_TOKENS({_IMAGE_PART_TOKENS})——否则 image_tokens(m.content) 只是"
        "贡献了非零但错误的常数（此前的 `>` 断言对此不敏感）"
    )


class _WireCapturingAnthropicAdapter(AnthropicAdapter):
    """驱动 Task 5 的真实 wire 转换代码（AnthropicAdapter._build_payload → _serialize_messages）
    捕获实际会发给 Anthropic Messages API 的 payload，但不做真实网络调用——complete() 直接
    从 payload 合成一段 chunk 流，不经 httpx。captured_payloads 是本 Phase 的验收证据：
    图片是否真的进了 wire content blocks，而不是仅仅停留在 AssembledPrompt.messages 里。"""

    def __init__(self, **kw) -> None:
        super().__init__(api_key="test-key", **kw)
        self.captured_payloads: list[dict] = []
        self._n = 0

    def _id(self) -> str:
        self._n += 1
        return f"tc{self._n}"

    def complete(self, request, stream=True):
        payload = self._build_payload(request)
        self.captured_payloads.append(payload)
        names = {t.get("name", "") for t in (payload.get("tools") or [])}
        if "control__update_task_metadata" in names:
            return self._fake_stream(text="", tool_calls=[])
        return self._fake_stream(
            text="ok",
            tool_calls=[ToolCall(
                id=self._id(), name="control__finish_task",
                arguments={"deliverables_summary": "d"},
            )],
        )

    async def _fake_stream(self, *, text: str, tool_calls: list[ToolCall]):
        if text:
            yield LLMChunk(kind="token", text=text)
        for tc in tool_calls:
            yield LLMChunk(kind="tool_call", tool_call=tc)
        yield LLMChunk(kind="usage", usage=LLMUsage(
            prompt_tokens=1, completion_tokens=1, total_tokens=2,
        ))
        yield LLMChunk(kind="done", finish_reason="tool_use" if tool_calls else "stop")


def _has_image_block(payload: dict) -> bool:
    for m in payload.get("messages", []):
        content = m.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "image":
                    return True
    return False


@pytest.mark.asyncio
async def test_multimodal_prompt_reaches_wire_payload_as_image_block() -> None:
    """本 Phase 的真正验收（spec §6.5）：start_session(user_prompt=[TextPart, ImagePart])
    走完一个 actor 回合后，
      1) 不抛异常；
      2) memory 里的 USER_PROMPT 记录仍是 part 列表；
      3) 送到 LLM adapter 的 wire payload（真实 AnthropicAdapter._build_payload 产出）里
         含 image block ——图片真的出网了，不是止步于 AssembledPrompt.messages。
    """
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())

    llm = _WireCapturingAnthropicAdapter()
    llm.supports_vision = True  # instance-only（见上文同名注释的理由）
    runtime = make_runtime(llm=llm, agent_provider=resolver)
    memory = InMemoryMemoryProvider()
    runtime.providers.register_memory(memory)
    runtime.providers.register_event_blob_store(_StubEventBlobStore())

    handle = await runtime.start_session(
        SessionStartParams.create(
            template_id="agent:tpl_echo",
            user_prompt=_MULTIMODAL_PROMPT,
            context_limit=100_000,
        )
    )
    state = await handle.wait_for_finish(timeout=5.0)  # 1) 不抛异常

    assert state is not None
    assert state.task.status == "FINISHED"

    # 2) memory 里的 USER_PROMPT 记录仍是 part 列表
    ctxp = ProviderContext(session_id=state.session.id, agent_id=state.agent.id)
    user_recs = await memory.recall_recent(
        state.scope, [MemoryEventType.USER_PROMPT], 10, ctxp,
    )
    assert len(user_recs) == 1
    assert user_recs[0].content == _MULTIMODAL_PROMPT

    # 3) 送到 LLM adapter 的 wire payload 里含 image block
    assert llm.captured_payloads, "expected at least one captured wire payload"
    assert any(_has_image_block(p) for p in llm.captured_payloads), (
        "图片没有出现在任何一次 wire payload 的 image block 里——"
        "AssembledPrompt.messages 里可能有 ImagePart，但没有真的出网"
    )


# ── Task 5：Phase 3b 端到端收口 ────────────────────────────────────────────────
#
# 前四个任务各自都有单元测试，但都是在打过桩的边界上验证的。下面三条从**真实全链路**
# （start_session → 归一层外部化 → memory → 装配 → gateway rehydrate → adapter wire
# payload）证明它们确实拼得起来。

_RAW_IMAGE_BYTES = base64.b64decode(_MULTIMODAL_PROMPT[1].data)


def _image_sources(payload: dict) -> list[dict]:
    """payload 里所有 image block 的 source 字典（真实 AnthropicAdapter 产出的 wire 形态）。"""
    out: list[dict] = []
    for m in payload.get("messages", []):
        content = m.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "image":
                    out.append(b.get("source") or {})
    return out


def _image_parts(content: object) -> list:
    """content 里的 ImagePart（dataclass 形态）。

    调用方必须先 ``assert isinstance(content, list)``——``content`` 是 ``str`` 时本函数
    返回空列表，用它做「没有图」的断言会得到一条永真的重言式（已知陷阱 1）。
    """
    if not isinstance(content, list):
        return []
    return [p for p in content if getattr(p, "type", None) == "image"]


async def _recall_user_prompt_parts(memory, state) -> object:
    ctxp = ProviderContext(session_id=state.session.id, agent_id=state.agent.id)
    recs = await memory.recall_recent(
        state.scope, [MemoryEventType.USER_PROMPT], 10, ctxp,
    )
    assert len(recs) == 1, f"expected exactly one USER_PROMPT record, got {len(recs)}"
    return recs[0].content


async def _run_multimodal_session(*, blob_store=None, session_id: str | None = None):
    """跑一整个 start_session 会话（多模态 user_prompt），返回 (runtime, memory, state, llm)。

    ``blob_store`` 为 None 时**完全不注册**——runtime 拿到 NullMemoryBlobStore，即 Phase 3a 的
    既有行为面。
    """
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())

    llm = _WireCapturingAnthropicAdapter()
    llm.supports_vision = True  # instance-only（见上文同名注释的理由）
    runtime = make_runtime(llm=llm, agent_provider=resolver)
    memory = InMemoryMemoryProvider()
    runtime.providers.register_memory(memory)
    runtime.providers.register_event_blob_store(_StubEventBlobStore())
    if blob_store is not None:
        runtime.providers.register_memory_blob_store(blob_store)

    handle = await runtime.start_session(
        SessionStartParams.create(
            template_id="agent:tpl_echo",
            user_prompt=_MULTIMODAL_PROMPT,
            session_id=session_id,
            context_limit=100_000,
        )
    )
    state = await handle.wait_for_finish(timeout=5.0)
    assert state is not None
    assert state.task.status == "FINISHED", f"expected FINISHED, got {state.task.status}"
    return runtime, memory, state, llm


@pytest.mark.asyncio
async def test_ref_externalized_in_memory_but_full_base64_on_the_wire(tmp_path) -> None:
    """覆盖 1（ref 全链路）：注册**真** MemoryBlobStore（``SqlMemoryProvider``，Task C3）后——

    Phase 3b 时这里挂的是 ``FilesystemToolsProvider``；裁定 D5 移除了那个实现，
    改挂 SQL provider。**三条断言一字未改**——它们钉的是「ref 全链路」本身，
    与 blob 存哪里无关，换实现后仍全绿就是契约面未漂移的证据。

    a) core 侧（memory 记录）里的图是 ``source_type="ref"`` 的 ``blob:<sha>``，
       **不再是 base64**（证明 Task 2 的入口外部化在真实 start_session 上生效）；
    b) 送到 adapter 的 wire payload 里是**完整 base64**，且 base64 解码回来
       **逐字节等于原始图片字节**（证明 Task 3 的 gateway rehydrate 在真实出网路径上生效）。

    (b) 刻意校验解码结果而不是「不以 blob: 开头」——后者对任何非空字符串几乎恒真，
    杀不掉「rehydrate 还原出了别的字节」这类损坏。
    """
    session_id = "ses_blob_e2e"
    async with open_sqlite_memory(tmp_path / "blobs.db") as blob_store:
        await _assert_ref_roundtrip(blob_store, session_id)


async def _assert_ref_roundtrip(blob_store, session_id: str) -> None:
    _rt, memory, state, llm = await _run_multimodal_session(
        blob_store=blob_store, session_id=session_id,
    )

    # (a) memory 侧只见 ref
    content = await _recall_user_prompt_parts(memory, state)
    assert isinstance(content, list), (  # 陷阱 1 的守卫：str 上所有 part 断言都退化成重言式
        f"USER_PROMPT 记录应仍是 part 列表，实为 {type(content).__name__}"
    )
    images = _image_parts(content)
    assert len(images) == 1, f"expected exactly one ImagePart in memory, got {len(images)}"
    assert images[0].source_type == "ref", (
        f"接了真 MemoryBlobStore 时 memory 里应是 ref，实为 source_type={images[0].source_type!r}"
    )
    assert images[0].data.startswith(BLOB_REF_PREFIX), (
        f"ref 的 data 应是 blob:<sha>，实为 {images[0].data!r}"
    )
    assert images[0].data != _MULTIMODAL_PROMPT[1].data, "memory 里仍是原始 base64——没有外部化"
    # 同一条也钉住 task 侧（start_session 把外部化后的 params 透传给 create_session）
    assert _image_parts(state.task.user_prompt)[0].source_type == "ref"

    # blob 真的落盘了，且内容就是原始字节（外部化不是「把 data 改成个假 ref」）
    ctxp = ProviderContext(session_id=session_id)
    got = await blob_store.get(images[0].data, ctxp)
    assert got is not None, f"blob {images[0].data!r} 没有真的落进 store"
    assert got[0] == _RAW_IMAGE_BYTES
    assert got[1] == "image/png"

    # (b) wire 上是完整 base64，解码回原始字节
    sources = [s for p in llm.captured_payloads for s in _image_sources(p)]
    assert sources, (
        "没有任何一次 wire payload 含 image block——ref 没有在出网前被还原成图片"
    )
    for src in sources:
        assert src.get("type") == "base64"
        assert src.get("media_type") == "image/png"
        wire_data = src.get("data") or ""
        assert base64.b64decode(wire_data) == _RAW_IMAGE_BYTES, (
            "wire 上的 base64 解码后不等于原始图片字节——rehydrate 没有真的还原内容"
        )
        assert wire_data == _MULTIMODAL_PROMPT[1].data


@pytest.mark.asyncio
async def test_without_blob_store_memory_and_wire_stay_inline_base64() -> None:
    """覆盖 2（不接 MemoryBlobStore 时行为不变）：不注册 MemoryBlobStore（runtime 用 NullMemoryBlobStore）时，
    memory 记录与 wire payload 都仍是 **inline base64**，与 Phase 3a 既有 e2e
    （``test_multimodal_prompt_reaches_wire_payload_as_image_block``）结果一致。

    这条最容易写成永真（「某件事没有发生」型断言，已知陷阱 3），所以主断言取
    **正向的逐字节相等**：``content == _MULTIMODAL_PROMPT``——它同时排除了「变成了 ref」
    「data 被改写」「part 被降级成文本」「列表被拍扁成 str」全部走样，
    而不只是「没有出现 blob: 前缀」。
    """
    _rt, memory, state, llm = await _run_multimodal_session(blob_store=None)

    content = await _recall_user_prompt_parts(memory, state)
    assert content == _MULTIMODAL_PROMPT, (
        f"不接 MemoryBlobStore 时 memory 记录必须与 Phase 3a 逐字节一致，实为 {content!r}"
    )
    assert state.task.user_prompt == _MULTIMODAL_PROMPT

    images = _image_parts(content)
    assert len(images) == 1
    assert images[0].source_type == "base64"

    sources = [s for p in llm.captured_payloads for s in _image_sources(p)]
    assert sources, "不接 MemoryBlobStore 时图片仍应照常出网（Phase 3a 的既有行为）"
    for src in sources:
        assert src.get("data") == _MULTIMODAL_PROMPT[1].data
        assert base64.b64decode(src["data"]) == _RAW_IMAGE_BYTES


@pytest.mark.asyncio
async def test_compact_assembly_carries_no_image_while_act_still_does() -> None:
    """覆盖 3（compaction 不再带图）：拿一次真实会话跑完后**真实 memory 里的记录**，
    经**真实装配链**（runtime._build_assembler → PriorityBudgetStrategy → DefaultComposer）
    各装配一次 ``purpose="compact"`` 与 ``purpose="act"``：

    - compact 的 messages 里**一个 ImagePart 都没有**（Task 4 的 per-purpose 降级）；
    - act 的 messages 里**仍有** ImagePart —— 对照组，否则「compact 没有图」可能只是
      因为这条会话的 memory 里根本就没有图（假绿）。

    另断言 compact 的 token_count 严格小于 act 的：降级发生在 token_count 计算**之前**
    （裁定 T1），否则 budget/compact 会基于含图的错误数字判断。
    """
    runtime, memory, state, _llm = await _run_multimodal_session(blob_store=None)

    ctxp = ProviderContext(session_id=state.session.id, agent_id=state.agent.id)
    assembler = runtime._build_assembler(memory, ctxp, {})

    def _request(purpose: str, extra: dict) -> ContextRequest:
        return ContextRequest(
            purpose=purpose,
            scope=state.scope,
            task=state.task,
            agent=state.agent,
            session=state.session,
            template=state.extra.get("template"),
            bound_capabilities=[],
            extra=extra,
        )

    act_prompt = await assembler.assemble(_request("act", {}))
    compact_prompt = await assembler.assemble(_request("compact", {"compact_scope": "task"}))

    act_images = [im for m in act_prompt.messages for im in _image_parts(m.content)]
    assert act_images, (
        "对照组失败：act 装配里就没有 ImagePart——本测试的 compact 断言会是假绿"
    )

    compact_images = [im for m in compact_prompt.messages for im in _image_parts(m.content)]
    assert not compact_images, (
        f"compact 装配仍携带 {len(compact_images)} 个 ImagePart——compaction 又在重发图片"
    )
    # 图确实变成了确定性文本占位（裁定 D2），而不是整条消息被丢掉
    compact_text = "\n".join(content_to_text(m.content) for m in compact_prompt.messages)
    assert "[image image/png]" in compact_text, (
        "compact 里既没有图、也没有占位文本——图被整个丢掉了，而不是降级"
    )

    assert compact_prompt.token_count < act_prompt.token_count, (
        f"compact token_count({compact_prompt.token_count}) 应低于 act"
        f"({act_prompt.token_count})——降级必须发生在 token_count 计算之前（裁定 T1）"
    )
