from ctx_weft.providers._tooldecl import make_tool_registry


def test_description_falls_back_to_full_docstring():
    tool, tools, _ = make_tool_registry("x")

    @tool(purposes=["act"])
    async def multi():
        """First line.

        Second line with more detail.
        """

    assert tools["multi"].description == "First line.\n\nSecond line with more detail."


def test_explicit_description_overrides_docstring():
    tool, tools, _ = make_tool_registry("x")

    @tool(purposes=["act"], description="explicit")
    async def fn():
        """Docstring that should be ignored."""

    assert tools["fn"].description == "explicit"
