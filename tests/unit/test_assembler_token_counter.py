"""装配链路统一经 ContextRequest.token_counter 计数（默认回退未校准启发式）。"""
from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.assembler.assembler import ContextRequest
from ctx_weft.core.utils.estimate import estimate_tokens
from ctx_weft.protocols import MemoryAddress


def _request(counter=None):
    kw = {} if counter is None else {"token_counter": counter}
    return ContextRequest(
        purpose="act",
        scope=MemoryAddress(session_id="s", task_id="t", agent_id="a"),
        task=SimpleNamespace(id="t"),
        agent=SimpleNamespace(id="a"),
        session=SimpleNamespace(id="s"),
        template=None,
        bound_capabilities=[],
        **kw,
    )


def test_default_counter_is_heuristic():
    assert _request().token_counter is estimate_tokens


def test_counter_field_carried():
    marker = lambda t: 42
    assert _request(marker).token_counter is marker


async def test_guidance_source_uses_request_counter():
    """GuidanceSource only snapshots state; PromptBuilder owns token accounting."""
    from ctx_weft.core.assembler.sources.guidance import GuidanceSource
    from tests.assembler_testkit import compile_prompt

    req = _request(lambda t: 42)
    req.task.unattended = False
    req.task.title = "Task"
    req.task.description = ""
    req.task.user_prompt = ""
    req.task.user_prompt_in_memory = True
    req.task.outputs = ""
    req.session.context_limit = 100_000
    req.session.reserved_output_tokens = 0
    blocks = []
    async for b in GuidanceSource().fetch(req, SimpleNamespace()):
        blocks.append(b)
    assert blocks and isinstance(blocks[0].value, dict)
    assert not hasattr(blocks[0], "token_estimate")
    assert compile_prompt(blocks, req).token_count > 0
