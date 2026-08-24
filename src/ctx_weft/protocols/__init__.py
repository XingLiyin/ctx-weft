"""ctx-weft 协议层。

定义 V1 的硬契约：
- KnowledgeProvider（§4.2）
- MemoryProvider（§4.3，统一协议：ingest + recall_* + subscribe + apply_compact）
- CapabilityProvider（§4.4）
- AgentTemplate / IdentityFacet（§4.6）
- LLMClient / LLMClientResolver（§11，LLM 接入契约）
- ProviderContext

零运行时依赖；所有方法 `async def`；所有返回值是 dataclass。
"""

from ctx_weft.protocols.capability import (
    AgentCapability,
    AgentCapabilityProvider,
    Capability,
    CapabilityEvent,
    CapabilityProvider,
    CapabilityProviderInfo,
    Purpose,
    SessionScopedCapabilityProvider,
    SkillCapability,
    SkillCapabilityProvider,
    SkillDefinition,
    ToolCapability,
    ToolCapabilityProvider,
)
from ctx_weft.protocols.context import ContentPart, Citation, ImagePart, ProviderContext, TextPart
from ctx_weft.protocols.filesystem import BLOB_REF_PREFIX, BlobStore, FS_PROVIDER_NAME, FsTool, NullBlobStore, SpillSink
from ctx_weft.protocols.llm import (
    # adapter 契约：实现一个 LLM adapter 所需的全部类型
    LLMCallError,
    LLMChunk,
    LLMClient,
    LLMMessage,
    LLMOutageError,
    LLMRequest,
    LLMTool,
    LLMUsage,
    RAW_ARGS_KEY,
    ToolCall,
    Tokenizer,
    # resolver 契约：仅多账号 LLM provider 需要
    LLMClientResolver,
)
from ctx_weft.protocols.knowledge import (
    KnowledgeDoc,
    KnowledgeProvider,
    KnowledgeProviderInfo,
    KnowledgeQuery,
)
from ctx_weft.protocols.memory import (
    EVENT_LAYER,
    MemoryAddress,
    MemoryEvent,
    MemoryEventType,
    MemoryLayer,
    MemoryScope,
    MemoryProvider,
    MemoryProviderInfo,
    MemoryRecord,
    Subscription,
)
from ctx_weft.protocols.memory_compat import MemoryKind
from ctx_weft.protocols.template import (
    AgentTemplate,
    CapabilityRef,
    IdentityFacet,
    LoopConfig,
    MemoryConfig,
)

__all__ = [
    # Capability
    "AgentCapability",
    "AgentCapabilityProvider",
    "Capability",
    "CapabilityEvent",
    "CapabilityProvider",
    "CapabilityProviderInfo",
    "Purpose",
    "SessionScopedCapabilityProvider",
    "SkillCapability",
    "SkillCapabilityProvider",
    "SkillDefinition",
    "ToolCapability",
    "ToolCapabilityProvider",
    # Filesystem capability
    "FS_PROVIDER_NAME",
    "FsTool",
    "SpillSink",
    # Blob store
    "BLOB_REF_PREFIX",
    "BlobStore",
    "NullBlobStore",
    # Context
    "ContentPart",
    "Citation",
    "ImagePart",
    "ProviderContext",
    "TextPart",
    # LLM — adapter 契约
    "LLMCallError",
    "LLMChunk",
    "LLMClient",
    "LLMMessage",
    "LLMOutageError",
    "LLMRequest",
    "LLMTool",
    "LLMUsage",
    "RAW_ARGS_KEY",
    "ToolCall",
    "Tokenizer",
    # LLM — resolver 契约（多账号 provider）
    "LLMClientResolver",
    # Knowledge
    "KnowledgeDoc",
    "KnowledgeProvider",
    "KnowledgeProviderInfo",
    "KnowledgeQuery",
    # Memory
    "MemoryKind",
    "EVENT_LAYER",
    "MemoryEvent",
    "MemoryEventType",
    "MemoryLayer",  # host 兼容别名（P4c）——host 迁移后删
    "MemoryScope",
    "MemoryProvider",
    "MemoryProviderInfo",
    "MemoryRecord",
    "MemoryAddress",
    "Subscription",
    # Template
    "AgentTemplate",
    "CapabilityRef",
    "IdentityFacet",
    "LoopConfig",
    "MemoryConfig",
]
