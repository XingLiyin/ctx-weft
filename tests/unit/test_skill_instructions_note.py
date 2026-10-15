from types import SimpleNamespace

from ctx_weft.core.assembler.assembler import ContextBlock
from ctx_weft.core.assembler.builder import PromptBuilder
from ctx_weft.core.assembler.context import PromptContext


def test_runtime_note_is_rendered_after_raw_skill_body():
    body = "Run scripts/convert.py to convert the file."
    request = SimpleNamespace(
        purpose="act", intent=None, extra={}, template=None,
        task=SimpleNamespace(
            id="task-1", title="Task", description="", user_prompt="convert",
            user_prompt_in_memory=False,
        ),
        session=SimpleNamespace(context_limit=20_000, reserved_output_tokens=0),
    )
    directive = SimpleNamespace(skill_name="pdf", instructions=body)
    block = ContextBlock(
        id="directive-1", source="skill", kind="directive", value=directive)
    prompt = PromptBuilder(len).compile(PromptContext(request, (block,)))
    out = "\n".join(str(message.content) for message in prompt.messages)

    assert "## Instructions for the current task" in out
    assert "skill_executor__exec_script" in out
    assert "skill_executor__read_file" in out
    assert "skill_executor__list_files" in out
    assert out.index("skill_executor__exec_script") > out.index(body)
