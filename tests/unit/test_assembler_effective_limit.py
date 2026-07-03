"""assembler.assemble 应把 effective_limit(context_limit, reserved_output_tokens)
作为 token_limit 传给 budget，而非裸 context_limit（为 LLM 输出预留余量）。
"""
from types import SimpleNamespace

import pytest

from ctx_weft.core.assembler.assembler import (
    AssemblerDeps,
    ContextAssembler,
    ContextRequest,
)
from ctx_weft.core.assembler.budget import PriorityBudgetStrategy


class _CaptureBudget(PriorityBudgetStrategy):
    seen_limit = None

    async def apply(self, blocks, token_limit, request):
        _CaptureBudget.seen_limit = token_limit
        return blocks


class _StubComposer:
    async def compose(self, blocks, request):
        return SimpleNamespace(system="", messages=[], tools=[], token_count=0, metadata={})


def _req(context_limit: int, reserved_output_tokens: int) -> ContextRequest:
    session = SimpleNamespace(
        id="s1", context_limit=context_limit, reserved_output_tokens=reserved_output_tokens,
    )
    return ContextRequest(
        purpose="act",
        scope=SimpleNamespace(session_id="s1", task_id="t1", agent_id="a1"),
        task=SimpleNamespace(id="t1"),
        agent=SimpleNamespace(id="a1"),
        session=session,
        template=None,
        bound_capabilities=[],
        extra={},
    )


@pytest.mark.asyncio
async def test_assembler_passes_effective_limit():
    """budget 收到的 token_limit = context_limit - reserved_output_tokens。"""
    req = _req(context_limit=100_000, reserved_output_tokens=8192)
    deps = AssemblerDeps(memory=None, knowledge_providers=[], provider_ctx=None)
    asm = ContextAssembler(
        sources=[], budget=_CaptureBudget(), composer=_StubComposer(), deps=deps,
    )
    await asm.assemble(req)
    assert _CaptureBudget.seen_limit == 100_000 - 8192
