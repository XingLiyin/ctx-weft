"""Observer cue lists reviewable sub-tasks from task_manager (spec Phase 3 2026-06-30),
independent of any blackboard subscription."""
from ctx_weft.core.assembler.composer import DefaultComposer
from ctx_weft.core.assembler.assembler import ContextRequest, ContextBlock
from ctx_weft.core.state.models import Task, NormalTaskSettings
from ctx_weft.protocols import MemoryAddress


def _req(extra):
    task = Task(id="p1", session_id="s1", status="RUNNING", tenant_id="default",
                title="parent", settings=NormalTaskSettings())
    agent = type("A", (), {"id": "ag1"})()
    session = type("S", (), {"id": "s1"})()
    return ContextRequest(
        purpose="observe", scope=MemoryAddress(session_id="s1", task_id="p1", agent_id="ag1"),
        task=task, agent=agent, session=session, template=None, bound_capabilities=[],
        extra=extra,
    )


def test_observer_cue_lists_subtask_reviews_from_extra():
    composer = DefaultComposer()
    # minimal history block so there is a user turn to anchor the trailing cue
    blocks = [ContextBlock(id="b1", source="t", kind="history", target="messages",
                           content="do the work", priority=3, token_estimate=3,
                           metadata={"role": "user", "type": "user_prompt", "timestamp": "2026-06-30T00:00:00+00:00"})]
    req = _req({"subtask_reviews": [
        {"task_id": "tsk_amy", "title": "向 Amy 问好", "outcome": "finished"},
        {"task_id": "tsk_lily", "title": "向 Lily 问好", "outcome": "failed"},
    ]})
    msgs = composer._build_observer_messages(blocks, req)
    cue = msgs[-1].content
    assert "tsk_amy" in cue and "tsk_lily" in cue, f"cue must list child task_ids; got: {cue}"
    assert "向 Amy 问好" in cue and "failed" in cue
    assert "task_reviews" in cue, "cue must tell the observer to review via task_reviews"


def test_observer_cue_no_subtask_section_when_no_children():
    composer = DefaultComposer()
    blocks = [ContextBlock(id="b1", source="t", kind="history", target="messages",
                           content="do the work", priority=3, token_estimate=3,
                           metadata={"role": "user", "type": "user_prompt", "timestamp": "2026-06-30T00:00:00+00:00"})]
    req = _req({"subtask_reviews": []})
    msgs = composer._build_observer_messages(blocks, req)
    assert "tsk_" not in msgs[-1].content
