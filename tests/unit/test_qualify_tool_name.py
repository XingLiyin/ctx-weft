"""qualify(): capability_id → LLM-safe provider-qualified tool name."""

from __future__ import annotations

from ctx_weft.protocols.capability import qualify


def test_qualify_mcp_id() -> None:
    assert qualify("mcp:github:create_issue") == "mcp__github__create_issue"


def test_qualify_control_id() -> None:
    assert qualify("control:finish_task") == "control__finish_task"


def test_qualify_is_charset_safe() -> None:
    # Anthropic/OpenAI function names: ^[A-Za-z0-9_-]{1,64}$ — no colons.
    name = qualify("mcp:github:create_issue")
    assert ":" not in name


def test_qualify_idempotent_on_bare_name() -> None:
    assert qualify("echo") == "echo"
