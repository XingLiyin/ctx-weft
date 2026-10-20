"""Small test helpers for the raw-block/Jinja assembler architecture."""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.assembler.builder import PromptBuilder, _history_facts
from ctx_weft.core.assembler.context import PromptContext


def prompt_request(*, task_id: str = "t1", purpose: str = "act", **overrides):
    values = dict(
        purpose=purpose,
        intent=None,
        task=SimpleNamespace(
            id=task_id,
            title="Task",
            description="",
            user_prompt="",
            user_prompt_in_memory=True,
            outputs="",
            unattended=False,
        ),
        agent=SimpleNamespace(id="agent-1"),
        session=SimpleNamespace(context_limit=100_000, reserved_output_tokens=0),
        template=None,
        extra={},
        token_counter=len,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def history_messages(blocks, *, task_id: str = "t1"):
    """Project raw history blocks with the same mapper used before Jinja placement."""
    request = prompt_request(task_id=task_id)
    rows, _refs = _history_facts(PromptContext(request, tuple(blocks)))
    return [row["message"] for row in rows]


def compile_prompt(blocks, request=None, *, tools=None):
    request = request or prompt_request()
    return PromptBuilder(request.token_counter).compile(
        PromptContext(request, tuple(blocks)), tools=tools or [])
