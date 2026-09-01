"""HitlRequest 的状态两维化：outcome 存储、resolved/accepted 推导。"""

from ctx_weft.protocols.hitl import (
    HITL_OUTCOME_ACCEPTED,
    HITL_OUTCOME_CANCELLED,
    HITL_OUTCOME_REJECTED,
    HitlRequest,
)


def _req(**kw) -> HitlRequest:
    kw.setdefault("form", "approval")
    return HitlRequest(id="h1", session_id="s", task_id="t", **kw)


def test_new_request_is_unresolved():
    req = _req()
    assert req.outcome == ""
    assert req.resolved is False
    assert req.accepted is False


def test_resolve_sets_outcome_and_derives_resolved():
    req = _req()
    req.resolve(HITL_OUTCOME_ACCEPTED)
    assert req.outcome == "accepted"
    assert req.resolved is True
    assert req.accepted is True


def test_rejected_is_resolved_but_not_accepted():
    req = _req()
    req.resolve(HITL_OUTCOME_REJECTED)
    assert req.resolved is True
    assert req.accepted is False


def test_cancelled_is_resolved_but_not_accepted():
    req = _req()
    req.resolve(HITL_OUTCOME_CANCELLED)
    assert req.resolved is True
    assert req.accepted is False


def test_host_defined_outcome_is_resolved_and_not_accepted():
    """form 开放 → outcome 必须同样开放：自定义结局照样算「已决」，但不是 accepted。

    这是本次重整的目的——旧的四值 Literal 让 host 只能用审批语汇描述自定义 form 的结局。
    """
    req = _req(form="form_fill")
    req.resolve("partially_filled")
    assert req.resolved is True
    assert req.accepted is False
    assert req.outcome == "partially_filled"


def test_resolved_is_derived_not_stored():
    """绕过 resolve() 直接写 outcome，resolved 照样为真。

    这条钉的是「resolved 不是存储字段」——存两份就有一条要维护的不变量，而漏维护的后果是
    find_resolved_for_tool_call 返回 None、把已答过的问题重新问一遍。
    """
    req = _req()
    req.outcome = HITL_OUTCOME_CANCELLED
    assert req.resolved is True


def test_hitl_status_symbol_is_gone():
    """旧词汇必须彻底消失——留只读别名会让人继续按审批语汇写代码。"""
    import ctx_weft.protocols as p
    import ctx_weft.protocols.hitl as h
    assert not hasattr(h, "HitlStatus")
    assert not hasattr(p, "HitlStatus")
    assert "status" not in HitlRequest.__dataclass_fields__
