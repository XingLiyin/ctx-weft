from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ctx_weft.core.orchestrator.session_manager import SessionManager
from ctx_weft.core.runtime import SessionStartParams
from ctx_weft.core.state.models import Session
from ctx_weft.core.utils import content_to_text, now_utc
from ctx_weft.protocols import ImagePart, ProviderContext, TextPart
from ctx_weft.protocols.events import EventBlobStore
from ctx_weft.providers.llm.mock import MockLLMAdapter
from ctx_weft.providers.llm.provider import _FixedModelClient
from ctx_weft.providers.memory_blackboard import InMemoryMemoryProvider
from tests.integration.test_minimal_loop import (
    InlineAgentTemplateProvider,
    make_echo_template,
    make_runtime,
)

pytestmark = pytest.mark.asyncio


class _StubEventBlobStore(EventBlobStore):
    """携图路径的第三道门控（Task 4）要求宿主注册 EventBlobStore——最小可外部化桩。

    不 import 其他测试模块的等价实现（约定：测试模块之间不互相 import），故各文件
    各放一份最小拷贝。
    """

    def __init__(self) -> None:
        self.blobs: dict[str, tuple[bytes, str]] = {}

    async def put(self, data: bytes, media_type: str, ctx: ProviderContext) -> str:
        import hashlib
        ref = f"blob:{hashlib.sha256(data).hexdigest()}"
        self.blobs[ref] = (data, media_type)
        return ref

    async def get(self, ref: str, ctx: ProviderContext):
        return self.blobs.get(ref)


def _content():
    return [TextPart(text="看这张图"), ImagePart(data="ZGF0YQ==", media_type="image/png")]


async def test_session_start_params_accepts_parts():
    p = SessionStartParams.create(
        template_id="t", user_prompt=_content(), context_limit=1000,
    )
    assert p.user_prompt == _content()


async def test_session_start_params_still_accepts_str():
    p = SessionStartParams.create(
        template_id="t", user_prompt="纯文本", context_limit=1000,
    )
    assert p.user_prompt == "纯文本"


async def test_root_task_carries_full_content_session_carries_summary():
    sm = SessionManager(
        lifecycle_manager=SimpleNamespace(),
        event_bus=SimpleNamespace(emit=AsyncMock()),
        event_blob_store=_StubEventBlobStore(),
    )
    session = Session(
        id="s1", user_prompt=content_to_text(_content()), status="RUNNING",
        tenant_id="default", root_agent_id="a1", created_at=now_utc(),
    )
    task, _tm = await sm._make_root_task_manager(session, _content(), None)
    assert task.user_prompt == _content(), "Task 承载全量内容"
    assert isinstance(session.user_prompt, str), "Session 只承载文本摘要"
    # _make_root_task_manager 从不从 user_prompt 派生 description（源码恒为 ""）——
    # 断言确切值，而非宽松的 "" or isinstance(str) 判据，确保未来有人在此加派生逻辑时
    # 这条测试真的会盯着它是否走了 content_to_text。
    assert task.description == "", \
        "description 必须是 str——对 list 切片会静默产出截断的 part 列表"


async def test_run_single_task_root_task_description_is_text_not_truncated_parts():
    """run_single_task 内 description=user_prompt[:200] 是本任务点名的陷阱：对 list
    切片不报错，会得到截断的 part 列表。必须改走 content_to_text(user_prompt)[:200]。

    Phase 1 不碰装配链（core/assembler/），multimodal Task.user_prompt 走到 assembler
    的 task_spec source 还不被支持（那是后续任务的范围）——故这里在 _execute_task 处
    打桩截获 Task，只验证 run_single_task 自身构造 Task 的这一步，不跑真实 loop。
    """
    templates = InlineAgentTemplateProvider()
    templates.register(make_echo_template())
    runtime = make_runtime(agent_provider=templates)
    runtime.providers.register_memory(InMemoryMemoryProvider())
    runtime.providers.register_event_blob_store(_StubEventBlobStore())
    runtime.providers.register_llm_provider(
        SimpleNamespace(
            get_client=lambda account=None, model=None: _FixedModelClient(
                MockLLMAdapter(responses=[]), model or "mock-model", 128_000, 8_192,
                account=account or "acct-main",
            ),
        )
    )

    captured = {}

    async def _fake_execute_task(**kwargs):
        captured["task"] = kwargs["task"]
        return SimpleNamespace(), SimpleNamespace()

    runtime._execute_task = _fake_execute_task  # type: ignore[method-assign]

    await runtime.run_single_task(
        template_id="agent:tpl_echo", user_prompt=_content(),
    )

    task = captured["task"]
    assert task.description == content_to_text(_content())[:200]
    assert isinstance(task.description, str)
    assert task.user_prompt == _content(), "Task 承载全量内容"
