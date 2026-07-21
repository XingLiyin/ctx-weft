"""Common utilities: ID generation, token estimation, content rendering, schema extraction."""

from __future__ import annotations

import inspect
import json
import re
import types
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Union, get_args, get_origin, get_type_hints

from ulid import ULID

if TYPE_CHECKING:
    from ctx_weft.protocols import ContentPart


# 当前任务「上一段执行复述」的统一渲染标题：composer 的非压缩 retry 进度块、以及压缩复用
# act_recap 的 task 层段摘要（role=assistant、task_conversation 来源）都冠以此标题，确保
# 观察者/actor 总能识别"先前进度"锚点。放在 leaf utils 里供 composer 与 _history 共享（避免
# 经 sources 包 __init__ 触发循环 import）。
PROGRESS_SO_FAR_HEADING = "## Progress So Far"

# observe prompt 里「可 review 子任务清单」段的标题前缀：composer 渲染、
# report_task_outcome 的 task_reviews schema 引用（跨层字符串契约，勿散写字面量）。
SUBTASKS_REVIEW_HEADING = "## Your sub-tasks"


def now_utc() -> datetime:
    """UTC current time."""
    return datetime.now(UTC)


def as_utc(dt: datetime) -> datetime:
    """把可能 naive 的 datetime 归一为 aware(UTC)——事件重放 / DB 反序列化可能丢 tz
    （Postgres timestamp-without-tz、裸 isoformat 等），naive 与 aware 直接比较会抛
    TypeError。统一在此把无 tz 者按 UTC 补齐，供跨来源 datetime 排序 / 比较前调用。"""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def generate_id(prefix: str) -> str:
    """Generate a ULID-based primary key (time-sortable + globally unique).

    Format: {prefix}_{ulid}, e.g. ses_01H8K9XPYJ7DRT2RY3JFXSF7M2
    """
    return f"{prefix}_{ULID()}"


# CJK 表意字 / 假名 / 谚文 / 全角标点等：这些脚本 len//4 会严重低估（真实约 0.6~1 token/字），
# 单列出来按更保守的每字 1.5 token 估。ASCII 起始都 < 0x3000，findall 走 C 级、对大文本仍快。
_CJK_RE = re.compile(
    "["
    "　-〿"      # CJK 标点
    "぀-ヿ"      # 平假名 + 片假名
    "㐀-䶿"      # CJK 扩展 A
    "一-鿿"      # CJK 统一表意
    "가-힯"      # 谚文音节
    "豈-﫿"      # CJK 兼容表意
    "＀-￯"      # 全角/半角形式
    "\U00020000-\U0002fa1f"  # CJK 扩展 B–F + 兼容补充
    "]"
)


# 高熵 ASCII 长串：URL 段/UUID/哈希/hex/base64 等"随机字符"实测（cl100k/o200k）约 0.51~0.54
# token/字符。费率取 0.6 而非 0.5：保证冷启动单边高估（校准系数常态 <1、只向下回收窗口浪费，
# 不向上追赶低估缺口——追赶的过渡期就是 400 风险窗口）。≥20 连续才算：正常英文单词/短标识符
# 达不到，散文不受影响；超长 snake_case 标识符会被略高估（方向安全）。
_DENSE_ASCII_RE = re.compile(r"[A-Za-z0-9+/=_-]{20,}")

# 非 ASCII 字符（CJK 之外落此桶：emoji/西里尔/阿拉伯文/组合符等）：真实 1~3 token/字，
# len/3 严重低估（emoji 混排实测 est/real=0.96）。按 1/字计，方向安全。
_NON_ASCII_RE = re.compile(r"[^\x00-\x7f]")


def estimate_tokens(text: str) -> int:
    """Token 粗估：按脚本/熵分四段、**保证单边高估**（估算只许偏高，低估会触发 provider 400；
    偏高的浪费由 tokenizer 校准系数向下回收——系数常态 <1，收敛前的误差方向恒安全）。

    费率：CJK 表意字/假名/谚文每字 ``ceil(1.5*n)``（真实约 0.6~1.3）；高熵 ASCII 长串
    （≥20 连续 [A-Za-z0-9+/=_-]，URL 段/哈希/base64）``ceil(0.6*n)``（真实约 0.51~0.54）；
    其余非 ASCII（emoji/西里尔等）每字 1（真实约 0.5~2，混排下 ≥1.1x）；ASCII 散文/标点
    ``ceil(len/3)``（真实约 0.25~0.33）。四段相加、非空至少 1。
    """
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    dense = sum(len(m) for m in _DENSE_ASCII_RE.findall(text))
    non_ascii_other = len(_NON_ASCII_RE.findall(text)) - cjk
    ascii_other = len(text) - cjk - dense - non_ascii_other
    return max(
        1,
        (3 * cjk + 1) // 2
        + (3 * dense + 4) // 5
        + non_ascii_other
        + (ascii_other + 2) // 3,
    )


def effective_limit(context_limit: int, reserved_output_tokens: int) -> int:
    """装配/压缩预算的有效上限：为 LLM 输出预留余量后的可用输入窗口。"""
    return max(0, context_limit - max(0, reserved_output_tokens))


def default_output_reserve(context_limit: int) -> int:
    """未显式配置时的输出预留默认 = max(context_limit // 16, 4096)。

    按窗口尺寸取（固定 6.25% 比例）而非固定值：小上下文模型不至被固定 8192 吞光
    （effective_limit 归零），大模型自动放大；固定比例 → compact 触发点在各模型上统一
    （约 75% 原始窗口）。下限 4096 与输出软顶 output_min 对齐。
    """
    return max(context_limit // 16, 4096)


def dynamic_max_tokens(
    context_limit: int,
    used: int,
    ceiling: int,
    *,
    margin: int = 8192,
    floor: int = 1024,
) -> int:
    """按当前窗口占用实时算请求 max_tokens：clamp(context_limit − used − margin, floor, ceiling)。

    used = caller 估算的本请求真实 prompt token（真实基线 + 本轮增量，见
    gateway.request_prompt_estimate）——纯算术在此，不做估算。ceiling 默认由调用方传
    context_limit（剩余窗口全给输出）；配小则作收紧上限（如 Anthropic 硬输出上限）。
    """
    remaining = context_limit - used - margin
    return max(floor, min(ceiling, remaining))


def content_to_text(content: "str | list[ContentPart] | None") -> str:
    """Render ContentPart list as plain text (images skipped).

    None/空按空文本处理（与 estimate_tokens 对齐）——契约上 content 应为 str | list，但
    个别 provider 可能透传 None（如无描述的能力块），装配期须容错而非 raise。
    """
    if not content:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for item in content:
        if hasattr(item, "text"):
            parts.append(item.text)
    return "".join(parts)


# 一条消息/记录里文本 content 之外的计费补偿项（都往大了取，堵低估致 400 的洞）。
# 供 gateway（LLMMessage）与 prepare/composer（memory 记录 / 装配消息）共用，单一真源。
_MSG_FRAMING_TOKENS = 4       # 每条消息的角色/分隔 framing 开销（provider 计费、文本之外）
_IMAGE_PART_TOKENS = 1600     # 每个非文本 part（图片）的保守 token 数（不按 base64 长度算，
                              # 否则一张图几万字符会反向严重高估）


def _dumps_for_estimate(obj: Any) -> str:
    """把 tool_call 参数序列化成供估算的文本；dict/list 走 json，异常回退 str。"""
    if isinstance(obj, str):
        return obj
    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(obj)


def estimate_content_tokens(content: "str | list[ContentPart] | None", *, count: Callable[[str], int] | None = None) -> int:
    """一条 content 的估算：文本 + 图片 part 固定常数 + 每条 framing 开销。往大了估。

    None/空同样容错（与 content_to_text 对齐）：契约上 content 应为 str | list，但个别路径
    （旧/导入的 memory 记录、None 工具结果等）可能透传 None，估算期须容错而非迭代 None 崩溃。

    count：文本费率经 count 回调走 tokenizer；None 回退未校准启发式（纯单测/无 llm 场景）。
    framing 常数不过回调。
    """
    count = count or estimate_tokens
    total = _MSG_FRAMING_TOKENS + count(content_to_text(content))
    if content and not isinstance(content, str):
        total += _IMAGE_PART_TOKENS * sum(1 for p in content if not hasattr(p, "text"))
    return total


def estimate_tool_calls_tokens(tool_calls: "list[dict] | None", *, count: Callable[[str], int] | None = None) -> int:
    """tool_calls（[{name, arguments|input}]）的估算：名字 + 参数 JSON。往大了估。

    纯工具回合 content 常为空、体量全在 arguments 里——不数就会严重低估（致 400 / compact 欠触发）。

    count：文本费率经 count 回调走 tokenizer；None 回退未校准启发式（纯单测/无 llm 场景）。
    """
    count = count or estimate_tokens
    total = 0
    for tc in tool_calls or []:
        total += count(str(tc.get("name", "")))
        total += count(_dumps_for_estimate(tc.get("arguments", tc.get("input", {}))))
    return total


# ── JSON Schema extraction ────────────────────────────────────────────────────

_PY_TO_JSON: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}

_SCHEMA_SKIP_DEFAULT = frozenset({"ctx"})


def _parse_annotated(ann: Any) -> tuple[str, str]:
    """Annotated[type, description] → (json_type, description)."""
    if get_origin(ann) is Annotated:
        args = get_args(ann)
        base, desc = args[0], str(args[1]) if len(args) > 1 else ""
    else:
        base, desc = ann, ""

    # Unwrap Optional[T] / T | None → T，否则 union 落不进 _PY_TO_JSON 会被误标成 "string"
    # （模型据此回传 "3" 等字符串，工具做算术时崩溃）。取首个非 None 成员。
    if get_origin(base) in (Union, types.UnionType):
        members = [a for a in get_args(base) if a is not type(None)]
        if members:
            base = members[0]

    origin = get_origin(base)
    if origin is list:
        json_type = "array"
    elif origin is dict:
        json_type = "object"
    else:
        json_type = _PY_TO_JSON.get(base, "string")

    return json_type, desc


def extract_schema(
    fn: Callable,
    exclude: frozenset[str] = _SCHEMA_SKIP_DEFAULT,
) -> dict[str, Any]:
    """Build a JSON Schema dict from a function's Annotated type hints.

    Parameters in *exclude* are omitted (used for runtime-injected args like ctx).
    Parameters with defaults become optional; those without become required.
    """
    sig = inspect.signature(fn)
    try:
        hints = get_type_hints(fn, include_extras=True)
    except Exception:
        hints = {}

    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, param in sig.parameters.items():
        if name in exclude or param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue

        ann = hints.get(name, inspect.Parameter.empty)
        if ann is inspect.Parameter.empty:
            json_type, desc = "string", ""
        else:
            json_type, desc = _parse_annotated(ann)

        prop: dict[str, Any] = {"type": json_type}
        if desc:
            prop["description"] = desc

        has_default = param.default is not inspect.Parameter.empty
        if has_default and param.default is not None:
            prop["default"] = param.default

        properties[name] = prop
        if not has_default:
            required.append(name)

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema
