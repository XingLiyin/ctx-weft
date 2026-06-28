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

from ctx_weft.core.auth.authorizer import AllowAllAuthorizer, Authorizer  # noqa: F401
from ctx_weft.core.events import EventType
from ctx_weft.core.events.bus import EventBus
from ctx_weft.core.orchestrator.capability_cache import CapabilityCache
from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols.capability import CapabilityProvider, ToolCapabilityProvider, qualify
from ctx_weft.core.orchestrator.control_capability import PROVIDER_NAME as CONTROL
from ctx_weft.protocols.filesystem import SpillSink
from ctx_weft.protocols.memory import MemoryEvent, MemoryEventType, MemoryProvider, MemoryScope

if TYPE_CHECKING:
    from ctx_weft.core.loop.driver import LoopState, LoopContext

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
# collect_process_report 是 background observe 的终止工具：result 由 run_observe_react 取出落
# close report 槽（→ Process Report），且 background observe 在 task close 后才跑，若入 task 对话
# 会污染已冻结的对话且不被 supersede（泄漏进后续 task prompt）。
SILENT_TOOLS = frozenset({
    qualify(f"{CONTROL}:report_task_outcome"),
    qualify(f"{CONTROL}:update_task_metadata"),
    qualify(f"{CONTROL}:finish_task"),
    qualify(f"{CONTROL}:collect_process_report"),
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
        decision = await authorizer.authorize(
            cap, state.agent, state.task, ctx, arguments, tool_call_id=tool_call_id,
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

        # 5. 记录 invocation（事件 + TOOL_INVOCATION/TASK_DISPATCH 入 memory）
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

        content = "\n".join(result_parts) or ("(no output)" if not is_error else "")
        # 工具输出过长 → 委托 fs provider 落盘；在 human note / 审计 / memory ingest 之前，使下游拿到截断版。
        content = await self._maybe_spill(content, ctx, invocation_id, tool_name, cap.spillable)
        if decision.message:  # 放行时人类备注并入结果回灌 LLM
            content = f"[Human note: {decision.message}]\n{content}"

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
                    type=MemoryEventType.TOOL_RESULT,
                    scope=_tool_scope(state),
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
        """发 CapabilityInvoked + ingest TOOL_INVOCATION（派发→TASK_DISPATCH；SILENT 不入 task 对话）。"""
        from ctx_weft.core.loop.driver import make_event
        await self._event_bus.emit(make_event(state, EventType.CAPABILITY_INVOKED, payload={
            "invocation_id": invocation_id,
            "capability_name": tool_name,
            "capability_id": cap.id,
            "arguments": sanitized,
        }))
        if is_dispatch or not is_silent:
            await self._memory.ingest(
                MemoryEvent(
                    type=MemoryEventType.TASK_DISPATCH if is_dispatch else MemoryEventType.TOOL_INVOCATION,
                    scope=_tool_scope(state),
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
        await self._event_bus.emit(make_event(state, EventType.CAPABILITY_FINISHED, payload={
            "invocation_id": invocation_id,
            "capability_name": tool_name,
            "arguments": sanitized,
            "outcome": "error" if is_error else "success",
            "result": content[:8000],
            "result_length": len(content),
        }))
        if not is_dispatch and not is_silent:
            await self._memory.ingest(
                MemoryEvent(
                    type=MemoryEventType.TOOL_RESULT,
                    scope=_tool_scope(state),
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


def _tool_scope(state: "LoopState") -> MemoryScope:
    """工具调用的 memory scope（session/task/agent）。统一构造，避免重复。"""
    return MemoryScope(session_id=state.session.id, task_id=state.task.id, agent_id=state.agent.id)


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
