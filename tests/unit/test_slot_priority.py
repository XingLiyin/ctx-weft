from ctx_weft.core.assembler.priority import slot_priority


def test_structural_floor():
    assert slot_priority("identity") == 0
    assert slot_priority("task_spec") == 0


def test_capabilities_and_directive_tier1():
    assert slot_priority("capabilities") == 1
    assert slot_priority("directive") == 1


def test_agent_compact_summary_tier2():
    # 仅 agent 层跨 task 折叠受保护
    assert slot_priority("history", "agent_compact_summary") == 2


def test_blackboard_tier3():
    assert slot_priority("blackboard") == 3


def test_task_compact_summary_is_task_layer_capsule():
    # task 层胶囊内容，随胶囊走 → 6（已完成基线），不进 tier2
    assert slot_priority("history", "task_compact_summary") == 6


def test_agent_layer_turn_tier5():
    assert slot_priority("history", "agent_conversation_turn") == 5


def test_completed_task_layer_capsule_tier6():
    assert slot_priority("history", "user_prompt") == 6
    assert slot_priority("history", "llm_response") == 6
    assert slot_priority("history", "tool_result") == 6


def test_external_recall_tier7():
    assert slot_priority("reference") == 7
    assert slot_priority("summary") == 7


def test_never_returns_4():
    # 4 = 当前 task 内容，budget 动态提级，slot_priority 不静态返回
    for k, t in [("history", "user_prompt"), ("history", "agent_conversation_turn"),
                 ("history", "agent_compact_summary"), ("blackboard", None),
                 ("capabilities", None), ("identity", None), ("reference", None)]:
        assert slot_priority(k, t) != 4
