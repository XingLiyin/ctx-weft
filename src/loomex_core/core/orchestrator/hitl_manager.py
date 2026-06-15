"""HitlManager：Human-in-the-Loop。

一个机制（request → wait → resolve），两种 **kind**：

  - ``approval``：审批门控——放行/拒绝一次工具调用，可带 ``modified_arguments``。
                  由 HumanConfirmationAuthorizer 在 CapabilityGateway 鉴权步触发。
  - ``input``   ：向人提问——取回人类文字 ``answer`` 回灌给 LLM。
                  由 ask_user 工具、act 纯文本暂停（wait_for_user）触发。

状态：``pending → accepted | rejected | cancelled``

  - approval accepted（无改参）→ HitlApproved；（带改参）→ HitlModified
  - input    accepted          → HitlAnswered
  - rejected → HitlRejected；cancelled → HitlCancelled

超时语义（spec/07 §3/§7）：timeout_sec 到期 → 热→冷驱逐（移除 future，保留 pending）+
抛 HitlPark。答案后到时走冷 resume。HITL_TIMEOUT 事件定义保留但不再发出。

应答方式按 kind：approval 用 ``approve``/``reject``；input 用 ``answer``/``reject``。
host 据 ``request.kind`` 决定 UI（批准/拒绝按钮 vs 答题输入框）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from loomex_core.core.events import EventType
from loomex_core.core.utils import generate_id, now_utc

if TYPE_CHECKING:
    from loomex_core.core.events.bus import EventBus
    from loomex_core.core.control.types import HitlRequestView

logger = logging.getLogger(__name__)

HitlKind = Literal["approval", "input"]
HitlStatus = Literal["pending", "accepted", "rejected", "cancelled"]


@dataclass
class HitlRequest:
    """一次 HITL 请求（含其解析结果）。kind 决定语义与应答形态。"""

    id: str
    kind: HitlKind
    session_id: str
    task_id: str
    agent_id: str = ""
    capability_id: str = ""                       # approval: 被门控的工具；input: 触发提问的工具
    tool_call_id: str = ""                        # 发起本次调用的 LLM tool_call id（§6 短路门控的键）
    arguments: dict[str, Any] = field(default_factory=dict)
    question: str = ""                            # 展示给人类的问题（approval / wait_for_user 用）
    context: str = ""
    questions: list[dict[str, Any]] = field(default_factory=list)  # ask_user 的结构化批量问题（含 options/multi_select）
    status: HitlStatus = "pending"
    # 解析载荷
    message: str = ""                             # 人类附带的自由文本：答复 / 拒绝理由 / 备注——任何决定下都可有
    modified_arguments: dict[str, Any] | None = None  # approval kind：改写后的工具参数（暂仅记录，不生效）
    created_at: datetime = field(default_factory=now_utc)
    resolved_at: datetime | None = None

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"


# 向后兼容别名（旧名 HitlApproval）
HitlApproval = HitlRequest


class HitlManager:
    """管理 HITL 请求的生命周期。"""

    def __init__(
        self,
        timeout_sec: int | None = None,
        event_bus: "EventBus | None" = None,
        max_resolved: int = 1000,
        on_cold_resolve: "Callable[[HitlRequest], Awaitable[None]] | None" = None,
    ) -> None:
        # None = 永不超时，一直等待人类应答（默认）。显式给正整数才启用超时守护。
        self._timeout_sec = timeout_sec
        self._event_bus = event_bus
        # 冷应答（Future 已驱逐 / 重启后无 future）后触发该 session resume 的回调（Runtime 绑定）。
        # 传入**已解决的请求**——Runtime 据 kind/capability_id 决定续跑方式（act 的 ask_user/approval
        # 走 reconcile；act 纯文本暂停 wait_for_user 须注入回复，见 runtime._resume_after_cold_hitl）。
        # 热/冷分流由 HitlManager 自身完成,host 只转发回复、不感知 was_hot（spec/07 §6/§9）。
        # None → 不 resume（如纯单测）。
        self._on_cold_resolve = on_cold_resolve
        # 保留的「已解决」请求上限：决定缓存（find_for_tool_call）只需近期的，超限裁剪最旧者
        # 防止长跑进程里 _requests 无界增长。pending 永不裁剪。
        self._max_resolved = max_resolved
        self._requests: dict[str, HitlRequest] = {}
        self._futures: dict[str, asyncio.Future[HitlRequest]] = {}
        self._lock = asyncio.Lock()

    async def request(
        self,
        kind: HitlKind,
        session_id: str,
        task_id: str,
        *,
        capability_id: str = "",
        arguments: dict[str, Any] | None = None,
        question: str = "",
        context: str = "",
        questions: list[dict[str, Any]] | None = None,
        agent_id: str = "",
        tool_call_id: str = "",
    ) -> str:
        """登记一个 HITL 请求，返回 request_id。发 HitlRequired + SessionPausedHitl。"""
        # idempotent by tool_call_id（§6）：cold reconcile / resume 再入同一调用时不新建。
        existing = self.find_for_tool_call(tool_call_id)
        if existing is not None:
            if existing.status == "pending":
                # 仍未解决（resume 后 re-park）：补一个存活 future 供本次 await。
                fut = self._futures.get(existing.id)
                if fut is None or fut.done():
                    self._futures[existing.id] = asyncio.get_event_loop().create_future()
            # 已解决：保留终态，调用方据 status 短路（不补 future）。
            return existing.id
        rid = generate_id("hit")
        req = HitlRequest(
            id=rid, kind=kind, session_id=session_id, task_id=task_id, agent_id=agent_id,
            capability_id=capability_id, arguments=arguments or {}, question=question, context=context,
            questions=questions or [], tool_call_id=tool_call_id,
        )
        self._requests[rid] = req
        self._futures[rid] = asyncio.get_event_loop().create_future()
        logger.info("HITL requested [%s]: %s (%s)", kind, rid, question[:80])
        await self._emit(EventType.HITL_REQUIRED, req, payload={
            "approval_id": rid, "kind": kind, "capability_id": capability_id,
            "tool_call_id": tool_call_id,
            "question": question, "context": context,
            "arguments": dict(arguments or {}),
            "questions": questions or [],
        })
        await self._emit(EventType.SESSION_PAUSED_HITL, req, payload={})
        return rid

    async def request_parked(
        self,
        kind: HitlKind,
        session_id: str,
        task_id: str,
        *,
        capability_id: str = "",
        arguments: dict[str, Any] | None = None,
        question: str = "",
        context: str = "",
        agent_id: str = "",
        tool_call_id: str = "",
    ) -> str:
        """登记一个 pending HITL 但**不保留活 future**,返回 request_id。

        用于调用方随后立即 HitlPark 释放协程(冷 park)而非 await 的场景
        (如 ActStep interactive 任务纯文本暂停)。future 被驱逐后,后续 answer/approve/reject
        会经 on_cold_resolve 触发 session resume(等价于 wait() 超时的热→冷降级,但无需等超时)。
        """
        rid = await self.request(
            kind=kind, session_id=session_id, task_id=task_id,
            capability_id=capability_id, arguments=arguments,
            question=question, context=context, agent_id=agent_id, tool_call_id=tool_call_id,
        )
        self._futures.pop(rid, None)  # 驱逐 future → 应答走冷路径
        return rid

    async def wait(self, request_id: str) -> HitlRequest:
        """阻塞至应答。timeout_sec=None（默认）则永不超时。

        显式正整数 timeout_sec 超时 → 热→冷驱逐：移除 future、保留 pending、抛 HitlPark
        （spec/07 §3/§7）。answer 先到（race）则正常返回已解决请求。未知 id 抛 KeyError。
        """
        future = self._futures.get(request_id)
        if future is None:
            raise KeyError(f"No HITL request found: {request_id}")
        try:
            async with asyncio.timeout(self._timeout_sec):
                return await future
        except TimeoutError:
            async with self._lock:
                req = self._requests[request_id]
                if req.status != "pending":
                    return req                       # answer 先到：走热已解决
                self._futures.pop(request_id, None)  # 驱逐 future，保留 pending
            from loomex_core.core.loop.park import HitlPark
            raise HitlPark(request_id=request_id, tool_call_id=req.tool_call_id)

    async def approve(
        self,
        request_id: str,
        *,
        message: str = "",
        modified_arguments: dict[str, Any] | None = None,
    ) -> HitlRequest:
        """放行一个 approval 请求，可选备注 / 改写参数。accepted + HitlApproved/HitlModified。"""
        req, _ = await self.resolve_approve(request_id, message=message, modified_arguments=modified_arguments)
        return req

    async def answer(self, request_id: str, text: str) -> HitlRequest:
        """应答一个 input 请求（人类文字答复）。accepted + HitlAnswered。"""
        req, _ = await self.resolve_answer(request_id, text)
        return req

    async def reject(self, request_id: str, *, message: str = "") -> HitlRequest:
        """拒绝请求（approval 与 input 通用），可带指导性反馈 message。rejected + HitlRejected。"""
        req, _ = await self.resolve_reject(request_id, message=message)
        return req

    async def cancel(self, request_id: str, *, message: str = "") -> HitlRequest:
        """收口一个悬挂 pending（session 关闭 / interrupt / GC）。cancelled + HitlCancelled。

        终态、不 requeue（§3）；已解决则幂等 no-op。
        """
        req = self._require(request_id)
        req.message = message
        result, _ = await self._resolve(req, "cancelled", EventType.HITL_CANCELLED)
        return result

    def set_cold_resolve_handler(self, handler: "Callable[[HitlRequest], Awaitable[None]] | None") -> None:
        """注入冷应答后的 session resume 回调（Runtime 绑定 recover_session）。

        供构造后晚绑定（Runtime 需 self.recover_session）。冷分流在 core 内闭环,host 不参与。
        """
        self._on_cold_resolve = handler

    def get(self, request_id: str) -> HitlRequest | None:
        return self._requests.get(request_id)

    def find_for_tool_call(self, tool_call_id: str) -> HitlRequest | None:
        """按 tool_call_id 取最近一条 HITL 请求（§6 权威决定缓存）；空 id → None。"""
        if not tool_call_id:
            return None
        matches = [r for r in self._requests.values() if r.tool_call_id == tool_call_id]
        if not matches:
            return None
        return max(matches, key=lambda r: r.created_at)

    def list_pending(self, session_id: str | None = None) -> list[HitlRequest]:
        return [
            r for r in self._requests.values()
            if r.status == "pending" and (session_id is None or r.session_id == session_id)
        ]

    def rebuild_pending(self, pending: dict[str, "HitlRequestView"]) -> None:
        """从 replayed view 的 pending_hitl 重建内存请求（spec/07 §9）。

        不建 future（_futures 空）→ 后续 answer/approve 自动走冷 resume；
        re-park（resume 后 reconcile 再 request 同一 tool_call_id）时由 request() 补 future。
        """
        for rid, h in pending.items():
            self._requests[rid] = HitlRequest(
                id=rid, kind=h.kind, session_id=h.session_id, task_id=h.task_id,
                capability_id=h.capability_id, tool_call_id=h.tool_call_id,
                question=h.question, context=h.context, status="pending",
            )

    async def resolve_answer(self, request_id: str, text: str) -> tuple[HitlRequest, bool]:
        """input-kind 应答，返回 (req, was_hot)。was_hot=False 时调用方须触发冷 resume。"""
        req = self._require(request_id)
        req.message = text
        return await self._resolve(req, "accepted", EventType.HITL_ANSWERED, resume_on_cold=True)

    async def resolve_approve(
        self, request_id: str, *, message: str = "",
        modified_arguments: dict[str, Any] | None = None,
    ) -> tuple[HitlRequest, bool]:
        """approval-kind 放行，返回 (req, was_hot)。was_hot=False 时调用方须触发冷 resume。"""
        req = self._require(request_id)
        req.message = message
        req.modified_arguments = modified_arguments
        evt = EventType.HITL_MODIFIED if modified_arguments is not None else EventType.HITL_APPROVED
        return await self._resolve(req, "accepted", evt, resume_on_cold=True)

    async def resolve_reject(self, request_id: str, *, message: str = "") -> tuple[HitlRequest, bool]:
        """拒绝（approval 与 input 通用），返回 (req, was_hot)。was_hot=False 时调用方须触发冷 resume。"""
        req = self._require(request_id)
        req.message = message
        return await self._resolve(req, "rejected", EventType.HITL_REJECTED, resume_on_cold=True)

    # ── internals ──────────────────────────────────────────────────────────────

    def _require(self, request_id: str) -> HitlRequest:
        req = self._requests.get(request_id)
        if req is None:
            raise KeyError(f"No HITL request found: {request_id}")
        return req

    async def _resolve(
        self,
        req: HitlRequest,
        status: HitlStatus,
        event_type: EventType,
        *,
        resume_on_cold: bool = False,
    ) -> tuple[HitlRequest, bool]:
        async with self._lock:
            if req.status != "pending":
                return req, False                    # 已解决（含驱逐后）→ 幂等
            req.status = status
            req.resolved_at = now_utc()
            future = self._futures.get(req.id)
            was_hot = future is not None and not future.done()
            if was_hot:
                future.set_result(req)
        await self._emit(event_type, req, payload={"approval_id": req.id})
        self._gc_resolved()
        # 冷应答：活协程已驱逐 / 重启后无 future,无法就地唤醒 → 触发该 session resume（spec/07 §6/§9）。
        # 仅 answer/approve/reject（resume_on_cold=True）；cancel 是终态、不 requeue。Runtime 据请求
        # 内容选续跑方式（reconcile / wait_for_user 注入）。
        if resume_on_cold and not was_hot and self._on_cold_resolve is not None:
            await self._on_cold_resolve(req)
        return req, was_hot

    def _gc_resolved(self) -> None:
        """裁剪已解决请求，防止 _requests 无界增长（决定缓存只需近期的）。pending 永不裁剪。

        同步、无 await：在 asyncio 单线程下原子执行，无需持锁。
        """
        resolved = [r for r in self._requests.values() if r.status != "pending"]
        if len(resolved) <= self._max_resolved:
            return
        resolved.sort(key=lambda r: r.resolved_at or r.created_at)
        for r in resolved[: len(resolved) - self._max_resolved]:
            self._requests.pop(r.id, None)
            self._futures.pop(r.id, None)

    async def _emit(self, event_type: EventType, req: HitlRequest, payload: dict) -> None:
        if self._event_bus is None:
            return
        from loomex_core.core.events.types import EVENT_TYPES, Event
        if event_type not in EVENT_TYPES:
            raise ValueError(f"Unknown event type: {event_type}; not in EVENT_TYPES")
        await self._event_bus.emit(Event(
            id=generate_id("evt"),
            run_id=None,
            sequence=0,
            session_id=req.session_id,
            type=event_type,
            timestamp=now_utc(),
            task_id=req.task_id or None,
            payload=payload,
        ))
