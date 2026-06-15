"""共享的 @tool 装饰器工厂：进程内 capability provider 用它声明工具。

每个 provider 调用 make_tool_registry(provider_name) 得到独立的 (decorator, tools, impls)
三元组，互不串扰——这样文件系统工具与其他内置工具可以分属不同 provider、各自 PROVIDER 前缀。
"""

from __future__ import annotations

from collections.abc import Callable

from loomex_core.core.utils import extract_schema
from loomex_core.protocols.capability import Purpose, ToolCapability


def make_tool_registry(provider_name: str):
    """返回 (tool 装饰器, tools dict, impls dict)，三者绑定同一 provider 前缀。

    用法::

        tool, _TOOLS, _IMPLS = make_tool_registry("fs")

        @tool(purposes=["act"], side_effects=True)
        async def write_file(...): ...
    """
    tools: dict[str, ToolCapability] = {}
    impls: dict[str, Callable] = {}

    def tool(
        *,
        purposes: list[Purpose],
        side_effects: bool = False,
        description: str | None = None,
    ):
        """声明并注册工具：提取 schema，存函数体为实现。

        description 缺省取 docstring 首行；传入则覆盖（用于运行时动态生成的描述）。
        """
        def decorator(fn: Callable) -> Callable:
            first_line = (fn.__doc__ or "").strip().split("\n")[0].strip()
            cap = ToolCapability(
                id=f"{provider_name}:{fn.__name__}",
                name=fn.__name__,
                kind="tool",
                purposes=list(purposes),
                description=description or first_line,
                input_schema=extract_schema(fn),  # ctx 已在 _SCHEMA_SKIP_DEFAULT 中
                side_effects=side_effects,
            )
            tools[fn.__name__] = cap
            impls[fn.__name__] = fn
            return fn
        return decorator

    return tool, tools, impls
