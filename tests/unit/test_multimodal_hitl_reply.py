import pytest

from ctx_weft.core.state.models import HitlRequest
from ctx_weft.protocols import ImagePart, TextPart


def _content():
    return [TextPart(text="这是我的答复"), ImagePart(data="ZGF0YQ==", media_type="image/png")]


def test_hitl_request_message_accepts_parts():
    req = HitlRequest(id="h1", form="wait", session_id="s", task_id="t")
    req.message = _content()
    assert req.message == _content()


def test_rejected_reply_keeps_image_via_prefix():
    """拒绝路径把「Human declined:」拼到回复前——必须保 parts。"""
    from ctx_weft.core.content import content_with_prefix
    out = content_with_prefix(_content(), "Human declined: ")
    assert any(not hasattr(p, "text") for p in out), "图片不得在拼接中丢失"
    assert out[0].text.startswith("Human declined: ")


def test_interrupt_edit_prefix_keeps_image_via_content_with_prefix():
    """① 打断续接说明拼到多模态回复前——必须保 parts，走 content_with_prefix 而非 f-string。"""
    from ctx_weft.core.content import content_with_prefix
    from ctx_weft.core.loop.steps.act import _interrupt_edit_prefix

    prefix = _interrupt_edit_prefix("do X")
    assert prefix
    out = content_with_prefix(_content(), prefix)
    assert any(not hasattr(p, "text") for p in out), "图片不得在拼接中丢失"
    assert out[0].text.startswith(prefix)


from ctx_weft.core.events.bus import InProcessEventBus
from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.state.models import Task
from ctx_weft.core.utils import now_utc
from ctx_weft.protocols import ProviderContext


class _StubEventBlobStore:
    """Task 4 收口后 `content_to_event_jsonable` 不再对不可外部化的 event store 短路——
    本文件直接白盒构造 `TaskManager`（不经 `CtxWeftRuntime` 的入口门控），push_task /
    reopen_task 携图内容因此需要一个真的可外部化 event blob store 才能走通。
    """

    can_externalize = True

    def __init__(self) -> None:
        self.blobs: dict[str, tuple[bytes, str]] = {}

    async def put(self, data: bytes, media_type: str, ctx: ProviderContext) -> str:
        import hashlib
        ref = f"blob:{hashlib.sha256(data).hexdigest()}"
        self.blobs[ref] = (data, media_type)
        return ref

    async def get(self, ref: str, ctx: ProviderContext):
        return self.blobs.get(ref)


async def _finished_task_manager(prompt):
    from ctx_weft.core.content import content_to_event_jsonable

    tm = TaskManager(session_id="s1", event_bus=InProcessEventBus())
    store = _StubEventBlobStore()
    tm.set_event_blob_store(store)
    task = Task(
        id="tsk_1", session_id="s1", status="FINISHED", tenant_id="default",
        assigned_agent_id="a1", creator_agent_id="a1",
        title="T", description="d", user_prompt=prompt,
        outputs="旧产出", created_at=now_utc(),
    )
    # 事件侧载荷由调用方备好（blob-store 解耦 Task 3：push_task 不再自己算——它手上的
    # user_prompt 可能已是 memory ref）。本文件白盒构造 TaskManager、prompt 就是原始
    # 内容，故这里现算一份，与入口的算法完全一致。
    await tm.push_task(task, user_prompt_event_jsonable=await content_to_event_jsonable(
        prompt, event_blob_store=store, ctx=ProviderContext(session_id="s1"),
    ))
    task.status = "FINISHED"          # push 会置 PENDING，reopen 要求 FINISHED
    return tm, task


@pytest.mark.asyncio
async def test_reopen_keeps_multimodal_original_prompt():
    """original_user_prompt 是 reopen 的 base；被丢空会让重开后图片永久消失。"""
    tm, task = await _finished_task_manager(_content())
    assert await tm.reopen_task("tsk_1", reason="重做") is True
    assert task.original_user_prompt == _content(), "多模态 base 必须原样快照"
    assert any(not hasattr(p, "text") for p in task.user_prompt), \
        "重写后的 prompt 必须仍带图片"
    assert "## Revision required" in task.user_prompt[-1].text


@pytest.mark.asyncio
async def test_reopen_plain_text_prompt_byte_identical():
    """纯文本路径必须与改造前逐字节相同。"""
    tm, task = await _finished_task_manager("原始要求")
    assert await tm.reopen_task("tsk_1", reason="重做") is True
    assert task.original_user_prompt == "原始要求"
    assert task.user_prompt == (
        "原始要求\n\n## Previous attempt (rejected)\n旧产出\n\n## Revision required\n重做"
    )


@pytest.mark.asyncio
async def test_reopen_multimodal_zero_sections_does_not_alias_original():
    """多模态 base + 零 section（无 reason/upstream/outputs）时，new_prompt 不得与
    original_user_prompt 共享同一个列表对象——否则日后就地修改一方会污染另一方。"""
    tm, task = await _finished_task_manager(_content())
    task.outputs = None
    task.process_report = None
    assert await tm.reopen_task("tsk_1") is True
    assert task.original_user_prompt == _content()
    assert task.user_prompt == _content()
    assert task.user_prompt is not task.original_user_prompt, \
        "new_prompt 必须是 base 的独立拷贝，不能与 original_user_prompt 别名同一对象"
