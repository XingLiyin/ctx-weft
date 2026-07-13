"""Parser for tool calls / <think> embedded in model text output."""

from ctx_weft.providers.llm.text_calls import (
    DIALECTS,
    ContentGate,
    TextToolCallDialect,
    clean_visible,
    contains_minimax_tool_call,
    contains_tool_call_tag,
    merge_content,
    parse_minimax_tool_calls,
    parse_tool_calls_from_text,
    extract_think,
    scan_text_tool_calls,
    unwrap_raw_arguments,
)


def test_contains_tool_call_tag_detects_tool_call():
    assert contains_tool_call_tag("hi <tool_call>{}</tool_call>")
    assert contains_tool_call_tag("call <function=foo></function>")
    assert not contains_tool_call_tag("just plain text")


def test_parse_json_tool_call():
    text = 'before<tool_call>{"name": "read", "arguments": {"path": "/a"}}</tool_call>'
    scan = parse_tool_calls_from_text(text)
    assert scan.text_before == "before"
    assert len(scan.tool_calls) == 1
    assert scan.tool_calls[0].name == "read"
    assert scan.tool_calls[0].arguments == {"path": "/a"}


def test_parse_json_tool_call_arguments_as_string():
    # Some models put a JSON string in "arguments".
    text = '<tool_call>{"name": "x", "arguments": "{\\"k\\": 1}"}</tool_call>'
    scan = parse_tool_calls_from_text(text)
    assert scan.tool_calls[0].arguments == {"k": 1}


def test_parse_strict_xml_tool_call():
    text = (
        "<tool_call><function=write>"
        "<parameter=path>/tmp/a</parameter>"
        "<parameter=content>hello</parameter>"
        "</function></tool_call>"
    )
    scan = parse_tool_calls_from_text(text)
    assert scan.tool_calls[0].name == "write"
    assert scan.tool_calls[0].arguments == {"path": "/tmp/a", "content": "hello"}


def test_parse_lenient_xml_no_closing_tags():
    text = (
        "<tool_call><function=write>"
        "<parameter=path>/tmp/a"
        "<parameter=content>hello"
        "</tool_call>"
    )
    scan = parse_tool_calls_from_text(text)
    assert scan.tool_calls[0].name == "write"
    assert scan.tool_calls[0].arguments == {"path": "/tmp/a", "content": "hello"}


def test_parse_multiple_tool_calls():
    text = (
        '<tool_call>{"name": "a", "arguments": {}}</tool_call>'
        '<tool_call>{"name": "b", "arguments": {}}</tool_call>'
    )
    scan = parse_tool_calls_from_text(text)
    assert [tc.name for tc in scan.tool_calls] == ["a", "b"]


def test_unclosed_tool_call_tag_marks_open():
    text = 'keep this <tool_call>{"name": "a"'
    scan = parse_tool_calls_from_text(text)
    assert scan.has_open_tag is True
    assert scan.text_before == "keep this"
    assert scan.tool_calls == []


def test_no_tool_call_returns_text_as_before():
    scan = parse_tool_calls_from_text("plain answer")
    assert scan.text_before == "plain answer"
    assert scan.tool_calls == []
    assert scan.has_open_tag is False


def test_extract_think_closed_block():
    reasoning, remaining = extract_think("<think>reasoning here</think>the answer")
    assert reasoning == "reasoning here"
    assert remaining == "the answer"


def test_extract_think_unclosed_block():
    reasoning, remaining = extract_think("answer prefix <think>still thinking")
    assert reasoning == "still thinking"
    assert remaining == "answer prefix"


def test_extract_think_no_tag():
    reasoning, remaining = extract_think("no think tag")
    assert reasoning == ""
    assert remaining == "no think tag"


# ── merge_content (N5: delta vs cumulative) ────────────────────────────────────


def test_merge_content_delta():
    assert merge_content("Hel", "lo") == "Hello"


def test_merge_content_cumulative():
    # provider re-sends the full text so far → not concatenated.
    assert merge_content("Hel", "Hello") == "Hello"


def test_merge_content_first_chunk():
    assert merge_content("", "Hi") == "Hi"


# ── clean_visible (D3: strip tags from visible stream) ─────────────────────────


def test_clean_visible_plain():
    assert clean_visible("hello") == "hello"


def test_clean_visible_removes_closed_think():
    assert clean_visible("a<think>b</think>c") == "ac"


def test_clean_visible_cuts_unclosed_think():
    assert clean_visible("a<think>b") == "a"


def test_clean_visible_cuts_tool_call():
    assert clean_visible("a<tool_call>{...}") == "a"
    assert clean_visible("a<function=f>") == "a"


def test_clean_visible_withholds_trailing_partial_tag():
    assert clean_visible("ab<to") == "ab"


def test_clean_visible_cuts_minimax_tool_call():
    # <minimax:tool_call> 也要从可见正文里扣掉，别把 XML 泄露给用户
    assert clean_visible("答案是\n<minimax:tool_call><invoke name=\"f\"></invoke></minimax:tool_call>") == "答案是\n"


# ── MiniMax 风格 tool call ─────────────────────────────────────────────────────

_MINIMAX = """好的
<minimax:tool_call>
<invoke name="control__delegate_task">
<parameter name="title">迁移规则</parameter>
<parameter name="task_prompt">第一行

第二段
1. 步骤一
2. 步骤二</parameter>
<parameter name="description">迁移</parameter>
</invoke>
</minimax:tool_call>"""


def test_contains_minimax_tool_call():
    assert contains_minimax_tool_call(_MINIMAX)
    assert not contains_minimax_tool_call("plain <tool_call>{}</tool_call>")


def test_parse_minimax_tool_call():
    calls = parse_minimax_tool_calls(_MINIMAX)
    assert len(calls) == 1
    c = calls[0]
    assert c.name == "control__delegate_task"
    assert c.arguments["title"] == "迁移规则"
    assert c.arguments["description"] == "迁移"
    # 多行参数值：内部换行保留
    assert "第二段" in c.arguments["task_prompt"]
    assert "\n" in c.arguments["task_prompt"]


def test_parse_minimax_multiple_invokes():
    text = (
        "<minimax:tool_call>"
        '<invoke name="a"><parameter name="x">1</parameter></invoke>'
        '<invoke name="b"><parameter name="y">2</parameter></invoke>'
        "</minimax:tool_call>"
    )
    calls = parse_minimax_tool_calls(text)
    assert [c.name for c in calls] == ["a", "b"]
    assert calls[0].arguments == {"x": "1"}
    assert calls[1].arguments == {"y": "2"}


def test_parse_minimax_unclosed_block_lenient():
    # 流式截断：缺 </invoke> / </minimax:tool_call> 也要能解析出来
    text = '<minimax:tool_call><invoke name="f"><parameter name="p">v</parameter>'
    calls = parse_minimax_tool_calls(text)
    assert len(calls) == 1
    assert calls[0].name == "f"
    assert calls[0].arguments == {"p": "v"}
    assert clean_visible("ab<") == "ab"


def test_clean_visible_keeps_non_marker_lt():
    assert clean_visible("a<b") == "a<b"


# ── ContentGate (incremental, monotonic) ──────────────────────────────────────


def test_content_gate_emits_incrementally():
    g = ContentGate()
    assert g.feed("hel") == "hel"
    assert g.feed("hello") == "lo"
    assert g.emitted_len == 5


def test_content_gate_withholds_then_releases_after_think():
    g = ContentGate()
    assert g.feed("a<think>b") == "a"
    assert g.feed("a<think>b</think>c") == "c"
    assert g.emitted_len == 2


# ── unwrap_raw_arguments (_raw 兜底哨兵解包 / doom-loop 修复) ────────────────────


def test_unwrap_raw_promotes_valid_inner_json():
    # 模型把 `_raw` 误当参数名照抄 {"_raw": "<合法json>"} → 解包回真实参数。
    args = {"_raw": '{"questions": [{"question": "q?"}]}'}
    assert unwrap_raw_arguments(args) == {"questions": [{"question": "q?"}]}


def test_unwrap_raw_leaves_unparseable_untouched_same_object():
    # 真畸形（缺逗号）→ 原样返回同一对象，交 gateway 报错。
    args = {"_raw": '{"questions": [{"question": "q?" "x": 1}]}'}
    assert unwrap_raw_arguments(args) is args


def test_unwrap_raw_leaves_non_wrapper_same_object():
    # 正常参数（无 _raw）→ 同一对象返回，保证 finalize 处的身份不变（无谓 replace）。
    args = {"questions": [{"question": "q?"}]}
    assert unwrap_raw_arguments(args) is args


def test_unwrap_raw_ignores_extra_keys():
    # 有 _raw 但不是「唯一键」→ 不动（顶层已带真参，不该解包覆盖）。
    args = {"_raw": "{}", "questions": []}
    assert unwrap_raw_arguments(args) is args


def test_unwrap_raw_ignores_non_string_value():
    args = {"_raw": {"already": "dict"}}
    assert unwrap_raw_arguments(args) is args


def test_unwrap_raw_inner_not_object_left_untouched():
    # 内层是合法 JSON 但不是对象（数组/标量）→ 不解包（工具参数必须是对象）。
    args = {"_raw": "[1, 2, 3]"}
    assert unwrap_raw_arguments(args) is args


def test_unwrap_raw_handles_double_wrap():
    args = {"_raw": '{"_raw": "{\\"questions\\": []}"}'}
    assert unwrap_raw_arguments(args) == {"questions": []}


# ── scan_text_tool_calls (dialect entry point) ─────────────────────────────────


def test_scan_wrapped_json_tool_call():
    name, calls = scan_text_tool_calls(
        'ok<tool_call>{"name": "read", "arguments": {"path": "/a"}}</tool_call>'
    )
    assert name == "wrapped"
    assert calls[0].name == "read"
    assert calls[0].arguments == {"path": "/a"}


def test_scan_tool_code_json_tool_and_args_keys():
    # <tool_code> uses {tool, args} instead of {name, arguments}.
    name, calls = scan_text_tool_calls(
        'ok<tool_code>{"tool": "read", "args": {"path": "/a"}}</tool_code>'
    )
    assert name == "wrapped"
    assert calls[0].name == "read"
    assert calls[0].arguments == {"path": "/a"}


def test_scan_tool_code_empty_args_kept():
    # args == {} is valid and must not be dropped by a truthiness check.
    _, calls = scan_text_tool_calls('<tool_code>{"tool": "ping", "args": {}}</tool_code>')
    assert calls[0].name == "ping"
    assert calls[0].arguments == {}


def test_scan_tool_code_xml_fallback_equivalent():
    # <tool_code> supports the same XML degradation path as <tool_call>.
    text = (
        "<tool_code><function=write>"
        "<parameter=path>/tmp/a</parameter>"
        "<parameter=content>hello</parameter>"
        "</function></tool_code>"
    )
    _, calls = scan_text_tool_calls(text)
    assert calls[0].name == "write"
    assert calls[0].arguments == {"path": "/tmp/a", "content": "hello"}


def test_scan_mixed_tool_call_and_tool_code_blocks():
    text = (
        '<tool_call>{"name": "a", "arguments": {}}</tool_call>'
        '<tool_code>{"tool": "b", "args": {}}</tool_code>'
    )
    _, calls = scan_text_tool_calls(text)
    assert [c.name for c in calls] == ["a", "b"]


def test_scan_minimax_dialect():
    name, calls = scan_text_tool_calls(_MINIMAX)
    assert name == "minimax"
    assert calls[0].name == "control__delegate_task"


def test_scan_no_tag_returns_none():
    assert scan_text_tool_calls("just a plain answer") is None


def test_scan_tag_present_but_zero_parsed():
    # Malformed content: dialect detects, parse yields nothing → (name, []).
    name, calls = scan_text_tool_calls("<tool_call>not json and not xml</tool_call>")
    assert name == "wrapped"
    assert calls == []


def test_clean_visible_cuts_tool_code():
    assert clean_visible("答案是\n<tool_code>{...}") == "答案是\n"


def test_dialects_order_wrapped_before_minimax():
    # Global constraint: WRAPPED must precede MINIMAX (preserves _finalize's detection order).
    assert [d.name for d in DIALECTS] == ["wrapped", "minimax"]
    assert all(isinstance(d, TextToolCallDialect) for d in DIALECTS)
