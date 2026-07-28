"""Smoke test：协议层 import 通畅。"""

from datetime import UTC, datetime


def test_imports():
    from ctx_weft.protocols import (
        AgentTemplate,
        Capability,
        CapabilityProvider,
        IdentityFacet,
        KnowledgeProvider,
        MemoryEvent,
        MemoryEventType,
        MemoryProvider,
        MemoryAddress,
        ProviderContext,
    )

    # 基础数据结构构造
    scope = MemoryAddress(session_id="ses_001", agent_id="agt_001")
    ctx = ProviderContext(session_id="ses_001", tenant_id="default")
    facet = IdentityFacet(text="I am a helpful assistant.")
    event = MemoryEvent(
        type=MemoryEventType.USER_PROMPT,
        address=scope,
        content="hello",
        timestamp=datetime.now(UTC),
    )
    cap = Capability(id="builtin:echo", name="echo", kind="tool", purposes=["act"])

    assert event.type == MemoryEventType.USER_PROMPT
    assert cap.purposes == ["act"]
    assert facet.text.startswith("I am")
    assert ctx.tenant_id == "default"
    assert scope.session_id == "ses_001"


def test_template_construction():
    from ctx_weft.protocols import (
        AgentTemplate,
        CapabilityRef,
        IdentityFacet,
        LoopConfig,
        MemoryConfig,
    )

    template = AgentTemplate(
        id="tpl_test",
        name="test_agent",
        version="0.1.0",
        identity={
            "act": IdentityFacet(text="SOUL: I am helpful."),
            "observe": IdentityFacet(text="ROLE: I evaluate task results."),
        },
        capability_refs=[CapabilityRef(capability_id="builtin:echo")],
        memory_config=MemoryConfig(),
        loop_config=LoopConfig(),
    )

    assert template.identity["act"].text.startswith("SOUL")
    assert template.identity["observe"].text.startswith("ROLE")
    assert template.capability_refs[0].capability_id == "builtin:echo"
    assert template.loop_config.compact_token_ratio == 0.8
