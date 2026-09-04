"""HitlService：HITL 的唯一漏斗。

只做三件事：登记（open）、终局（resolve / cancel）、**发事实**。它不认识 Runtime、
不认识协程栈、不持久化任何东西——耐久性是 event store provider 的事。

**冷续跑不是订阅出来的**（spec §7.3 订正）：早期草案让一个 `ResumeCoordinator` 订阅
`HitlResolved` 去驱动冷续跑，那个设计已被推翻——总线 handler 在 `emit()` 内同步 drain
且背压下丢事件，把控制流的关键信号挂上去，「人答了但会话永不续跑」就成了可能。现行
唯一驱动方是 `CtxWeftRuntime.reply_to_hitl` 的**返回值**：它按 `resolved.claimed` 分流，
未被热投递消费的才触发冷续跑。本模块只发事实，不认识续跑。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ctx_weft.core.hitl.registry import HitlRegistry, PendingHitl
from ctx_weft.core.hitl.reply_intake import ReplyIntake
from ctx_weft.core.util import emit_event
from ctx_weft.core.util import generate_id, now_utc
from ctx_weft.protocols.events import EventOrigin, EventType
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

_ORIGIN = EventOrigin.HITL_SERVICE


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
        stage: str,
        invocation_key: str = "",
        tenant_id: str = "default",
    ) -> PendingHitl:
        """登记一个请求并发 `HitlOpened`。同 `(session_id, tool_call_id, stage,
        invocation_key)` 复用既有请求且**不重发事实**。

        `invocation_key` 见 `PendingHitl.invocation_key`：同一 tool_call id 下的**另一次**
        调用不得复用上一次的记录/决定（复审 I3）。

        `tenant_id`：调用方从其上下文（`ProviderContext.tenant_id` / `Session.tenant_id`）
        传入——本类自己不持有、也不去解——存进 `PendingHitl.tenant_id`，供 `_emit` 与
        之后 `resolve`/`cancel` 时同一个 `req` 复用（总账 A5：漏填时事件落到 `Event` 的
        默认值 `"default"`，非 default 租户的投影租户就错了）。
        """
        existing = self.registry.find_for_tool_call(
            session_id, tool_call_id, stage, invocation_key=invocation_key or None)
        if existing is not None:
            return existing
        req = self.registry.open(
            ask, hitl_id=self._new_id(), session_id=session_id, task_id=task_id,
            agent_id=agent_id, tool_call_id=tool_call_id, stage=stage, created_at=self._now(),
            invocation_key=invocation_key, tenant_id=tenant_id,
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
            "stage": req.stage,
            "invocation_key": req.invocation_key,
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
        resolved.claimed = claimed
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
        await emit_event(
            self._bus,
            event_type,
            session_id=req.session_id,
            tenant_id=req.tenant_id,
            origin=_ORIGIN,
            task_id=req.task_id or None,
            agent_id=req.agent_id or None,
            payload=payload,
            # **显式传**：HitlService 持有一个可注入的时钟（单测靠它冻结时间），
            # 丢掉它会让时间源静默换成 emit_event 内部的 now_utc()。
            timestamp=self._now(),
        )
