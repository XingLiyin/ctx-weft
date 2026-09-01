"""HitlService：HITL 的唯一漏斗。

只做三件事：登记（open）、终局（resolve / cancel）、**发事实**。它不认识 Runtime、
不认识协程栈、不持久化任何东西——耐久性是 event store provider 的事，冷续跑是
`ResumeCoordinator` 订阅 `HitlResolved` 的事（spec §3.1 / §7.3）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ctx_weft.core.hitl.registry import HitlRegistry, PendingHitl
from ctx_weft.core.hitl.reply_intake import ReplyIntake
from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols.events import Event, EventType
from ctx_weft.protocols.hitl import (
    HITL_OUTCOME_CANCELLED,
    Delivery,
    HitlAsk,
    HitlDecision,
    HitlReply,
    NoResumeDelivery,
    ToolResultDelivery,
    UserTurnDelivery,
)

if TYPE_CHECKING:
    from ctx_weft.protocols.events import EventBus
    from ctx_weft.protocols import ContentPart

logger = logging.getLogger(__name__)


def delivery_to_payload(delivery: Delivery) -> dict[str, Any]:
    """Delivery → 事件载荷。**封闭值域**，故穷举即完备。"""
    if isinstance(delivery, ToolResultDelivery):
        return {"kind": "tool_result", "tool_call_id": delivery.tool_call_id}
    if isinstance(delivery, UserTurnDelivery):
        return {"kind": "user_turn", "task_id": delivery.task_id,
                "preface": delivery.preface}
    if isinstance(delivery, NoResumeDelivery):
        return {"kind": "no_resume"}
    raise ValueError(f"Unknown delivery: {delivery!r}")


class HitlService:
    def __init__(
        self,
        registry: HitlRegistry,
        event_bus: "EventBus",
        reply_intake: ReplyIntake,
        *,
        id_factory: Callable[[], str] = lambda: generate_id("hit"),
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self.registry = registry
        self._bus = event_bus
        self._intake = reply_intake
        self._new_id = id_factory
        self._now = clock

    async def open(
        self,
        ask: HitlAsk,
        *,
        session_id: str,
        task_id: str,
        agent_id: str = "",
        tool_call_id: str = "",
    ) -> PendingHitl:
        """登记一个请求并发 `HitlOpened`。同 tool_call_id 复用既有请求且**不重发事实**。"""
        existing = self.registry.find_for_tool_call(tool_call_id)
        if existing is not None:
            return existing
        req = self.registry.open(
            ask, hitl_id=self._new_id(), session_id=session_id, task_id=task_id,
            agent_id=agent_id, tool_call_id=tool_call_id, created_at=self._now(),
        )
        logger.info("HITL opened [%s]: %s (%s)", req.form, req.id, req.prompt[:80])
        await self._emit(EventType.HITL_OPENED, req, {
            "hitl_id": req.id,
            "form": req.form,
            "delivery": delivery_to_payload(req.delivery),
            "subject_id": req.subject_id,
            "prompt": req.prompt,
            "detail": req.detail,
            "fields": list(req.fields),
            "proposal": req.proposal,
            "tool_call_id": req.tool_call_id,
            "agent_id": req.agent_id,
            "resume_state": req.resume_state,
            "reply_as_result": req.reply_as_result,
        })
        return req

    async def resolve(self, reply: HitlReply) -> PendingHitl | None:
        """终局一个请求。已终局 → `None`（幂等 no-op，不重发事实）；未知 id → `KeyError`。"""
        req = self.registry.get(reply.hitl_id)
        if req is None:
            raise KeyError(f"No HITL request found: {reply.hitl_id}")
        # 校验/外部化**先于**任何状态改动：被拒的内容不得写进 decision、不得发事实。
        message, event_payload = await self._intake.normalize(reply.message, req)
        decision = HitlDecision(outcome=reply.outcome, message=message,
                                modified_arguments=reply.modified_arguments)
        return await self._commit(req, decision, event_payload)

    async def cancel(self, hitl_id: str, *, message: "str | list[ContentPart]" = "",
                     ) -> PendingHitl | None:
        """收口一个悬挂 pending（会话关闭 / 熔断）。终态、不 requeue；已终局则 no-op。

        message 与 resolve 同走 `ReplyIntake`——不走同一条路就会发出「message 为真、
        载荷为 None」的事实，把「为什么被取消」从重放流里抹掉。
        """
        req = self.registry.get(hitl_id)
        if req is None:
            raise KeyError(f"No HITL request found: {hitl_id}")
        normalized, event_payload = await self._intake.normalize(message, req)
        decision = HitlDecision(outcome=HITL_OUTCOME_CANCELLED, message=normalized)
        return await self._commit(req, decision, event_payload)

    # ── internals ─────────────────────────────────────────────────────────────

    async def _commit(
        self,
        req: PendingHitl,
        decision: HitlDecision,
        message_event_payload: "str | list[dict] | None",
    ) -> PendingHitl | None:
        """状态转移 → 取槽 → 投递 → 发事实。

        转移与取槽在 `registry.resolve()` 里同步完成（无 await ⟹ 原子），因此
        「热投递」与「冷续跑」互斥、不双投。投递与发事实在其后，不占原子段。
        """
        transferred = self.registry.resolve(req.id, decision, self._now())
        if transferred is None:
            return None                              # 已终局：幂等 no-op
        resolved, slot = transferred
        claimed = False
        if slot is not None:
            # deliver 声明为不抛（-> bool），但对一个已完成的 future 再次 set 会抛
            # InvalidStateError。resolve() 已不可逆——这里若真抛出且不接住，请求就停在
            # 「已终局」却没有 HitlResolved 事实，跨重启无法恢复。发事实的义务优先于
            # 让这个异常继续传播。
            try:
                claimed = bool(slot.deliver(decision))
            except Exception:
                logger.exception(
                    "HitlService._commit: slot.deliver raised for hitl_id=%s; "
                    "treating as unclaimed and still emitting HitlResolved", resolved.id)
                claimed = False
        payload: dict[str, Any] = {
            "hitl_id": resolved.id,
            "outcome": decision.outcome,
            "claimed": claimed,
        }
        # 事件载荷由**原始**内容一步之前算好、顺参数递进来——不在这里拿
        # decision.message 重算：那份内容已是 memory 侧的 ref，event store 解不开。
        if message_event_payload:
            payload["message"] = message_event_payload
        if decision.modified_arguments is not None:
            payload["modified_arguments"] = decision.modified_arguments
        await self._emit(EventType.HITL_RESOLVED, resolved, payload)
        self.registry.gc()
        return resolved

    async def _emit(self, event_type: EventType, req: PendingHitl, payload: dict) -> None:
        await self._bus.emit(Event(
            id=generate_id("evt"),
            run_id=None,
            sequence=0,
            session_id=req.session_id,
            type=event_type,
            timestamp=self._now(),
            task_id=req.task_id or None,
            agent_id=req.agent_id or None,
            payload=payload,
        ))
