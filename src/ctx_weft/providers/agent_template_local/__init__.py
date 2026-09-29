"""agent_template_local：单根目录扫描版 agent template provider + 模板目录格式 loader。

**不含「缺 facet 从 default 模板借」那套**（2026-09-28 移出）：那是部署约定——「有一个叫
default 的母版」是宿主的产品概念，core 不知道也不该知道。宿主要这个行为就自己在它的
AgentCapabilityProvider 里做（host 侧 `providers/templates/provider.py` 即如此）。
"""

from ctx_weft.providers.agent_template_local._loader import TemplateLoader
from ctx_weft.providers.agent_template_local.provider import (
    PROVIDER_NAME,
    LocalAgentTemplateProvider,
)

__all__ = [
    "PROVIDER_NAME",
    "LocalAgentTemplateProvider",
    "TemplateLoader",
]
