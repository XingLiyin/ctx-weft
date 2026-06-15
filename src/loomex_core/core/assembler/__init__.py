"""Context Assembler：上下文装配流水线。

ContextRequest → [Sources 并行调度] → ContextBlock[] → [BudgetStrategy] → [Composer] → AssembledPrompt

详见设计文档 §5。
"""

from loomex_core.core.assembler.assembler import (
    AssembledPrompt,
    ContextAssembler,
    ContextBlock,
    ContextRequest,
    ContextSource,
)
from loomex_core.core.assembler.budget import BudgetStrategy, PriorityBudgetStrategy
from loomex_core.core.assembler.composer import Composer, DefaultComposer

__all__ = [
    "AssembledPrompt",
    "BudgetStrategy",
    "Composer",
    "ContextAssembler",
    "ContextBlock",
    "ContextRequest",
    "ContextSource",
    "DefaultComposer",
    "PriorityBudgetStrategy",
]
