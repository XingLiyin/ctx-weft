"""Common utilities: ID generation, token estimation, content rendering, schema extraction."""

from __future__ import annotations

import inspect
import types
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Union, get_args, get_origin, get_type_hints

from ulid import ULID

if TYPE_CHECKING:
    from ctx_weft.protocols import ContentPart


# 当前任务「上一段执行复述」的统一渲染标题：composer 的非压缩 retry 进度块、以及压缩复用
# act_recap 的 task 层段摘要（role=assistant、task_conversation 来源）都冠以此标题，确保
# 观察者/actor 总能识别"先前进度"锚点。放在 leaf utils 里供 composer 与 _history 共享（避免
# 经 sources 包 __init__ 触发循环 import）。
PROGRESS_SO_FAR_HEADING = "## Progress So Far"

# observe prompt 里「可 review 子任务清单」段的标题前缀：composer 渲染、
# report_task_outcome 的 task_reviews schema 引用（跨层字符串契约，勿散写字面量）。
SUBTASKS_REVIEW_HEADING = "## Your sub-tasks"


def now_utc() -> datetime:
    """UTC current time."""
    return datetime.now(UTC)


def as_utc(dt: datetime) -> datetime:
    """把可能 naive 的 datetime 归一为 aware(UTC)——事件重放 / DB 反序列化可能丢 tz
    （Postgres timestamp-without-tz、裸 isoformat 等），naive 与 aware 直接比较会抛
    TypeError。统一在此把无 tz 者按 UTC 补齐，供跨来源 datetime 排序 / 比较前调用。"""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def generate_id(prefix: str) -> str:
    """Generate a ULID-based primary key (time-sortable + globally unique).

    Format: {prefix}_{ulid}, e.g. ses_01H8K9XPYJ7DRT2RY3JFXSF7M2
    """
    return f"{prefix}_{ULID()}"


def estimate_tokens(text: str) -> int:
    """Rough token estimation (4 chars ~ 1 token)."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def effective_limit(context_limit: int, reserved_output_tokens: int) -> int:
    """装配/压缩预算的有效上限：为 LLM 输出预留余量后的可用输入窗口。"""
    return max(0, context_limit - max(0, reserved_output_tokens))


def dynamic_max_tokens(
    context_limit: int,
    used: int,
    ceiling: int,
    *,
    margin: int = 4096,
    floor: int = 1024,
) -> int:
    """按当前窗口占用实时算请求 max_tokens：clamp(context_limit − used − margin, floor, ceiling)。

    used = caller 估算的本请求真实 prompt token（真实基线 + 本轮增量，见
    gateway.request_prompt_estimate）——纯算术在此，不做估算。ceiling 默认由调用方传
    context_limit（剩余窗口全给输出）；配小则作收紧上限（如 Anthropic 硬输出上限）。
    """
    remaining = context_limit - used - margin
    return max(floor, min(ceiling, remaining))


def content_to_text(content: "str | list[ContentPart]") -> str:
    """Render ContentPart list as plain text (images skipped)."""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for item in content:
        if hasattr(item, "text"):
            parts.append(item.text)
    return "".join(parts)


# ── JSON Schema extraction ────────────────────────────────────────────────────

_PY_TO_JSON: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}

_SCHEMA_SKIP_DEFAULT = frozenset({"ctx"})


def _parse_annotated(ann: Any) -> tuple[str, str]:
    """Annotated[type, description] → (json_type, description)."""
    if get_origin(ann) is Annotated:
        args = get_args(ann)
        base, desc = args[0], str(args[1]) if len(args) > 1 else ""
    else:
        base, desc = ann, ""

    # Unwrap Optional[T] / T | None → T，否则 union 落不进 _PY_TO_JSON 会被误标成 "string"
    # （模型据此回传 "3" 等字符串，工具做算术时崩溃）。取首个非 None 成员。
    if get_origin(base) in (Union, types.UnionType):
        members = [a for a in get_args(base) if a is not type(None)]
        if members:
            base = members[0]

    origin = get_origin(base)
    if origin is list:
        json_type = "array"
    elif origin is dict:
        json_type = "object"
    else:
        json_type = _PY_TO_JSON.get(base, "string")

    return json_type, desc


def extract_schema(
    fn: Callable,
    exclude: frozenset[str] = _SCHEMA_SKIP_DEFAULT,
) -> dict[str, Any]:
    """Build a JSON Schema dict from a function's Annotated type hints.

    Parameters in *exclude* are omitted (used for runtime-injected args like ctx).
    Parameters with defaults become optional; those without become required.
    """
    sig = inspect.signature(fn)
    try:
        hints = get_type_hints(fn, include_extras=True)
    except Exception:
        hints = {}

    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, param in sig.parameters.items():
        if name in exclude or param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue

        ann = hints.get(name, inspect.Parameter.empty)
        if ann is inspect.Parameter.empty:
            json_type, desc = "string", ""
        else:
            json_type, desc = _parse_annotated(ann)

        prop: dict[str, Any] = {"type": json_type}
        if desc:
            prop["description"] = desc

        has_default = param.default is not inspect.Parameter.empty
        if has_default and param.default is not None:
            prop["default"] = param.default

        properties[name] = prop
        if not has_default:
            required.append(name)

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema
