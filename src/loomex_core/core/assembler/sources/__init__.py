"""V1 内置的 Source 集合。

设计文档 §5.3：
| Source | 产出 block kind | 数据来源 | 落位 |
| identity | identity | template.identity[purpose] | system |
| capability | capabilities | CapabilityCache 按 purpose 过滤 | system |
| short_memory | history | MemoryProvider.recall_recent | messages |
| blackboard | background / blackboard | MemoryProvider.recall_topic + 订阅 | system / messages |
| long_memory | summary | MemoryProvider.recall_semantic | messages |
| knowledge | reference | KnowledgeProvider.retrieve | messages |
| task_spec | task_spec | 当前 task 描述 | messages（由 Composer 注入） |
"""

from loomex_core.core.assembler.sources.agent_experience import AgentExperienceSource
from loomex_core.core.assembler.sources.blackboard import BlackboardSource
from loomex_core.core.assembler.sources.capability import CapabilitySource
from loomex_core.core.assembler.sources.identity import IdentitySource
from loomex_core.core.assembler.sources.knowledge import KnowledgeRetrievalSource
from loomex_core.core.assembler.sources.long_memory import SemanticRecallSource
from loomex_core.core.assembler.sources.short_memory import RecentMemorySource
from loomex_core.core.assembler.sources.task_spec import TaskSpecSource

__all__ = [
    "AgentExperienceSource",
    "BlackboardSource",
    "CapabilitySource",
    "IdentitySource",
    "KnowledgeRetrievalSource",
    "RecentMemorySource",
    "SemanticRecallSource",
    "TaskSpecSource",
]
