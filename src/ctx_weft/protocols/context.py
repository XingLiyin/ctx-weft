"""ProviderContext + ContentPart 体系。

ProviderContext 在每次 provider 调用时由 core 注入，携带 session/task/agent 标识、
追踪信息、取消令牌。ContentPart 用于多模态内容（text + image）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Union


# ── 多模态内容部件 ─────────────────────────────────────────────────────────────


@dataclass
class TextPart:
    """文本片段。"""

    text: str
    type: Literal["text"] = "text"


@dataclass
class ImagePart:
    """图像片段。"""

    data: str  # base64 编码或 URL
    media_type: str  # 'image/png' / 'image/jpeg' / ...
    source_type: Literal["base64", "url", "ref"] = "base64"
    type: Literal["image"] = "image"


ContentPart = Union[TextPart, ImagePart]


# ── 边界归一的惰性绑定（Phase 3c Task E2）──────────────────────────────────────

_content_mod: Any = None


def normalize_content_parts(content: Any) -> Any:
    """转调 ``core.content.normalize_content_parts``（dict 形态 part → dataclass）。

    **本函数只是绑定，不是实现**——归一实现全仓唯一一份，在 ``core/content.py``
    （spec §3①：形态转换收在归一层）。这里存在的理由只有两条：

    1. **层序**：protocols 是比 core 低的层，模块级 ``from ctx_weft.core.content
       import ...`` 会把依赖反向（``core/content.py`` 已反向依赖
       ``protocols.filesystem``，今天不成环只是初始化顺序侥幸）。故惰性解析。
    2. **热路径**：三处边界（``LLMMessage`` / ``MemoryRecord`` / ``MemoryEvent``）的
       ``__post_init__`` 无条件调它。把 ``from X import Y`` 直接写在 ``__post_init__``
       里，**每次构造**都要跑一遍 ``__import__`` + ``_handle_fromlist``——实测该语句
       本身就要 361 ns（423.1 ns 的「import + 调用」减去 61.0 ns 的「模块属性 + 调用」），
       把 ``LLMMessage`` 的构造成本从 189 ns 抬到 682 ns。缓存**模块对象**后降到 232 ns。

    缓存的是**模块**而非函数：属性查找留在每次调用时，monkeypatch
    ``core.content.normalize_content_parts`` 仍对本绑定生效（测试可观测性 + 不留
    陈旧绑定的坑）。
    """
    global _content_mod
    if _content_mod is None:
        from ctx_weft.core import content as _m
        _content_mod = _m
    return _content_mod.normalize_content_parts(content)


# ── Citation（KnowledgeDoc 引用信息）────────────────────────────────────────────


@dataclass
class Citation:
    """知识检索结果的引用信息。"""

    url: str | None = None
    title: str | None = None
    anchor: str | None = None  # 段落锚点
    metadata: dict[str, Any] = field(default_factory=dict)


# ── ProviderContext ────────────────────────────────────────────────────────────


@dataclass
class ProviderContext:
    """Provider 调用上下文。core 在每次 provider 操作时构造并注入。

    携带：
    - 当前 session/task/agent 标识（限定操作范围）
    - tenant_id（多租户预留）
    - trace_id（全链路追踪）
    - invocation_id（capability invoke 时分配，用于 cancel）
    - 时间戳
    """

    session_id: str
    tenant_id: str = "default"
    task_id: str | None = None
    agent_id: str | None = None
    trace_id: str | None = None
    invocation_id: str | None = None
    request_id: str | None = None
    timestamp: datetime | None = None  # None 时 provider 自填 now
    skill_name: str = ""  # 供 SkillExecutorCapabilityProvider 读取
    extra: dict[str, Any] = field(default_factory=dict)
