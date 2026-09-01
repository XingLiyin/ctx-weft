"""ask_user（form=question）的答复带图时，图必须到达模型。

此前 control_capability 用 content_to_text 展平 approval.message、并把 metadata 硬写成
{}——非文本 part 被静默丢弃（content_to_text 连占位都不留）。而 CONTENT_PARTS_KEY 通道
就在旁边，media:get_image 走的正是它。

本文件全程走真实路径：provider.invoke → HitlManager → 出口 payload。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from ctx_weft.core.loop.capability_gateway import CONTENT_PARTS_KEY
from ctx_weft.core.orchestrator.control_capability import (
    PROVIDER_NAME,
    ControlCapabilityProvider,
)
from ctx_weft.core.orchestrator.hitl_manager import HitlManager
from ctx_weft.core.state.models import Session, Task
from ctx_weft.protocols import ImagePart, ProviderContext, TextPart

REF = "blob:" + "a" * 64


def _img() -> ImagePart:
    return ImagePart(data=REF, media_type="image/png", source_type="ref")


def _control_provider(mgr: HitlManager):
    provider = ControlCapabilityProvider(hitl_manager=mgr)
    session = Session(id="s1", tenant_id="default", user_prompt="do it", status="RUNNING")
    task = Task(id="tsk_1", session_id="s1", status="ACTIVE", title="T1")
    tm = SimpleNamespace(get_task=lambda tid: task, reopen_chain=None)
    provider.register_session("s1", tm, session)
    return provider


async def _await_pending(mgr: HitlManager):
    for _ in range(200):
        pend = mgr.list_pending()
        if pend:
            return pend[0]
        await asyncio.sleep(0)
    raise AssertionError("no pending HITL request appeared")


async def _ask_and_respond(mgr, provider, respond) -> dict:
    """跑一次真实的 ask_user，用 respond(hitl_id) 应答，返回收到的 result payload。"""
    ctx = ProviderContext(session_id="s1", tenant_id="default",
                          task_id="tsk_1", agent_id="agt_1")
    payloads: list[dict] = []

    async def drain():
        async for ev in provider.invoke(
            f"{PROVIDER_NAME}:ask_user", {"questions": [{"question": "Which DB?"}]}, ctx
        ):
            if ev.kind == "result":
                payloads.append(ev.payload)

    handle = asyncio.create_task(drain())
    req = await _await_pending(mgr)
    await respond(req.id)
    await handle
    assert payloads, "ask_user 没有产出 result 事件"
    return payloads[0]


@pytest.mark.asyncio
async def test_accepted_answer_with_image_carries_the_part():
    """🔴 本任务存在的理由：图经 CONTENT_PARTS_KEY 交给 gateway，不再被展平掉。"""
    mgr = HitlManager()
    provider = _control_provider(mgr)
    payload = await _ask_and_respond(
        mgr, provider,
        lambda hid: mgr.answer(hid, [TextPart(text="就是这个"), _img()]),
    )
    assert payload["content"] == "就是这个"
    assert [p.data for p in payload["metadata"][CONTENT_PARTS_KEY]] == [REF]


@pytest.mark.asyncio
async def test_rejected_answer_with_image_keeps_the_part_and_the_prefix():
    """拒绝路径的前缀必须并进首个 TextPart，不能用 f-string 把 parts 拍成 repr。"""
    mgr = HitlManager()
    provider = _control_provider(mgr)
    payload = await _ask_and_respond(
        mgr, provider,
        lambda hid: mgr.reject(hid, message=[TextPart(text="不行"), _img()]),
    )
    assert payload["content"] == "Human declined: 不行"
    assert [p.data for p in payload["metadata"][CONTENT_PARTS_KEY]] == [REF]


@pytest.mark.asyncio
async def test_plain_text_answer_is_byte_identical_and_has_no_parts_key():
    """纯文本路径逐字节不变（Global Constraint 第一条）。"""
    mgr = HitlManager()
    provider = _control_provider(mgr)
    payload = await _ask_and_respond(mgr, provider,
                                     lambda hid: mgr.answer(hid, "use postgres"))
    assert payload["content"] == "use postgres"
    assert CONTENT_PARTS_KEY not in payload["metadata"]


@pytest.mark.asyncio
async def test_rejected_without_message_uses_default_sentence():
    mgr = HitlManager()
    provider = _control_provider(mgr)
    payload = await _ask_and_respond(mgr, provider, lambda hid: mgr.reject(hid))
    assert payload["content"] == "Human rejected the request."
    assert CONTENT_PARTS_KEY not in payload["metadata"]
