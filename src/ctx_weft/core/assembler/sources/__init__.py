"""V1 内置的 Source 集合。

设计文档 §5.3：
| Source | 产出 block kind | 数据来源 | 落位 |
| identity | identity | template.identity[purpose] | system |
| capability | capabilities | CapabilityCache 按 purpose 过滤 | system |
| agent_recall | history | recall_recent_by_agent + recall_recent | messages |
| blackboard | background / blackboard | MemoryProvider.recall_topic + 订阅 | system / messages |
| long_memory | summary | MemoryProvider.recall_semantic | messages |
| knowledge | reference | KnowledgeProvider.retrieve | messages |
| task_spec | task_spec | 当前 task spec（metadata）| Composer 读 metadata 装饰当前消息 |
"""

from ctx_weft.core.assembler.sources.agent_recall import AgentRecallSource
from ctx_weft.core.assembler.sources.blackboard import BlackboardSource
from ctx_weft.core.assembler.sources.capability import CapabilitySource
from ctx_weft.core.assembler.sources.identity import IdentitySource
from ctx_weft.core.assembler.sources.knowledge import KnowledgeRetrievalSource
from ctx_weft.core.assembler.sources.long_memory import SemanticRecallSource
from ctx_weft.core.assembler.sources.task_spec import TaskSpecSource

__all__ = [
    "AgentRecallSource",
    "BlackboardSource",
    "CapabilitySource",
    "IdentitySource",
    "KnowledgeRetrievalSource",
    "SemanticRecallSource",
    "TaskSpecSource",
]
