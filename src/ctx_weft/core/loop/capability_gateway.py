"""CapabilityGateway：统一工具调用入口。

职责（对应 miniAgents ToolGateway）：
  1. 按名查 Capability 对象（CapabilityCache）
  2. 授权检查（Authorizer）
  3. 参数脱敏（headers 里的敏感 key）
  4. 执行（CapabilityProvider.invoke，流式）
  5. 发布审计事件（EventBus）
  6. Memory ingest（TOOL_INVOCATION + TOOL_RESULT）

ActStep 只调 gateway.invoke()，拿回 InvocationResult，不感知内部细节。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import jsonschema

from ctx_weft.core.content import normalize_content_parts, redact_content_for_event
from ctx_weft.protocols.events import EventType
from ctx_weft.protocols.events import EventBus
from ctx_weft.core.orchestrator.capability_cache import CapabilityCache
from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols.capability import Authorizer, CapabilityProvider, ToolCapabilityProvider, qualify
from ctx_weft.protocols.context import ContentPart, TextPart
from ctx_weft.protocols.llm import RAW_ARGS_KEY
from ctx_weft.core.orchestrator.control_capability import PROVIDER_NAME as CONTROL, _PLAN_DISPATCH_ACK
from ctx_weft.protocols.filesystem import SpillSink
from ctx_weft.protocols.memory import MemoryEvent, MemoryEventType, MemoryScope, MemoryProvider, MemoryAddress
from ctx_weft.protocols.memory_compat import MemoryKind

if TYPE_CHECKING:
    from ctx_weft.core.loop.driver import LoopState, LoopContext

logger = logging.getLogger(__name__)

_REDACT_HEADERS = frozenset({"authorization", "cookie", "x-api-key", "x-auth-token"})

# 畸形 {"_raw": ...} 报错里回吐原文的上限：畸形原文可能是大 write_file 的几 KB 内容，
# 整段回灌会炸 context，超长截断。
_RAW_ERROR_MAX_LEN = 800

# 派发型控制工具（spec 2026-06-28 §2.3）：其 tool_call 落 agent 层 delegate conversation turn
# （AGENT_CONVERSATION_TURN, assistant），即时 result 暂挂，由 child finalize 回填配对的 tool 回合
# （同 origin=delegating task）。普通工具仍走 task 层 TOOL_INVOCATION/RESULT。
DISPATCH_TOOLS = frozenset({
    qualify(f"{CONTROL}:delegate_task"),
    qualify(f"{CONTROL}:delegate_plan"),
})

# 计划型派发工具：除写 delegate conversation turn 外，还需写一条配对的 ack tool result，
# 避免该 plan 框悬挂（被 legalize 剥掉）。由 child finalize 补写的 result 仅针对 start_task 子框。
_PLAN_DISPATCH_TOOLS = frozenset({
    qualify(f"{CONTROL}:delegate_plan"),
})

# 编排/裁决型控制工具：其结果是状态信号、不入 task 对话——例如 report_task_outcome 的 HITL 回复
# 改由 finalize 以 role=user 注入。（ask_user 的人类答复是 actor 输入，仍写 task 层。）
# finish_task 同理：反转契约后它是无参收尾标记，最终答复即助手消息正文、canonical 出口是
# task.outputs（ActStep 收尾时合成），标记本身不入 task 对话。
# collect_process_report 是 background observe 的终止工具：result 由 run_observe_react 取出落
# close report 槽（→ Process Report），且 background observe 在 task close 后才跑，若入 task 对话
# 会污染已冻结的对话且不被 supersede（泄漏进后续 task prompt）。
SILENT_TOOLS = frozenset({
    qualify(f"{CONTROL}:report_task_outcome"),
    qualify(f"{CONTROL}:update_task_metadata"),
    qualify(f"{CONTROL}:finish_task"),
    qualify(f"{CONTROL}:collect_process_report"),
})


# 「工具返回非文本内容」的通用接缝（子设计 §4.2）。
#
# provider 在 `CapabilityEvent(kind="result")` 的 `payload["metadata"]` 里挂一个
# `list[ContentPart]`（或等价的 dict 形态，gateway 侧过归一层），gateway 把它拼在
# 文本部分之后，使 `InvocationResult.content` 变成 `list[ContentPart]`。
#
# **通道是通用的，不认发布者**：本期只有 `media:get_image` 用（Phase 4 Task 4），
# 但浏览器截图、图表生成等能力将来走同一条路，gateway 不做来源白名单。
#
# 流式协议本身不改：`_stream_tool` 依旧只聚合文本块（`result_parts: list[str]`）。
# 于是落盘截断（`_maybe_spill`）、human note 拼接、事件 payload 截断这些既有加工
# 全部只作用于**文本部分**——因为 parts 是在它们之后才拼上去的。
CONTENT_PARTS_KEY = "content_parts"


# ── InvocationResult ──────────────────────────────────────────────────────────


@dataclass
class InvocationResult:
    """Gateway.invoke() 的结构化返回。ActStep 直接消费，不再处理原始事件流。"""

    invocation_id: str
    tool_name: str
    # 拼好的 result，追加进 LLM messages。默认是**文本**（与改造前逐字节相同）；
    # 仅当 provider 经 metadata[CONTENT_PARTS_KEY] 贡献了非文本部分时才是
    # `list[ContentPart]`（形如 `[TextPart(文本), *parts]`）。
    # 读取方注意：对 list 做 `.strip()` / `join` / `content[:N]` 都是错的
    # （切片一个 list 不报错，但切出来的是前 N 个 part）——文本化请走
    # `core.content` 的 `content_to_text` / `redact_content_for_event`。
    content: str | list[ContentPart]
    metadata: dict[str, Any] = field(default_factory=dict)  # control signals
    is_error: bool = False


# ── CapabilityGateway ─────────────────────────────────────────────────────────


class CapabilityGateway:
    """统一 capability 调用入口：授权 → 脱敏 → 执行 → 审计 → memory。"""

    def __init__(
        self,
        capability_cache: CapabilityCache,
        capability_providers: list[CapabilityProvider],
        memory: MemoryProvider,
        event_bus: EventBus,
        provider_authorizers: dict[str, Authorizer] | None = None,
        default_authorizer: Authorizer | None = None,
        spill_threshold: int = 4000,
        spill_preview_chars: int = 1000,
    ) -> None:
        self._cache = capability_cache
        self._providers = capability_providers
        self._provider_index: dict[str, ToolCapabilityProvider] = {
            p.name: p for p in capability_providers
            if isinstance(p, ToolCapabilityProvider)
        }
        self._memory = memory
        self._event_bus = event_bus
        self._provider_authorizers: dict[str, Authorizer] = provider_authorizers or {}
        if default_authorizer is None:
            from ctx_weft.providers.authorizer import AllowAllAuthorizer
            default_authorizer = AllowAllAuthorizer()
        self._default_authorizer: Authorizer = default_authorizer
        # 工具输出截断阈值（字符）：超出则委托 SpillSink.spill() 落盘，
        # result 改为「提示 + 路径 + 预览」。<=0 关闭。
        self._spill_threshold = spill_threshold
        self._spill_preview_chars = spill_preview_chars
        # 落盘走 SpillSink（core 不直接碰文件系统、不知道 workspace 在哪）
        self._spill_sink: SpillSink | None = next(
            (p for p in capability_providers if isinstance(p, SpillSink)),
            None,
        )

    async def invoke(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        state: "LoopState",
        ctx: LoopContext,
        tool_call_id: str = "",
    ) -> InvocationResult:
        """执行一次工具调用，返回结构化结果。

        tool_call_id：发起本次调用的 LLM tool_call id（spec/06 §5），透传给派发工具用于委派回填。
        编排：解析 → 授权 → 脱敏 → 执行(流式) → 记录；各步细节见私有 helper。
        """
        invocation_id = generate_id("inv")
        is_dispatch = tool_name in DISPATCH_TOOLS
        is_silent = tool_name in SILENT_TOOLS  # 不入 task 对话的编排/裁决工具

        # 1. Lookup capability（只处理 kind="tool"）。控制工具的全局可达性由 CapabilityCache 的
        # session 全局区保证（get_by_qualified_name 回退），gateway 无需特殊逻辑。
        cap = self._cache.get_by_qualified_name(state.agent.id, tool_name)
        if cap is None or cap.kind != "tool":
            return await self._error_and_record(
                state, ctx, tool_name, invocation_id,
                f"[Error: unknown tool '{tool_name}']", is_dispatch, is_silent, tool_call_id,
            )

        # 2. Authorization：按 cap.id 前缀取 per-provider authorizer，无则用 default
        authorizer = self._get_authorizer(cap.id)
        # 交出 ProviderContext（不是 loop 的 LoopContext）——授权契约只认 protocols 类型。
        decision = await authorizer.authorize(
            cap, ctx.provider_ctx, arguments, tool_call_id=tool_call_id,
        )
        if decision.defer:
            # 守住安全不变式：绝不调 provider.invoke；上抛 park 信号 → loop 落 SUSPENDED（spec/07 §7）。
            from ctx_weft.core.loop.park import HitlPark
            raise HitlPark(tool_call_id=tool_call_id)
        if not decision.allowed:
            logger.warning("Capability '%s' blocked by authorizer for agent %s", cap.id, state.agent.id)
            content = (
                f"[Blocked by human: {decision.message}]" if decision.message
                else f"[Error: capability '{tool_name}' not authorized]"
            )
            return await self._error_and_record(
                state, ctx, tool_name, invocation_id, content, is_dispatch, is_silent, tool_call_id,
            )

        # 3. Sanitize arguments（改写参数生效，None → 原参；仍走脱敏）
        schema = getattr(cap, "input_schema", None)
        effective_args = decision.modified_arguments if decision.modified_arguments is not None else arguments
        effective_args = _coerce_args(effective_args, schema)
        # 兜底 {"_raw": <无法解析文本>}：adapter 对「参数没解析成 JSON」的哨兵（可解析的已在
        # finalize 解包）。给直白报错，别让 _validate_args 报误导性的「必填项缺失」——那会诱导
        # 模型把参数照抄进 _raw、陷入死循环（见 protocols.llm.RAW_ARGS_KEY）。
        if list(effective_args) == [RAW_ARGS_KEY]:
            # 带上畸形原文（截断防炸 context）：模型下轮读这条 tool_result 才看得到自己写错了什么
            # → 据此自纠。线上 arguments 那格已被降级成合法 "{}"（见 openai._dump_tool_arguments），
            # 原文只能靠这条 error 传回。
            raw = str(effective_args[RAW_ARGS_KEY])
            excerpt = raw if len(raw) <= _RAW_ERROR_MAX_LEN else raw[:_RAW_ERROR_MAX_LEN] + " …(truncated)"
            return await self._error_and_record(
                state, ctx, tool_name, invocation_id,
                f"[Error: invalid arguments for '{tool_name}': arguments were not valid JSON "
                f"and could not be parsed. You sent: {excerpt} — re-send the call with a "
                f"well-formed JSON arguments object]",
                is_dispatch, is_silent, tool_call_id,
            )
        # 剥掉 schema 未声明的顶层键（对任意调用生效）。放在 _raw 兜底之后，避免把哨兵剥空
        # 而丢掉「参数非法」信号；放在 required 校验之前，使「只发了未知键」被剥空后照样触发 required。
        effective_args = _strip_unknown_keys(effective_args, schema)
        # 参数校验：放在 coerce 之后，看到的是收敛后的类型（3 而非 "3"），不会假阳性。
        # 只拦 required/type/enum（见 _validate_args），失败回灌 LLM 让其改参重试，与 unknown-tool 同出口。
        err = _validate_args(effective_args, schema)
        if err is not None:
            return await self._error_and_record(
                state, ctx, tool_name, invocation_id,
                f"[Error: invalid arguments for '{tool_name}': {err}]",
                is_dispatch, is_silent, tool_call_id,
            )
        sanitized = _sanitize(effective_args)

        # 4. Find provider
        provider = self._find_provider(cap.id)
        if provider is None:
            return await self._error_and_record(
                state, ctx, tool_name, invocation_id,
                f"[Error: no provider found for '{cap.id}']", is_dispatch, is_silent, tool_call_id,
            )

        # 5. 记录 invocation（事件 + TOOL_INVOCATION / delegate conversation turn 入 memory）
        await self._record_invocation(state, ctx, tool_name, cap, invocation_id, sanitized, is_dispatch, is_silent, tool_call_id)

        # 6. 执行（流式）。透传 invocation_id（provider 据此登记在途句柄，供 cancel 对应）与
        # tool_call_id（控制工具据此把 origin_tool_call_id 写到 child）。
        provider_ctx = dataclasses.replace(
            ctx.provider_ctx,
            invocation_id=invocation_id,
            extra={**ctx.provider_ctx.extra, "tool_call_id": tool_call_id},
        )
        result_parts, metadata, is_error = await self._stream_tool(
            provider, cap.id, sanitized, provider_ctx, state, invocation_id,
        )

        text = "\n".join(result_parts) or ("(no output)" if not is_error else "")
        # 工具输出过长 → 委托 fs provider 落盘；在 human note / 审计 / memory ingest 之前，使下游拿到截断版。
        text = await self._maybe_spill(text, ctx, invocation_id, tool_name, cap.spillable)
        if decision.message:  # 放行时人类备注并入结果回灌 LLM
            text = f"[Human note: {decision.message}]\n{text}"
        # 非文本部分最后拼上（见 CONTENT_PARTS_KEY）：上面 spill / human note 两步只处理文本，
        # 无 content_parts 时 content 仍是同一个 str，纯文本路径逐字节不变。
        content: str | list[ContentPart] = text
        parts = metadata.get(CONTENT_PARTS_KEY)
        if isinstance(parts, (list, tuple)) and parts:
            # 过归一层：宿主 provider 可能给 dict 形态的 part（JSON 往返），
            # 与 MemoryEvent / LLMMessage 的 __post_init__ 共用同一份归一。
            content = normalize_content_parts([TextPart(text=text), *parts])

        # 7. 记录 result（事件 + TOOL_RESULT 入 memory）
        await self._record_result(state, ctx, tool_name, invocation_id, sanitized, content, is_error, is_dispatch, is_silent, tool_call_id)

        return InvocationResult(
            invocation_id=invocation_id, tool_name=tool_name,
            content=content, metadata=metadata, is_error=is_error,
        )

    @staticmethod
    def _error_result(invocation_id: str, tool_name: str, content: str) -> InvocationResult:
        return InvocationResult(invocation_id=invocation_id, tool_name=tool_name, content=content, is_error=True)

    async def _error_and_record(
        self, state, ctx, tool_name, invocation_id, content, is_dispatch, is_silent, tool_call_id,
    ) -> InvocationResult:
        """执行前错误出口（未知工具 / 未授权 / 非法参数 / 无 provider）：返回 error_result 的同时，
        补一条配对 TOOL_RESULT 入 task 对话，使 act 在派发前已落库的 assistant LLM_RESPONSE.tool_call
        不悬挂（spec/06 §4 无损重建）。否则纯从 memory 重组 prompt（observe at max_turns / resume 恢复）
        时会出现 assistant(tool_calls=[id]) 无配对 tool 消息 → provider 400。
        派发(submit_*)/SILENT 工具的 tool_call 本就不入 task 层 LLM_RESPONSE、不会悬挂，故跳过落库
        （与 _record_result 的 is_dispatch/is_silent 处理一致）。
        """
        if not is_dispatch and not is_silent:
            await self._memory.ingest(
                MemoryEvent(
                    kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK,
                    address=_tool_scope(state),
                    content=content,
                    timestamp=now_utc(),
                    role="tool",
                    metadata={"invocation_id": invocation_id, "tool_name": tool_name,
                              "tool_call_id": tool_call_id, "is_error": True},
                ),
                ctx.provider_ctx,
            )
        return self._error_result(invocation_id, tool_name, content)

    async def _record_invocation(
        self, state, ctx, tool_name, cap, invocation_id, sanitized, is_dispatch, is_silent, tool_call_id,
    ) -> None:
        """发 CapabilityInvoked + ingest（派发→agent 层 delegate conversation turn；
        普通→task 层 TOOL_INVOCATION；SILENT 普通工具不入对话）。"""
        from ctx_weft.core.loop.driver import make_event
        await self._event_bus.emit(make_event(state, EventType.CAPABILITY_INVOKED, payload={
            "invocation_id": invocation_id,
            "capability_name": tool_name,
            "capability_id": cap.id,
            "arguments": sanitized,
            "tool_call_id": tool_call_id,
        }))
        if is_dispatch:
            # 派发（spec 2026-06-28 §2.3；2026-07-03 修订）：**只有 delegate_plan 的 envelope 框**
            # 在此 eager 写（plan 框 + 配对 ack，避免 plan 框悬挂被 legalize 剥掉）。
            # **delegate_task 不再 eager 写框**——eager 框只能带「派发时刻」，无法落在「任务开始执行」
            # 时间线上；改由 child finalize 的 _ensure_dispatch_frame 铸框，框与 result 同锚 task.started_at
            # → 二者严格相邻、且在 started_at 时间线上（并发多派发也各自成对、不再堆叠错序）。
            if tool_name in _PLAN_DISPATCH_TOOLS:
                await self._memory.ingest(
                    MemoryEvent(
                        kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.AGENT,
                        address=_tool_scope(state),
                        content="",
                        timestamp=now_utc(),
                        role="assistant",
                        metadata={"origin_task_id": state.task.id,
                                  "parent_task_id": state.task.parent_task_id,
                                  "tool_calls": [{"id": tool_call_id, "name": tool_name,
                                                  "input": sanitized}]},
                    ),
                    ctx.provider_ctx,
                )
                # envelope: 给 plan 框写一条配对的 ack tool result，避免该框悬挂(被 legalize 剥掉)。
                await self._memory.ingest(
                    MemoryEvent(
                        kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.AGENT,
                        address=_tool_scope(state),
                        content=_PLAN_DISPATCH_ACK,
                        timestamp=now_utc(),
                        role="tool",
                        metadata={"origin_task_id": state.task.id,
                                  "parent_task_id": state.task.parent_task_id,
                                  "tool_call_id": tool_call_id},
                    ),
                    ctx.provider_ctx,
                )
        elif not is_silent:
            await self._memory.ingest(
                MemoryEvent(
                    kind=MemoryKind.TOOL_AUDIT, scope=MemoryScope.TASK,
                    address=_tool_scope(state),
                    content=f"{tool_name}({sanitized})",
                    timestamp=now_utc(),
                    role="assistant",
                    metadata={"invocation_id": invocation_id, "tool_name": tool_name,
                              "tool_call_id": tool_call_id},
                ),
                ctx.provider_ctx,
            )

    async def _stream_tool(
        self, provider, cap_id, sanitized, provider_ctx, state, invocation_id,
    ) -> tuple[list[str], dict[str, Any], bool]:
        """流式执行 provider.invoke，聚合 result/metadata/error。

        CancelledError（在途被打断）→ 调 provider.cancel 作安全网后重抛（provider 自身的 finally，
        如 bash terminate_tree，已先杀进程树）。其它异常 → 收敛为错误 result，不让 loop 崩。
        """
        from ctx_weft.core.loop.driver import make_event
        result_parts: list[str] = []
        metadata: dict[str, Any] = {}
        is_error = False
        try:
            async for ev in provider.invoke(cap_id, sanitized, provider_ctx):
                if ev.kind in ("stdout", "progress"):
                    await self._event_bus.emit(make_event(state, EventType.CAPABILITY_PROGRESS, payload={
                        "invocation_id": invocation_id,
                        "kind": ev.kind,
                        "data": ev.payload.get("data", "")[:500],
                    }))
                elif ev.kind == "result":
                    result_parts.append(ev.payload.get("content", ""))
                    metadata.update(ev.payload.get("metadata", {}))
                elif ev.kind == "error":
                    is_error = True
                    result_parts.append(
                        f"[Error {ev.payload.get('code', 'ERR')}: {ev.payload.get('message', '')}]"
                    )
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await provider.cancel(invocation_id, provider_ctx)
            raise
        except Exception as exc:
            logger.exception("CapabilityGateway: invoke failed for %s", cap_id)
            is_error = True
            result_parts = [f"[Exception: {exc}]"]
        return result_parts, metadata, is_error

    async def _record_result(
        self, state, ctx, tool_name, invocation_id, sanitized, content, is_error, is_dispatch, is_silent, tool_call_id,
    ) -> None:
        """发 CapabilityFinished + ingest TOOL_RESULT（派发暂挂 / SILENT 不入 / 普通写 task 层）。"""
        from ctx_weft.core.loop.driver import make_event
        # 事件 payload 必须先脱敏再截断：content 可能是 list[ContentPart]（见 CONTENT_PARTS_KEY），
        # 直接 `content[:8000]` 切的是**前 8000 个 part**——不报错、语义完全错，且图片 part 的
        # base64 会随 repr 泄漏进事件库。`redact_content_for_event` 对 str 输入返回同一对象，
        # 纯文本路径逐字节不变。
        redacted = redact_content_for_event(content)
        await self._event_bus.emit(make_event(state, EventType.CAPABILITY_FINISHED, payload={
            "invocation_id": invocation_id,
            "capability_name": tool_name,
            "arguments": sanitized,
            "outcome": "error" if is_error else "success",
            "result": redacted[:8000],
            "result_length": len(redacted),
            "tool_call_id": tool_call_id,
        }))
        if not is_dispatch and not is_silent:
            await self._memory.ingest(
                MemoryEvent(
                    kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK,
                    address=_tool_scope(state),
                    content=content,
                    timestamp=now_utc(),
                    role="tool",
                    metadata={
                        "invocation_id": invocation_id,
                        "tool_name": tool_name,
                        "tool_call_id": tool_call_id,
                        "is_error": is_error,
                    },
                ),
                ctx.provider_ctx,
            )

    def _find_provider(self, capability_id: str) -> ToolCapabilityProvider | None:
        prefix = capability_id.rsplit(":", 1)[0]
        return self._provider_index.get(prefix)

    def _get_authorizer(self, capability_id: str) -> Authorizer:
        if capability_id in self._provider_authorizers:
            return self._provider_authorizers[capability_id]
        prefix = capability_id.rsplit(":", 1)[0]
        return self._provider_authorizers.get(prefix, self._default_authorizer)

    async def _maybe_spill(
        self,
        content: str,
        ctx: "LoopContext",
        invocation_id: str,
        tool_name: str,
        spillable: bool = True,
    ) -> str:
        """工具输出超阈值时委托 SpillSink 落盘，返回「截断提示 + 路径 + 头部预览」。

        spillable=False の工具（如 read_file）直接原样返回，不做任何截断或落盘。
        阈值 <=0 或未超出时原样返回。落盘走 SpillSink.spill()——core 不直接碰文件系统。
        无 SpillSink / 该 session 无可落盘位置（spill 抛错）/ 落盘异常时，回退到硬截断
        （保留预览，不丢上下文窗口，但全文不可恢复）。
        """
        if not spillable:
            return content
        if self._spill_threshold <= 0 or len(content) <= self._spill_threshold:
            return content

        original_length = len(content)
        preview = content[: self._spill_preview_chars]
        header = (
            f"[Tool output truncated: {original_length} chars exceeded "
            f"{self._spill_threshold}-char limit"
        )

        provider_ctx = ctx.provider_ctx
        if self._spill_sink is None:
            return (
                f"{header}; no spill sink available, full output dropped]\n"
                f"--- preview (first {len(preview)} chars) ---\n{preview}"
            )

        try:
            path = await self._spill_sink.spill(content, provider_ctx, name_hint=invocation_id)
        except Exception:
            logger.exception("CapabilityGateway: spill failed for '%s'", tool_name)
            return (
                f"{header}; spill failed, full output dropped]\n"
                f"--- preview (first {len(preview)} chars) ---\n{preview}"
            )

        logger.info(
            "CapabilityGateway: spilled %d-char output of '%s' to %s",
            original_length, tool_name, path,
        )
        return (
            f"{header}; full output saved to {path}]\n"
            f"--- preview (first {len(preview)} chars) ---\n{preview}"
        )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _tool_scope(state: "LoopState") -> MemoryAddress:
    """工具调用的 memory scope（session/task/agent）。统一构造，避免重复。"""
    return MemoryAddress(session_id=state.session.id, task_id=state.task.id, agent_id=state.agent.id)


def _coerce_scalar(value: str, json_type: Any) -> Any:
    """把字符串 value 转成 json_type 声明的标量；转不动则原样返回（绝不抛）。"""
    try:
        if json_type == "integer":
            return int(value)
        if json_type == "number":
            return float(value)
        if json_type == "boolean":
            low = value.strip().lower()
            if low in ("true", "1", "yes"):
                return True
            if low in ("false", "0", "no"):
                return False
    except (ValueError, TypeError):
        return value
    return value


def _coerce_args(arguments: dict[str, Any], schema: dict[str, Any] | None) -> dict[str, Any]:
    """按 input_schema 把字符串入参收敛到声明的标量类型。

    防御纵深：即便 schema 正确，模型仍可能给整型参数回传 "3"。这里据 schema 把它转成 int，
    免得工具做算术时崩。未知 key / 非字符串值 / 转不动的值一律原样保留。
    """
    props = (schema or {}).get("properties") or {}
    out = dict(arguments)
    for key, value in arguments.items():
        if not isinstance(value, str):
            continue
        decl = props.get(key)
        if isinstance(decl, dict):
            out[key] = _coerce_scalar(value, decl.get("type"))
    return out


def _strip_unknown_keys(arguments: dict[str, Any], schema: dict[str, Any] | None) -> dict[str, Any]:
    """丢弃 input_schema.properties 未声明的顶层键（对任意调用生效）。

    模型偶尔臆造 schema 里没有的键；畸形缓冲救援也可能抠出带杂键的对象（如把嵌套内层
    ``{"b": 2}`` 当参数）。剥掉它们，只把 schema 声明的参数交给工具，避免杂键流进工具实现，
    也避免错碎片被当成合法调用执行。

    仅当能明确「什么是已知键」时才剥（否则 fail-open 原样返回）：
      - schema 非 dict / 无 ``properties`` → 不剥；
      - 含组合关键字 ``allOf/anyOf/oneOf/not`` 或顶层 ``$ref`` → 键可能由子 schema 声明，不剥；
      - ``additionalProperties`` 显式为 ``True`` 或子 schema（schema 主动允许附加属性）→ 不剥。
    仅剥顶层，不递归进嵌套对象（组合/``$ref`` 下递归易误删）。
    """
    if not isinstance(schema, dict):
        return arguments
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return arguments
    if any(k in schema for k in ("allOf", "anyOf", "oneOf", "not", "$ref")):
        return arguments
    ap = schema.get("additionalProperties")
    if ap is True or isinstance(ap, dict):
        return arguments
    unknown = [k for k in arguments if k not in props]
    if not unknown:
        return arguments
    logger.info("CapabilityGateway: dropping arg keys not declared in schema: %s", unknown)
    return {k: v for k, v in arguments.items() if k in props}


# 只在这三类约束上拦截（spec B）：required 缺失 / type 不符 / enum 越界。
# additionalProperties / format / pattern 等故意忽略，避免未打磨的 schema 误伤现有工具。
_ENFORCED_KEYWORDS = frozenset({"required", "type", "enum"})


def _validate_args(arguments: dict[str, Any], schema: dict[str, Any] | None) -> str | None:
    """按 input_schema 校验入参，返回人读得懂的错误信息（可回灌 LLM）或 None（放行）。

    借 jsonschema 的成熟语义，但只对 required/type/enum 报错（见 _ENFORCED_KEYWORDS）；
    不开 format_checker，故 format 天然不查。schema 缺失/无 properties → 放行。
    任何校验自身异常（含畸形 schema）一律 fail-open，绝不让 loop 因校验崩。
    """
    if not schema or not schema.get("properties"):
        return None
    try:
        validator = jsonschema.Draft202012Validator(schema)
        messages = [
            err.message
            for err in validator.iter_errors(arguments)
            if err.validator in _ENFORCED_KEYWORDS
        ]
    except Exception:
        logger.exception("CapabilityGateway: arg validation crashed; allowing through")
        return None
    return "; ".join(messages) if messages else None


def _sanitize(arguments: dict[str, Any]) -> dict[str, Any]:
    """脱敏 headers 中的敏感 key。"""
    result = dict(arguments)
    if isinstance(result.get("headers"), dict):
        result["headers"] = {
            k: "***" if k.lower() in _REDACT_HEADERS else v
            for k, v in result["headers"].items()
        }
    return result
