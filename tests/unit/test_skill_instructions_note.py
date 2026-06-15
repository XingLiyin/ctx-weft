from loomex_core.core.loop.steps.prepare import wrap_skill_instructions


def test_empty_stays_empty():
    assert wrap_skill_instructions("") == ""


def test_note_appended_after_body():
    body = "Run scripts/convert.py to convert the file."
    out = wrap_skill_instructions(body)
    assert out.startswith(body)
    assert "skill_executor__exec_script" in out
    assert "skill_executor__read_file" in out
    assert "skill_executor__list_files" in out
    # the note must come AFTER the body (recency)
    assert out.index("skill_executor__exec_script") > out.index(body)
