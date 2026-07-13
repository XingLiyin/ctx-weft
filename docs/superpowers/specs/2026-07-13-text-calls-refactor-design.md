# text_calls.py 重组 + 方言抽象 + `<tool_code>` 支持

日期:2026-07-13
文件:`src/ctx_weft/providers/llm/text_calls.py`(连带 `_finalize.py`、`tests/unit/test_text_calls.py`)

## 背景与动机

`text_calls.py`(312 行)把四类关注点混在一个平铺文件里,并有三套高度重复的
"文本 tool-call 方言"实现,每套配一对 `contains_* / parse_*` 与各自的正则家族。
`_finalize.py` 第 92–135 行有一条三段近乎复制的 `elif contains_X: parse_X → raise outage` 链。
每加一种方言,要同步改 `_finalize` 的 elif 链、`_VISIBLE_MARKERS`、`contains_*` 三处,
漏改任一处就产生"解析器认得、可见门不认、标签泄漏进正文"这类 bug。

现在要新增 `<tool_code>` 标签(与 `<tool_call>` **完全对等**:JSON `{tool, args}` 键 +
XML 退化路径),照现状复制只会加剧混乱。

### 已确认事实
- `TextScan.text_before` / `has_open_tag` 是**生产死代码**:两个 adapter(openai/anthropic)
  只用 `ContentGate` + `merge_content`;`_finalize.py` 只读 `.tool_calls`。仅测试在断言这两字段。
- `contains_tool_call_tag` 的检测集 = `{<tool_call>, <function=}`,`_VISIBLE_MARKERS` 同;
  两处本质是同一份"起始标签集",却各写各的。

## 目标

1. 单文件内按关注点重组为 4 个清晰段落(**不拆分成多文件**)。
2. 引入方言抽象,消除三套重复;新增方言 = 往一张表加一项。
3. 新增 `<tool_code>`,与 `<tool_call>` 对等。
4. 删除死代码 `TextScan`;旧的 `contains_* / parse_*` 签名直接替换为方言 API,测试跟随更新。
5. 除 `<tool_code>` 外,现有所有格式行为保持不变。

## 设计

### 段落① 共享类型 & `_raw` 哨兵
- `unwrap_raw_arguments`(不变)、`ParsedToolCall`(不变)。
- **删除** `TextScan`。

### 段落② 文本 tool-call 方言(抽象核心)

```python
@dataclass(frozen=True)
class TextToolCallDialect:
    name: str                      # 日志/错误信息用
    open_markers: tuple[str, ...]  # 起始标签集:同时用于 detect 与可见门
    parse: Callable[[str], list[ParsedToolCall]]

    def detect(self, text: str) -> bool:
        return any(m in text for m in self.open_markers)
```

检测与可见门都从 `open_markers` 派生,不再单独维护 `contains_*` 与 `_VISIBLE_MARKERS`。

两个方言(顺序 = 现有 finalize 检测顺序,wrapped 先于 minimax):

- **WRAPPED**(`<tool_call>` / `<tool_code>`)
  - `open_markers = ("<tool_call>", "<tool_code>", "<function=")`
  - 块匹配正则用反向引用配对:`<(tool_call|tool_code)>\s*(.*?)\s*</\1>`(DOTALL)。
  - 每块内容按 **JSON → 严格 XML → 宽松 XML** 解析(沿用现有 `_parse_single_tool_call` /
    `_parse_xml_tool_call` 逻辑)。
  - JSON 分支同时认两套键:name 取 `data.get("name") or data.get("tool")`;
    参数按 **presence** 取——`arguments in data` 优先,否则 `args in data`,否则 `{}`
    (不能用真值判断,`args` 可能是合法的 `{}`)。字符串参数仍尝试 `json.loads`,
    非 dict 归 `{}`(不变)。
- **MINIMAX**(`<minimax:tool_call>`)
  - `open_markers = ("<minimax:tool_call>",)`
  - `parse` 沿用现有 `parse_minimax_tool_calls` 逻辑(含未闭合块兜底)。

```python
DIALECTS: tuple[TextToolCallDialect, ...] = (WRAPPED_DIALECT, MINIMAX_DIALECT)

def scan_text_tool_calls(text: str) -> tuple[str, list[ParsedToolCall]] | None:
    """首个 detect 命中的方言 → (name, calls);都不命中 → None。
    (name, []) 表示"标签在但零解析"——供 finalize 判 outage 退避。"""
    for d in DIALECTS:
        if d.detect(text):
            return d.name, d.parse(text)
    return None
```

### 段落③ 流式可见门
- `merge_content`、`clean_visible`、`ContentGate` 逻辑不变。
- `_VISIBLE_MARKERS` 改为派生:
  ```python
  _VISIBLE_MARKERS = (THINK_START, *(m for d in DIALECTS for m in d.open_markers))
  ```
  `<tool_code>` 因此自动进门,根除三处手动同步的 bug 类。

### 段落④ think 抽取
- `extract_think` 不变。

### 连带改动:`_finalize.py`
第 92–135 行 elif 链塌缩为:
```python
scan = scan_text_tool_calls(content_text)
if scan is not None:
    dialect_name, parsed = scan
    if not parsed:
        raise LLMCallError(
            f"LLM emitted a {dialect_name} tool call tag that parsed to zero tool calls "
            "(truncated or malformed text tool call)",
            retriable=True, outage=True,
        )
    out.extend(
        LLMChunk(kind="tool_call", tool_call=ToolCall(
            id=generate_id("call"), name=p.name,
            arguments=unwrap_raw_arguments(p.arguments),
        ))
        for p in parsed
    )
```
删除对 `contains_tool_call_tag` / `contains_minimax_tool_call` /
`parse_tool_calls_from_text` / `parse_minimax_tool_calls` 的 import。

## 对外 API 变化

移除:`TextScan`、`contains_tool_call_tag`、`contains_minimax_tool_call`、
`parse_tool_calls_from_text`、`parse_minimax_tool_calls`。
新增:`TextToolCallDialect`、`DIALECTS`、`scan_text_tool_calls`。
不变:`unwrap_raw_arguments`、`ParsedToolCall`、`merge_content`、`clean_visible`、
`ContentGate`、`extract_think`。

## 测试

`tests/unit/test_text_calls.py` 更新:
- 现有 JSON / 严格 XML / 宽松 XML / 多块 tool_call 用例改用 `scan_text_tool_calls`。
- 现有 minimax 用例改用 `scan_text_tool_calls`(name == "minimax")。
- 删除 `TextScan.text_before` / `has_open_tag` 断言(字段已移除);未闭合标签的
  "扣下正文尾部"行为改由 `clean_visible` / `ContentGate` 用例覆盖(已有)。
- **新增 `<tool_code>` 用例**:
  - `{"tool": "read", "args": {"path": "/a"}}` 正确解析出 name/arguments。
  - `args` 为 `{}` 时不被误判丢弃。
  - `<tool_code>` 内 XML 退化路径与 `<tool_call>` 等价。
  - `clean_visible` 把 `<tool_code>...` 从可见正文扣掉。
  - tool_call 与 tool_code 混排多块。
- `merge_content` / `unwrap_raw_arguments` / `extract_think` 用例不变。

## 非目标(YAGNI)
- 不给 `<tool_code>` 加 Python 表达式负载解析(已确认里面是 JSON)。
- 不重构 openai/anthropic adapter(它们只依赖 `ContentGate`/`merge_content`,接口不变)。
- 不新拆物理文件。
