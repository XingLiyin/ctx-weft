"""_persist_user_prompt：多模态 raw 落库（图片改造前第一次消失的地方，spec §6.3）。

装配期仍会拍扁（Phase 2 处理）；本文件断言的是 memory 里存的是什么，不是 prompt 里出现了什么。
"""
from types import SimpleNamespace

import pytest

from ctx_weft.core.loop.driver import _persist_user_prompt
from ctx_weft.protocols import (
    ImagePart, MemoryAddress, MemoryEventType, ProviderContext, TextPart,
)
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider


def _content():
    return [TextPart(text="看这张图"), ImagePart(data="ZGF0YQ==", media_type="image/png")]


async def _run(prompt):
    """与 tests/unit/test_current_message_framing.py 同一构造惯例。"""
    mem = InMemoryMemoryProvider()
    pctx = ProviderContext(session_id="s1", tenant_id="default")
    scope = MemoryAddress(session_id="s1", task_id="t1", agent_id="ag1")
    task = SimpleNamespace(
        id="t1", user_prompt=prompt, user_prompt_in_memory=False,
    )
    state = SimpleNamespace(task=task, scope=scope)
    ctx = SimpleNamespace(memory=mem, provider_ctx=pctx)
    await _persist_user_prompt(state, ctx)
    recs = await mem.recall_recent(scope, [MemoryEventType.USER_PROMPT], 10, pctx)
    return task, recs


@pytest.mark.asyncio
async def test_persist_user_prompt_keeps_parts():
    """图第一次消失的地方：_persist_user_prompt 此前无条件 content_to_text。"""
    task, recs = await _run(_content())
    assert recs, "必须落一条 USER_PROMPT"
    assert recs[0].content == _content(), "存进 memory 的必须仍是 part 列表"
    assert any(not hasattr(p, "text") for p in recs[0].content), "图片不得丢失"
    assert task.user_prompt_in_memory is True


@pytest.mark.asyncio
async def test_persist_user_prompt_str_path_unchanged():
    """纯文本路径必须与改造前逐字节相同。"""
    task, recs = await _run("把这个 ppt 转 pdf")
    assert recs[0].content == "把这个 ppt 转 pdf"
    assert "## Current" not in recs[0].content, "raw 落库，呈现态框架不落库"
    assert task.user_prompt_in_memory is True
