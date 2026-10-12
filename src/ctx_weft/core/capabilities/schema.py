"""从 Python 函数签名提取 JSON Schema，供工具声明使用。

原住 `core/utils.py`。消费者只有两类，都在 capability 域：
`capabilities/control_tools.py` 与 `capabilities/skill_executor.py` 的
`@control_tool` / `@skill_executor_tool` 装饰器，以及 `providers/_tooldecl.py` 与
`providers/capability_builtin/provider.py`。归位到这里。
"""

from __future__ import annotations

import inspect
import types
from collections.abc import Callable
from typing import Annotated, Any, Union, get_args, get_origin, get_type_hints

__all__ = ["extract_schema"]


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
