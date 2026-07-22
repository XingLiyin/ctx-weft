"""create_session must emit SESSION_CREATED before TASK_CREATED.

Regression: the host projection inserts the task row with a FK to sessions.id. If
TASK_CREATED is projected before SESSION_CREATED, the FK fails and the root task row
is lost from the read model (sqlite3.IntegrityError: FOREIGN KEY constraint failed).
The causal order must be Session → Agent → Task.
"""

from __future__ import annotations

from ctx_weft.core.events.bus import InProcessEventBus
from ctx_weft.core.events.types import EventType
from ctx_weft.core.orchestrator.agent_capability import TemplateAgentCapabilityProvider
from ctx_weft.core.orchestrator.lifecycle_manager import LifecycleManager
from ctx_weft.core.orchestrator.session_manager import SessionManager
from ctx_weft.core.orchestrator.template_lookup import TemplateLookup
from ctx_weft.core.runtime import ProviderRegistry
from ctx_weft.protocols import (
    AgentTemplate,
    IdentityFacet,
    LoopConfig,
    MemoryConfig,
    TemplateResolver,
)


class _Resolver(TemplateResolver):
    def __init__(self, t: AgentTemplate) -> None:
        self._t = t

    async def get(self, template_id, version, ctx):
        return self._t

    async def list_summaries(self, ctx):
        return []


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
    _reg.register_capability(TemplateAgentCapabilityProvider(resolver))
    sm = SessionManager(
        lifecycle_manager=LifecycleManager(template_lookup=TemplateLookup(_reg)),
        event_bus=bus,
    )
    await sm.create_session(template_id="agent:tpl", user_prompt="你好", context_limit=1000)

    assert EventType.SESSION_CREATED in order
    assert EventType.TASK_CREATED in order
    assert order.index(EventType.SESSION_CREATED) < order.index(EventType.TASK_CREATED)
