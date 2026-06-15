"""create_session must emit SESSION_CREATED before TASK_CREATED.

Regression: the host projection inserts the task row with a FK to sessions.id. If
TASK_CREATED is projected before SESSION_CREATED, the FK fails and the root task row
is lost from the read model (sqlite3.IntegrityError: FOREIGN KEY constraint failed).
The causal order must be Session → Agent → Task.
"""

from __future__ import annotations

from loomex_core.core.events.bus import InProcessEventBus
from loomex_core.core.events.types import EventType
from loomex_core.core.orchestrator.lifecycle_manager import LifecycleManager
from loomex_core.core.orchestrator.session_manager import SessionManager
from loomex_core.protocols import (
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
    sm = SessionManager(
        lifecycle_manager=LifecycleManager(template_resolver=_Resolver(_template())),
        event_bus=bus,
    )
    await sm.create_session(template_id="tpl", user_prompt="你好", context_limit=1000)

    assert EventType.SESSION_CREATED in order
    assert EventType.TASK_CREATED in order
    assert order.index(EventType.SESSION_CREATED) < order.index(EventType.TASK_CREATED)
