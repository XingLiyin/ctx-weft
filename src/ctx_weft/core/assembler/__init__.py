"""Context Assembler：上下文装配流水线。

ContextRequest → [Sources 并行调度] → ContextBlock[] → [BudgetStrategy] → [Composer] → AssembledPrompt

详见设计文档 §5。
"""

from ctx_weft.core.assembler.assembler import (
    AssembledPrompt,
    ContextAssembler,
    ContextBlock,
    ContextRequest,
    ContextSource,
)
from ctx_weft.core.assembler.budget import BudgetStrategy, PriorityBudgetStrategy
from ctx_weft.core.assembler.composer import (
    ActIntent,
    CompactIntent,
    Composer,
    DefaultComposer,
    MetadataIntent,
    PromptIntent,
    PromptPlan,
    ReviewIntent,
)
from ctx_weft.core.assembler.prompt_document import MessageRef, PromptDocument
from ctx_weft.core.assembler.prompts import PromptTemplate, load_prompt_template

__all__ = [
    "AssembledPrompt",
    "ActIntent",
    "BudgetStrategy",
    "Composer",
    "ContextAssembler",
    "ContextBlock",
    "ContextRequest",
    "ContextSource",
    "CompactIntent",
    "DefaultComposer",
    "MetadataIntent",
    "PriorityBudgetStrategy",
    "PromptIntent",
    "PromptPlan",
    "ReviewIntent",
    "MessageRef",
    "PromptDocument",
    "PromptTemplate",
    "load_prompt_template",
]
