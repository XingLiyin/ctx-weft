"""create_session must emit SESSION_CREATED before TASK_CREATED.

Regression: the host projection inserts the task row with a FK to sessions.id. If
TASK_CREATED is projected before SESSION_CREATED, the FK fails and the root task row
is lost from the read model (sqlite3.IntegrityError: FOREIGN KEY constraint failed).
The causal order must be Session → Agent → Task.
"""

from __future__ import annotations

from ctx_weft.providers.events import InProcessEventBus
from ctx_weft.protocols.events import EventType
from ctx_weft.core.orchestrator.agent_registry import AgentRegistry
from ctx_weft.core.orchestrator.session_manager import SessionManager
from ctx_weft.core.orchestrator.template_lookup import TemplateLookup
from ctx_weft.core.runtime import ProviderRegistry
from ctx_weft.protocols import (
    AgentCapabilityProvider,
    AgentTemplate,
    CapabilityProviderInfo,
    IdentityFacet,
    LoopConfig,
    MemoryConfig,
)


class _Resolver(AgentCapabilityProvider):
    name = "agent"

    def __init__(self, t: AgentTemplate) -> None:
        self._t = t

    async def list(self, ctx):
        return []

    async def get_template(self, template_id, version, ctx):
        return self._t

    async def describe(self, ctx):
        return CapabilityProviderInfo(name=self.name)


def _template() -> AgentTemplate:
    return AgentTemplate(
        id="tpl", name="t", version="1",
        identity={"act": IdentityFacet(text="soul")},
        capability_refs=[], memory_config=MemoryConfig(), loop_config=LoopConfig(),
    )


async def test_session_created_emitted_before_task_created() -> None:
    bus = InProcessEventBus()
    order: list[str] = []

    async def rec(ev):
        order.append(ev.type)

    bus.subscribe(None, rec)
    resolver = _Resolver(_template())
    _reg = ProviderRegistry()
    _reg.register_capability(resolver)
    sm = SessionManager(
        agent_registry=AgentRegistry(template_lookup=TemplateLookup(_reg), event_bus=bus),
        event_bus=bus,
    )
    await sm.create_session(template_id="agent:tpl", user_prompt="你好", context_limit=1000)

    assert EventType.SESSION_CREATED in order
    assert EventType.AGENT_INSTANTIATED in order
    assert EventType.TASK_CREATED in order
    # 完整因果序：Session → Agent → Task（docstring 顶部所述）。
    assert (
        order.index(EventType.SESSION_CREATED)
        < order.index(EventType.AGENT_INSTANTIATED)
        < order.index(EventType.TASK_CREATED)
    )
