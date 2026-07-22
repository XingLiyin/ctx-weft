"""agent_template_local：单根目录扫描版 agent template provider + 模板目录格式 loader。"""

from ctx_weft.providers.agent_template_local._loader import (
    DEFAULT_MERGE_PURPOSES,
    TemplateLoader,
    merge_default_facets,
)
from ctx_weft.providers.agent_template_local.provider import (
    PROVIDER_NAME,
    LocalAgentTemplateProvider,
)

__all__ = [
    "DEFAULT_MERGE_PURPOSES",
    "PROVIDER_NAME",
    "LocalAgentTemplateProvider",
    "TemplateLoader",
    "merge_default_facets",
]
