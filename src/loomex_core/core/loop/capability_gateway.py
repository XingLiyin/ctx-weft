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

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from loomex_core.core.auth.authorizer import AllowAllAuthorizer, Authorizer  # noqa: F401
from loomex_core.core.events import EventType
from loomex_core.core.events.bus import EventBus
from loomex_core.core.orchestrator.capability_cache import CapabilityCache
from loomex_core.core.utils import generate_id, now_utc
from loomex_core.protocols.capability import CapabilityProvider, ToolCapabilityProvider, qualify
from loomex_core.core.orchestrator.control_capability import PROVIDER_NAME as CONTROL
from loomex_core.protocols.filesystem import SpillSink
from loomex_core.protocols.memory import MemoryEvent, MemoryEventType, MemoryProvider, MemoryScope

if TYPE_CHECKING:
    from loomex_core.core.loop.driver import LoopState, LoopContext

logger = logging.getLogger(__name__)

_REDACT_HEADERS = frozenset({"authorization", "cookie", "x-api-key", "x-auth-token"})

# 派发型控制工具（spec/06 §5）：其 tool_call 落 agent 层 TASK_DISPATCH，即时 result 暂挂，
# 由 child finalize 回填 TASK_DISPATCH_RESULT 配对。普通工具仍走 task 层 TOOL_INVOCATION/RESULT。
DISPATCH_TOOLS = frozenset({
    qualify(f"{CONTROL}:delegate_task"),
    qualify(f"{CONTROL}:delegate_plan"),
    qualify(f"{CONTROL}:replan"),
})

# 编排/裁决型控制工具：其结果是状态信号、不入 task 对话——例如 report_task_outcome 的 HITL 回复
# 改由 finalize 以 role=user 注入。（ask_user 的人类答复是 actor 输入，仍写 task 层。）
# finish_task 同理：其 result 的 canonical 出口是 task.outputs，不入 task 对话。
SILENT_TOOLS = frozenset({
    qualify(f"{CONTROL}:report_task_outcome"),
    qualify(f"{CONTROL}:update_task_metadata"),
    qualify(f"{CONTROL}:finish_task"),
})


# ── InvocationResult ──────────────────────────────────────────────────────────


@dataclass
class InvocationResult:
    """Gateway.invoke() 的结构化返回。ActStep 直接消费，不再处理原始事件流。"""

    invocation_id: str
    tool_name: str
    content: str                              # 拼好的 result 文本，追加进 LLM messages
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
        spill_threshold: int = 8000,
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
        self._default_authorizer: Authorizer = default_authorizer or AllowAllAuthorizer()
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
        """
        from loomex_core.core.events.types import EVENT_TYPES
        from loomex_core.core.loop.driver import make_event

        invocation_id = generate_id("inv")
        is_dispatch = tool_name in DISPATCH_TOOLS
        is_silent = tool_name in SILENT_TOOLS  # 不入 task 对话的编排/裁决工具

        # 1. Lookup capability（只处理 kind="tool"）
        cap = self._cache.get_by_qualified_name(state.agent.id, tool_name)
        if cap is None or cap.kind != "tool":
            return InvocationResult(
                invocation_id=invocation_id,
                tool_name=tool_name,
                content=f"[Error: unknown tool '{tool_name}']",
                is_error=True,
            )

        # 2. Authorization：按 cap.id 前缀取 per-provider authorizer，无则用 default
        authorizer = self._get_authorizer(cap.id)
        decision = await authorizer.authorize(
            cap, state.agent, state.task, ctx, arguments, tool_call_id=tool_call_id,
        )
        if decision.defer:
            # 守住安全不变式：绝不调 provider.invoke；上抛 park 信号 → loop 落 SUSPENDED（spec/07 §7）。
            from loomex_core.core.loop.park import HitlPark
            raise HitlPark(tool_call_id=tool_call_id)
        if not decision.allowed:
            logger.warning("Capability '%s' blocked by authorizer for agent %s", cap.id, state.agent.id)
            content = (
                f"[Blocked by human: {decision.message}]" if decision.message
                else f"[Error: capability '{tool_name}' not authorized]"
            )
            return InvocationResult(
                invocation_id=invocation_id,
                tool_name=tool_name,
                content=content,
                is_error=True,
            )

        # 3. Sanitize arguments（改写参数生效，None → 原参；仍走脱敏）
        effective_args = decision.modified_arguments if decision.modified_arguments is not None else arguments
        sanitized = _sanitize(effective_args)

        # 4. Find provider
        provider = self._find_provider(cap.id)
        if provider is None:
            return InvocationResult(
                invocation_id=invocation_id,
                tool_name=tool_name,
                content=f"[Error: no provider found for '{cap.id}']",
                is_error=True,
            )

        # 5. Emit CapabilityInvoked + ingest TOOL_INVOCATION
        await self._event_bus.emit(make_event(state, EventType.CAPABILITY_INVOKED, payload={
            "invocation_id": invocation_id,
            "capability_name": tool_name,
            "capability_id": cap.id,
            "arguments": sanitized,
        }))
        # 派发工具 → agent 层 TASK_DISPATCH（result 暂挂）；普通能力工具 → task 层 TOOL_INVOCATION；
        # 编排/裁决工具（SILENT_TOOLS）不入 task 对话。
        if is_dispatch or not is_silent:
            await self._memory.ingest(
                MemoryEvent(
                    type=MemoryEventType.TASK_DISPATCH if is_dispatch else MemoryEventType.TOOL_INVOCATION,
                    scope=MemoryScope(
                        session_id=state.session.id,
                        task_id=state.task.id,
                        agent_id=state.agent.id,
                    ),
                    content=f"{tool_name}({sanitized})",
                    timestamp=now_utc(),
                    role="assistant",
                    metadata={"invocation_id": invocation_id, "tool_name": tool_name,
                              "tool_call_id": tool_call_id,
                              # 派发调用保留 arguments，供 agent_experience 重建 delegate_task tool_call
                              **({"arguments": sanitized} if is_dispatch else {})},
                ),
                ctx.provider_ctx,
            )

        # 6. Execute (stream)
        result_parts: list[str] = []
        metadata: dict[str, Any] = {}
        is_error = False

        # 透传 tool_call_id 给 provider（控制工具据此把 origin_tool_call_id 写到 child）
        import dataclasses
        provider_ctx = dataclasses.replace(
            ctx.provider_ctx,
            extra={**ctx.provider_ctx.extra, "tool_call_id": tool_call_id},
        )
        try:
            async for ev in provider.invoke(cap.id, sanitized, provider_ctx):
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
        except Exception as exc:
            logger.exception("CapabilityGateway: invoke failed for %s", cap.id)
            is_error = True
            result_parts = [f"[Exception: {exc}]"]

        content = "\n".join(result_parts) or ("(no output)" if not is_error else "")

        # 工具输出过长 → 委托 fs provider 落盘到 workspace，content 改为「截断提示 + 路径 + 预览」。
        # 在 human note / 审计事件 / memory ingest 之前执行，使所有下游拿到的都是截断版本。
        content = await self._maybe_spill(content, ctx, invocation_id, tool_name)

        # 放行时若人类附了备注，并入结果一并回灌给 LLM
        if decision.message:
            content = f"[Human note: {decision.message}]\n{content}"

        # 7. Emit CapabilityFinished + ingest TOOL_RESULT
        await self._event_bus.emit(make_event(state, EventType.CAPABILITY_FINISHED, payload={
            "invocation_id": invocation_id,
            "capability_name": tool_name,
            "arguments": sanitized,
            "outcome": "error" if is_error else "success",
            "result": content[:8000],
            "result_length": len(content),
        }))
        # 派发工具的 result 暂挂（child finalize 回填）；编排工具不入 task 对话；普通工具写 task 层
        if not is_dispatch and not is_silent:
            await self._memory.ingest(
                MemoryEvent(
                    type=MemoryEventType.TOOL_RESULT,
                    scope=MemoryScope(
                        session_id=state.session.id,
                        task_id=state.task.id,
                        agent_id=state.agent.id,
                    ),
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

        return InvocationResult(
            invocation_id=invocation_id,
            tool_name=tool_name,
            content=content,
            metadata=metadata,
            is_error=is_error,
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
    ) -> str:
        """工具输出超阈值时委托 SpillSink 落盘，返回「截断提示 + 路径 + 头部预览」。

        阈值 <=0 或未超出时原样返回。落盘走 SpillSink.spill()——core 不直接碰文件系统。
        无 SpillSink / 该 session 无可落盘位置（spill 抛错）/ 落盘异常时，回退到硬截断
        （保留预览，不丢上下文窗口，但全文不可恢复）。
        """
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


def _sanitize(arguments: dict[str, Any]) -> dict[str, Any]:
    """脱敏 headers 中的敏感 key。"""
    result = dict(arguments)
    if isinstance(result.get("headers"), dict):
        result["headers"] = {
            k: "***" if k.lower() in _REDACT_HEADERS else v
            for k, v in result["headers"].items()
        }
    return result
