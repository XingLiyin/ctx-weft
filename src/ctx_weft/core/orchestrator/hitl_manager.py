"""HitlManager：Human-in-the-Loop。

一个机制（request → wait → resolve），三种 **form**：

  - ``approval``：审批门控——放行/拒绝一次工具调用，可带 ``modified_arguments``。
                  由 HumanConfirmationAuthorizer 在 CapabilityGateway 鉴权步触发。
  - ``question``：向人提问——取回人类文字 ``answer`` 回灌给 LLM。由 ask_user 工具触发。
  - ``wait``    ：act 纯文本暂停 / 软打断（wait_for_user 冷 park），回复经注入续跑。

状态：``pending → accepted | rejected | cancelled``

  - approval accepted（无改参）→ HitlApproved；（带改参）→ HitlModified
  - question/wait accepted     → HitlAnswered
  - rejected → HitlRejected；cancelled → HitlCancelled

超时语义（spec/07 §3/§7）：timeout_sec 到期 → 热→冷驱逐（移除 future，保留 pending）+
抛 HitlPark。答案后到时走冷 resume。HITL_TIMEOUT 事件定义保留但不再发出。

应答方式按 form：approval 用 ``approve``/``reject``；question/wait 用 ``answer``/``reject``。
host 据 ``request.form`` 决定 UI（批准/拒绝按钮 vs 答题输入框 vs 普通输入框）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ctx_weft.core.content import content_to_event_jsonable
from ctx_weft.core.events import EventType
from ctx_weft.core.state.models import HitlForm, HitlRequest, HitlStatus  # noqa: F401  (HitlStatus re-export 供既有 import)
from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols.context import ProviderContext
from ctx_weft.protocols.events import NullEventBlobStore

if TYPE_CHECKING:
    from ctx_weft.core.events.bus import EventBus
    from ctx_weft.protocols import ContentPart

logger = logging.getLogger(__name__)


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
        # 传入**已解决的请求**——Runtime 据 form/capability_id 决定续跑方式（act 的 ask_user/approval
        # 走 reconcile；act 纯文本暂停 wait_for_user 须注入回复，见 runtime._resume_after_cold_hitl）。
        # 热/冷分流由 HitlManager 自身完成,host 只转发回复、不感知 was_hot（spec/07 §6/§9）。
        # None → 不 resume（如纯单测）。
        self._on_cold_resolve = on_cold_resolve
        # 保留的「已解决」请求上限：决定缓存（find_for_tool_call）只需近期的，超限裁剪最旧者
        # 防止长跑进程里 _requests 无界增长。pending 永不裁剪。
        self._max_resolved = max_resolved
        # 冷决定查询（Runtime 绑定,挂事件日志折叠）：决定缓存的跨重启回落。重启后内存只重建
        # pending、不重建已解决,find_for_tool_call 未命中不等于"没答过"——不查日志就会重问。
        self._cold_decision_lookup: (
            "Callable[[str, str], Awaitable[HitlRequest | None]] | None"
        ) = None
        # 应答内容的校验 + 双侧外部化回调（Runtime 绑定 _normalize_hitl_content,
        # 见 set_content_normalizer）。签名 async (content, req) ->
        # (content, event_jsonable)——整个 HitlRequest 传过去（而非零散字段）：blob 的
        # tenant 锚点要由 req.session_id 解出，将来再要别的字段（如 resume_llm_*）也不
        # 必改签名。HitlRequest 本就是本模块自己的类型，故仍不必 import
        # MemoryBlobStore / LLM 任何类型。
        # None（纯单测直接构造 HitlManager() 时）→ 恒等变换、行为逐字节不变。
        self._content_normalizer: (
            "Callable[[str | list[ContentPart], HitlRequest], "
            "Awaitable[tuple[str | list[ContentPart], str | list[dict] | None]]] | None"
        ) = None
        self._requests: dict[str, HitlRequest] = {}
        self._futures: dict[str, asyncio.Future[HitlRequest]] = {}
        self._lock = asyncio.Lock()

    async def request(
        self,
        form: HitlForm,
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
        """登记一个 HITL 请求，返回 hitl_id。发 HitlRequired + SessionPausedHitl。"""
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
            id=rid, form=form, session_id=session_id, task_id=task_id, agent_id=agent_id,
            capability_id=capability_id, arguments=arguments or {}, question=question, context=context,
            questions=questions or [], tool_call_id=tool_call_id,
        )
        self._requests[rid] = req
        self._futures[rid] = asyncio.get_event_loop().create_future()
        logger.info("HITL requested [%s]: %s (%s)", form, rid, question[:80])
        await self._emit(EventType.HITL_REQUIRED, req, payload={
            "hitl_id": rid, "form": form, "capability_id": capability_id,
            "tool_call_id": tool_call_id, "agent_id": agent_id,
            "question": question, "context": context,
            "arguments": dict(arguments or {}),
            "questions": questions or [],
        })
        await self._emit(
            EventType.SESSION_PAUSED_HITL, req,
            payload={"capability_id": capability_id, "form": form},
        )
        return rid

    async def request_parked(
        self,
        form: HitlForm,
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
        """登记一个 pending HITL 但**不保留活 future**,返回 hitl_id。

        用于调用方随后立即 HitlPark 释放协程(冷 park)而非 await 的场景
        (如 ActStep interactive 任务纯文本暂停)。future 被驱逐后,后续 answer/approve/reject
        会经 on_cold_resolve 触发 session resume(等价于 wait() 超时的热→冷降级,但无需等超时)。
        """
        rid = await self.request(
            form=form, session_id=session_id, task_id=task_id,
            capability_id=capability_id, arguments=arguments,
            question=question, context=context, agent_id=agent_id, tool_call_id=tool_call_id,
        )
        self._futures.pop(rid, None)  # 驱逐 future → 应答走冷路径
        return rid

    async def wait(self, hitl_id: str) -> HitlRequest:
        """阻塞至应答。timeout_sec=None（默认）则永不超时。

        显式正整数 timeout_sec 超时 → 热→冷驱逐：移除 future、保留 pending、抛 HitlPark
        （spec/07 §3/§7）。answer 先到（race）则正常返回已解决请求。未知 id 抛 KeyError。
        """
        future = self._futures.get(hitl_id)
        if future is None:
            raise KeyError(f"No HITL request found: {hitl_id}")
        try:
            async with asyncio.timeout(self._timeout_sec):
                return await future
        except TimeoutError:
            async with self._lock:
                req = self._requests[hitl_id]
                if req.status != "pending":
                    return req                       # answer 先到：走热已解决
                self._futures.pop(hitl_id, None)  # 驱逐 future，保留 pending
            from ctx_weft.core.loop.park import HitlPark
            raise HitlPark(hitl_id=hitl_id, tool_call_id=req.tool_call_id)

    async def approve(
        self,
        hitl_id: str,
        *,
        message: "str | list[ContentPart]" = "",
        modified_arguments: dict[str, Any] | None = None,
        llm_account: str | None = None,
        llm_model: str | None = None,
    ) -> HitlRequest:
        """放行一个 approval 请求，可选备注 / 改写参数。accepted + HitlApproved/HitlModified。"""
        self._stash_resume_llm(hitl_id, llm_account, llm_model)
        req, _ = await self.resolve_approve(hitl_id, message=message, modified_arguments=modified_arguments)
        return req

    async def answer(
        self,
        hitl_id: str,
        text: "str | list[ContentPart]",
        *,
        llm_account: str | None = None,
        llm_model: str | None = None,
    ) -> HitlRequest:
        """应答一个 question/wait form 请求（人类文字答复）。accepted + HitlAnswered。"""
        self._stash_resume_llm(hitl_id, llm_account, llm_model)
        req, _ = await self.resolve_answer(hitl_id, text)
        return req

    async def reject(
        self,
        hitl_id: str,
        *,
        message: "str | list[ContentPart]" = "",
        llm_account: str | None = None,
        llm_model: str | None = None,
    ) -> HitlRequest:
        """拒绝请求（approval 与 question/wait form 通用），可带指导性反馈 message。rejected + HitlRejected。"""
        self._stash_resume_llm(hitl_id, llm_account, llm_model)
        req, _ = await self.resolve_reject(hitl_id, message=message)
        return req

    def _stash_resume_llm(
        self, hitl_id: str, llm_account: str | None, llm_model: str | None,
    ) -> None:
        """把应答时携带的当前所选模型暂存到 req，供 _resolve 的冷应答路径转发给 recover_session。

        host 在 /messages 应答时据 entry 传入；用户改了 model 后冷续跑（纯文本暂停回复等）须用
        新 model，而非投影里的旧 model。None 表示未提供、保持不变。"""
        if llm_account is None and llm_model is None:
            return
        req = self._requests.get(hitl_id)
        if req is None:
            return
        if llm_account is not None:
            req.resume_llm_account = llm_account
        if llm_model is not None:
            req.resume_llm_model = llm_model

    async def cancel(self, hitl_id: str, *, message: "str | list[ContentPart]" = "") -> HitlRequest:
        """收口一个悬挂 pending（session 关闭 / interrupt / GC）。cancelled + HitlCancelled。

        终态、不 requeue（§3）；已解决则幂等 no-op。

        message 与 answer/reject/approve 同走 `_normalize_message`：`_resolve` 写事件
        载荷的判据是 `if req.message`，载荷本身却由参数链递进去——不走同一条路就会发出
        「message 为真、载荷为 None」的 `HitlCancelled`，把「为什么被取消」从重放流里
        抹掉（生产调用方是熔断取消，`message="failure_threshold"`）。
        """
        req = self._require(hitl_id)
        req.message, event_jsonable = await self._normalize_message(req, message)
        result, _ = await self._resolve(
            req, "cancelled", EventType.HITL_CANCELLED,
            message_event_jsonable=event_jsonable,
        )
        return result

    def set_cold_resolve_handler(self, handler: "Callable[[HitlRequest], Awaitable[None]] | None") -> None:
        """注入冷应答后的 session resume 回调（Runtime 绑定 recover_session）。

        供构造后晚绑定（Runtime 需 self.recover_session）。冷分流在 core 内闭环,host 不参与。
        """
        self._on_cold_resolve = handler

    def get(self, hitl_id: str) -> HitlRequest | None:
        return self._requests.get(hitl_id)

    def find_for_tool_call(self, tool_call_id: str) -> HitlRequest | None:
        """按 tool_call_id 取最近一条 HITL 请求（§6 权威决定缓存）；空 id → None。"""
        if not tool_call_id:
            return None
        matches = [r for r in self._requests.values() if r.tool_call_id == tool_call_id]
        if not matches:
            return None
        return max(matches, key=lambda r: r.created_at)

    def set_content_normalizer(
        self,
        handler: (
            "Callable[[str | list[ContentPart], HitlRequest], "
            "Awaitable[tuple[str | list[ContentPart], str | list[dict] | None]]] | None"
        ),
    ) -> None:
        """注入应答内容的校验 + 双侧外部化回调（Runtime 绑定 _normalize_hitl_content）。

        回调返回**二元组** ``(content, event_jsonable)``：前者是 memory 侧归一化后的
        内容（写进 `req.message`），后者是同一份**原始** content 算出的 event 侧载荷
        （写进 HITL_* 事件 payload）。两者各自只碰自己那个 blob store，两个 ref 不必
        相同（blob-store 解耦 Task 3）。本类因此不再自己做事件侧外部化——它手上的
        `req.message` 已是 memory 侧的 ref，再算一次只会把一个 event store 永远打不开
        的引用写进事件。

        HITL 是人类往会话里注入内容的第二个入口——`run_single_task` / `start_session`
        两个入口早已接上 validate → normalize，此路径此前全程不校验、不外部化：图片
        既不过格式校验（`b64decode` 默认 `validate=False` **不抛**，静默解出垃圾字节
        ⟹ 静默损坏），还以 inline base64 永久留在 memory 里。

        回调收**整个 `HitlRequest`**：blob 的 tenant 锚点要由 `session_id` 解出——在
        req 上，将来再要别的字段（如 `resume_llm_account/model`）也不必改签名。

        供构造后晚绑定（与 set_cold_resolve_handler / set_cold_decision_lookup 同形态）。
        **未注入时是恒等变换**——直接构造 `HitlManager()` 的既有调用方行为逐字节不变。
        """
        self._content_normalizer = handler

    async def _normalize_message(
        self, req: HitlRequest, content: "str | list[ContentPart]",
    ) -> "tuple[str | list[ContentPart], str | list[dict] | None]":
        """应答内容过一遍校验 + 双侧外部化，返回 ``(memory 侧内容, event 侧载荷)``。

        刻意**不** try/except：校验失败必须原样抛给应答方（host 的 /messages），
        req 保持 pending、不发事件、不写 blob——与两个入口「入口即拒、不落库」一致。

        未注入 normalizer（纯单测直接构造 `HitlManager()`）→ 内容原样返回同一对象，
        event 侧载荷就地按 `NullEventBlobStore` 算：纯文本是零开销直通（逐字节不变），
        携图内容则**响亮抛错**——裸 HitlManager 从来不是生产路径（生产路径恒由
        `CtxWeftRuntime` 构造并接线 normalizer），携图内容绕过入口校验直接到这里本就
        该被看见，不该被一条静默降级吞掉。
        """
        if self._content_normalizer is not None:
            return await self._content_normalizer(content, req)
        return content, await content_to_event_jsonable(
            content,
            event_blob_store=NullEventBlobStore(),
            ctx=ProviderContext(session_id=req.session_id, tenant_id="default"),
        )

    def set_cold_decision_lookup(
        self, handler: "Callable[[str, str], Awaitable[HitlRequest | None]]",
    ) -> None:
        """绑定冷决定查询 async (session_id, tool_call_id) → HitlRequest | None。

        Runtime 挂事件日志折叠（fold_cold_hitl_decision）,供构造后晚绑定。"""
        self._cold_decision_lookup = handler

    async def find_resolved_for_tool_call(
        self, session_id: str, tool_call_id: str,
    ) -> HitlRequest | None:
        """决定缓存查询的跨重启版（reconcile 短路门控用,spec/07 §6）。

        内存已解决 → 直接用;内存 pending（活的等待）→ None,交 request() 幂等复用,不得用
        日志里的旧决定盖掉活请求;内存无记录 → 回落事件日志冷查询（缺可用内容时仍 None,
        调用方重新问）。空 tool_call_id / 未绑定冷查询 → None。"""
        if not tool_call_id:
            return None
        mem = self.find_for_tool_call(tool_call_id)
        if mem is not None:
            return mem if mem.status != "pending" else None
        if self._cold_decision_lookup is None:
            return None
        return await self._cold_decision_lookup(session_id, tool_call_id)

    def list_pending(self, session_id: str | None = None) -> list[HitlRequest]:
        return [
            r for r in self._requests.values()
            if r.status == "pending" and (session_id is None or r.session_id == session_id)
        ]

    def rebuild_pending(self, pending: dict[str, HitlRequest]) -> None:
        """从 replayed view 的 pending_hitl 重建内存请求（spec/07 §9）。

        不建 future（_futures 空）→ 后续 answer/approve 自动走冷 resume；
        re-park（resume 后 reconcile 再 request 同一 tool_call_id）时由 request() 补 future。
        fold_pending_hitl 每次折叠都构造新对象,直存无别名风险。
        """
        self._requests.update(pending)

    async def resolve_answer(self, hitl_id: str, text: "str | list[ContentPart]") -> tuple[HitlRequest, bool]:
        """question/wait form 应答，返回 (req, was_hot)。was_hot=False 时调用方须触发冷 resume。"""
        req = self._require(hitl_id)
        # 校验/外部化先于任何状态改动：被拒的内容不得写进 req.message、不得推进状态。
        req.message, event_jsonable = await self._normalize_message(req, text)
        return await self._resolve(
            req, "accepted", EventType.HITL_ANSWERED,
            resume_on_cold=True, message_event_jsonable=event_jsonable,
        )

    async def resolve_approve(
        self, hitl_id: str, *, message: "str | list[ContentPart]" = "",
        modified_arguments: dict[str, Any] | None = None,
    ) -> tuple[HitlRequest, bool]:
        """approval form 放行，返回 (req, was_hot)。was_hot=False 时调用方须触发冷 resume。"""
        req = self._require(hitl_id)
        # approve 的备注同样是 `str | list[ContentPart]`（见 approve 的签名）——
        # 与 answer/reject 走同一道校验 + 外部化，否则同一个洞在这条路径上仍开着。
        req.message, event_jsonable = await self._normalize_message(req, message)
        req.modified_arguments = modified_arguments
        evt = EventType.HITL_MODIFIED if modified_arguments is not None else EventType.HITL_APPROVED
        return await self._resolve(
            req, "accepted", evt,
            resume_on_cold=True, message_event_jsonable=event_jsonable,
        )

    async def resolve_reject(self, hitl_id: str, *, message: "str | list[ContentPart]" = "") -> tuple[HitlRequest, bool]:
        """拒绝（approval 与 question/wait form 通用），返回 (req, was_hot)。was_hot=False 时调用方须触发冷 resume。"""
        req = self._require(hitl_id)
        req.message, event_jsonable = await self._normalize_message(req, message)
        return await self._resolve(
            req, "rejected", EventType.HITL_REJECTED,
            resume_on_cold=True, message_event_jsonable=event_jsonable,
        )

    # ── internals ──────────────────────────────────────────────────────────────

    def _require(self, hitl_id: str) -> HitlRequest:
        req = self._requests.get(hitl_id)
        if req is None:
            raise KeyError(f"No HITL request found: {hitl_id}")
        return req

    async def _resolve(
        self,
        req: HitlRequest,
        status: HitlStatus,
        event_type: EventType,
        *,
        resume_on_cold: bool = False,
        message_event_jsonable: "str | list[dict] | None" = None,
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
        # 解析载荷进事件：message/改参是"决定的内容",不落盘则冷决定查询（跨重启的 reconcile
        # 短路）还原不出答案 → 只能重问、丢掉用户已给的回复。
        payload: dict = {"hitl_id": req.id}
        if req.message:
            # HITL_* 参与状态重建（reducers 折叠 pending_hitl/决定缓存），事件库恒不含
            # 字节。载荷由 `_normalize_message` 一步之前从**原始**应答内容算好、顺着
            # 参数递进来——**不**在这里拿 `req.message` 重算：那份内容已是 memory 侧
            # 归一化过的 ref，event store 既无权解读也解不开（两个契约独立、ref 命名
            # 空间互不相通，blob-store 解耦 Task 3）。
            payload["message"] = message_event_jsonable
        if req.modified_arguments is not None:
            payload["modified_arguments"] = req.modified_arguments
        await self._emit(event_type, req, payload=payload)
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
        from ctx_weft.core.events.types import EVENT_TYPES, Event
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
