"""runtime 构造期硬校验：模板通道唯一入口是 AgentCapabilityProvider（无 template_resolver 参数）。"""

from __future__ import annotations

import pytest

from ctx_weft.core.runtime import CtxWeftRuntime, ProviderRegistry


def test_runtime_requires_agent_capability_provider() -> None:
    """构造期硬校验：registry 无 AgentCapabilityProvider → ValueError（fail-fast）。"""
    with pytest.raises(ValueError, match="AgentCapabilityProvider"):
        CtxWeftRuntime(providers=ProviderRegistry())
