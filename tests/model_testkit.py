"""Explicit model snapshots for loop tests that do not use a runtime dispatcher."""

from ctx_weft.core.orchestrator.model import ResolvedModel
from ctx_weft.protocols import OutputPolicy
from ctx_weft.providers.llm.adapters.mock import MockLLMAdapter


def model_snapshot(
    context_limit=100_000,
    output_reserve=0,
    output_ceiling=None,
    *,
    model="mock",
    account="",
    client=None,
    output_policy=OutputPolicy(),
):
    client = client or MockLLMAdapter(
        [], context_limit=context_limit, output_reserve=output_reserve
    )
    return ResolvedModel(
        client=client,
        model=model,
        account=account,
        context_limit=context_limit,
        reserved_output_tokens=output_reserve,
        output_ceiling=output_ceiling,
        output_policy=output_policy,
    )
