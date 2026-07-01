from types import SimpleNamespace

import pytest

from ctx_weft.core.assembler.composer import (
    DefaultComposer, _AGENT_COMPACTION_INSTRUCTION, _COMPACTION_INSTRUCTION,
)
from ctx_weft.core.assembler.assembler import ContextBlock
from ctx_weft.core.utils import content_to_text

pytestmark = pytest.mark.asyncio


def _identity(text: str) -> ContextBlock:
    return ContextBlock(id="id", source="identity", kind="identity", target="system",
                        content=text, priority=0, token_estimate=1, metadata={})


def _user(text: str) -> ContextBlock:
    return ContextBlock(id="u", source="task_conversation", kind="history", target="messages",
                        content=text, priority=3, token_estimate=1,
                        metadata={"role": "user", "type": "user_prompt", "timestamp": "1",
                                  "task_id": "t1"})


def _req(scope: str | None):
    task = SimpleNamespace(id="t1", title="T", description="D", user_prompt="do X",
                           user_prompt_in_memory=True, process_report=None, outputs=None)
    extra = {"compact_scope": scope} if scope is not None else {}
    tmpl = SimpleNamespace(identity={"act": SimpleNamespace(text="SOUL", style=None),
                                     "compact": SimpleNamespace(text="COMPACTOR", style=None)})
    return SimpleNamespace(purpose="compact", task=task, template=tmpl, extra=extra)


async def _last_user(scope):
    prompt = await DefaultComposer().compose([_identity("COMPACTOR"), _user("do X")], _req(scope))
    return content_to_text([m for m in prompt.messages if m.role == "user"][-1].content)


async def test_task_scope_uses_task_cue():
    text = await _last_user("task")
    assert _COMPACTION_INSTRUCTION in text
    assert _AGENT_COMPACTION_INSTRUCTION not in text


async def test_agent_scope_uses_agent_cue():
    text = await _last_user("agent")
    assert _AGENT_COMPACTION_INSTRUCTION in text
    assert _COMPACTION_INSTRUCTION not in text


async def test_default_scope_is_task():
    text = await _last_user(None)
    assert _COMPACTION_INSTRUCTION in text
    assert _AGENT_COMPACTION_INSTRUCTION not in text
