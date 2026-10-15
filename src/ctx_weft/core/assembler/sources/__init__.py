"""Built-in collectors; every Source returns raw values without placement policy."""

from ctx_weft.core.assembler.sources.agent_recall import AgentRecallSource
from ctx_weft.core.assembler.sources.blackboard import BlackboardSource
from ctx_weft.core.assembler.sources.capability import CapabilitySource
from ctx_weft.core.assembler.sources.guidance import GuidanceSource
from ctx_weft.core.assembler.sources.identity import IdentitySource
from ctx_weft.core.assembler.sources.knowledge import KnowledgeRetrievalSource
from ctx_weft.core.assembler.sources.long_memory import SemanticRecallSource
from ctx_weft.core.assembler.sources.task_spec import TaskSpecSource

__all__ = [
    "AgentRecallSource",
    "BlackboardSource",
    "CapabilitySource",
    "GuidanceSource",
    "IdentitySource",
    "KnowledgeRetrievalSource",
    "SemanticRecallSource",
    "TaskSpecSource",
]
