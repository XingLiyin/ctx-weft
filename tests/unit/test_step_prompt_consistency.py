"""Cross-step prompt consistency.

Every step (observe / compact / recognize_intent) builds on the SAME act-base
message list — the reconstructed conversation (user / assistant / tool turns) — and
only adds its own role facet + cue on the trailing user message, with capabilities
also on the trailing user message (same as act). Switching steps must therefore
leave the preceding messages essentially unchanged: the assistant/tool conversation
turns are byte-identical across steps, and among the non-act steps everything except
the last user message is identical.
"""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.assembler.assembler import ContextBlock
from ctx_weft.core.assembler.composer import (
    _COMPACTION_INSTRUCTION,
    _RECOGNIZE_INTENT_INSTRUCTION,
    DefaultComposer,
)
from ctx_weft.core.utils import content_to_text

PURPOSES = ["act", "observe", "compact", "recognize_intent"]

# stable substrings of each step's trailing cue (see composer module constants)
_STEP_CUE = {
    "observe": "act as the observer",
    "compact": "act as a memory compactor",
    "recognize_intent": "set this task's metadata",
}


# ── block helpers (mirror what the real sources emit) ────────────────────────

def _identity_block(text: str) -> ContextBlock:
    return ContextBlock(id="id", source="identity", kind="identity", target="system",
                        content=text, priority=0, token_estimate=1, metadata={})


def _cap_block(name: str, kind: str, desc: str) -> ContextBlock:
    return ContextBlock(id=f"cap-{name}", source="capability", kind="capabilities",
                        target="system", content=desc, priority=1, token_estimate=1,
                        metadata={"capability_name": name, "capability_kind": kind})


def _directive_block(text: str) -> ContextBlock:
    return ContextBlock(id="dir", source="identity:skill", kind="directive", target="system",
                        content=text, priority=1, token_estimate=1,
                        metadata={"kind": "skill_instructions", "skill_name": "x"})


def _user_block(content: str, ts: str) -> ContextBlock:
    return ContextBlock(id=f"u-{ts}", source="task_conversation", kind="history",
                        target="messages", content=content, priority=3, token_estimate=1,
                        metadata={"role": "user", "timestamp": ts})


def _assistant_block(content: str, tcid: str, ts: str) -> ContextBlock:
    return ContextBlock(id=f"a-{ts}", source="task_conversation", kind="history",
                        target="messages", content=content, priority=3, token_estimate=1,
                        metadata={"role": "assistant", "timestamp": ts,
                                  "tool_calls": [{"id": tcid, "name": "web", "input": {"q": "x"}}]})


def _tool_block(content: str, tcid: str, ts: str) -> ContextBlock:
    return ContextBlock(id=f"t-{ts}", source="task_conversation", kind="history",
                        target="messages", content=content, priority=3, token_estimate=1,
                        metadata={"role": "tool", "timestamp": ts, "tool_call_id": tcid})


def _multi_turn_blocks() -> list[ContextBlock]:
    """A realistic act conversation: user → assistant(tool_call) → tool → assistant."""
    return [
        _identity_block("PERSONA"),
        _cap_block("report_task_outcome", "tool", "report the outcome"),
        _cap_block("docx", "skill", "make a docx"),
        _cap_block("planner", "agent", "a planning subagent"),
        _directive_block("Do the thing"),
        _user_block("## Current Message\nthe original ask", "1"),
        _assistant_block("let me search", "tc1", "2"),
        _tool_block("search results here", "tc1", "3"),
        _assistant_block("based on results, here is my analysis", "", "4"),
    ]


def _build_for_purpose(blocks: list[ContextBlock], purpose: str):
    c = DefaultComposer()
    # user_prompt_in_memory=True → the history user IS the task message (no extra context turn)
    task = SimpleNamespace(title="T", description="d", user_prompt="the original ask",
                           user_prompt_in_memory=True, process_report=None, outputs=None)
    req = SimpleNamespace(task=task, purpose=purpose)
    if purpose == "act":
        return c._build_actor_messages(blocks, req)
    if purpose == "observe":
        return c._build_observer_messages(blocks, req)
    if purpose == "compact":
        return c._build_facet_trailing_messages(blocks, req, _COMPACTION_INSTRUCTION)
    if purpose == "recognize_intent":
        return c._build_facet_trailing_messages(blocks, req, _RECOGNIZE_INTENT_INSTRUCTION)
    raise AssertionError(purpose)


def _conv_signature(msgs):
    """The conversation base = assistant/tool turns, captured faithfully (incl. tool links)."""
    sig = []
    for m in msgs:
        if m.role == "assistant":
            sig.append(("assistant", content_to_text(m.content),
                        tuple(tc.get("id") for tc in (m.tool_calls or []))))
        elif m.role == "tool":
            sig.append(("tool", content_to_text(m.content), m.tool_call_id))
    return sig


# ── tests ────────────────────────────────────────────────────────────────────

def test_all_steps_share_same_role_sequence() -> None:
    blocks = _multi_turn_blocks()
    seqs = {p: [m.role for m in _build_for_purpose(blocks, p)] for p in PURPOSES}
    base = seqs["act"]
    for p in PURPOSES:
        assert seqs[p] == base, f"{p} role sequence diverged: {seqs[p]} != {base}"


def test_all_steps_have_same_message_count() -> None:
    blocks = _multi_turn_blocks()
    counts = {p: len(_build_for_purpose(blocks, p)) for p in PURPOSES}
    assert len(set(counts.values())) == 1, f"message counts diverged: {counts}"


def test_conversation_base_identical_across_steps() -> None:
    """assistant/tool turns (the prior conversation) are byte-identical across all steps —
    switching step does not perturb what the model already said or the tool results."""
    blocks = _multi_turn_blocks()
    base = _conv_signature(_build_for_purpose(blocks, "act"))
    assert base  # sanity: there ARE assistant/tool turns
    for p in PURPOSES[1:]:
        assert _conv_signature(_build_for_purpose(blocks, p)) == base, \
            f"{p} perturbed the act conversation base"


def test_nonact_steps_differ_only_in_last_user_message() -> None:
    """Among observe/compact/recognize_intent, everything except the last user message is
    identical — the only thing that changes when switching between these steps is the trailing
    cue. (They share directive-on-first + caps-on-last via the same non-act path.)"""
    blocks = _multi_turn_blocks()
    trio = {p: _build_for_purpose(blocks, p) for p in ("observe", "compact", "recognize_intent")}

    def head(msgs):
        return [(m.role, content_to_text(m.content),
                 tuple(tc.get("id") for tc in (m.tool_calls or [])) if m.role == "assistant" else m.tool_call_id)
                for m in msgs[:-1]]

    base_head = head(trio["observe"])
    for p in ("compact", "recognize_intent"):
        assert head(trio[p]) == base_head, f"{p} changed a non-trailing message vs observe"


def test_each_step_appends_its_role_and_cue_on_last_user() -> None:
    blocks = _multi_turn_blocks()
    for p, cue in _STEP_CUE.items():
        last = [m for m in _build_for_purpose(blocks, p) if m.role == "user"][-1].content
        assert "## Your Current Role" in last and "PERSONA" in last, f"{p} missing role facet on last user"
        assert cue in last, f"{p} missing its cue on last user"


def test_capabilities_on_last_user_for_every_step() -> None:
    blocks = _multi_turn_blocks()
    for p in PURPOSES:
        user_msgs = [m for m in _build_for_purpose(blocks, p) if m.role == "user"]
        first, last = user_msgs[0].content, user_msgs[-1].content
        # capabilities ride the trailing user message...
        assert "## Capabilities" in last, f"{p}: capabilities not on last user"
        assert "### Available Tools" in last, f"{p}: tools not on last user"
        # ...never front-loaded onto the (history-derived) first user message
        assert "the original ask" in first
        assert "## Capabilities" not in first, f"{p}: capabilities front-loaded onto first user"
        assert "### Available Tools" not in first


def test_only_observe_carries_observe_cue() -> None:
    """A step's cue does not bleed into other steps (no shared mutable trailing block)."""
    blocks = _multi_turn_blocks()
    joined = {p: "\n".join(content_to_text(m.content) for m in _build_for_purpose(blocks, p))
              for p in PURPOSES}
    assert _STEP_CUE["observe"] in joined["observe"]
    assert _STEP_CUE["observe"] not in joined["act"]
    assert _STEP_CUE["observe"] not in joined["compact"]
    assert _STEP_CUE["compact"] not in joined["observe"]
