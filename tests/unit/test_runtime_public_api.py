"""runtime 公开只读访问器：host 不得绕私有属性（template_resolver 曾被 host 直取 _template_resolver）。"""

from __future__ import annotations

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.providers.llm.mock import MockLLMAdapter
from tests.integration.test_minimal_loop import InMemoryTemplateResolver


def test_template_resolver_is_public_readonly() -> None:
    resolver = InMemoryTemplateResolver()
    rt = CtxWeftRuntime(llm=MockLLMAdapter(responses=[]), template_resolver=resolver)
    assert rt.template_resolver is resolver
