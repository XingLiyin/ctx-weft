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
# 每张图的**地板**。历史上 image_tokens 就等于「张数 × 本常数」，Phase 3c Task D 起
# 它退化成下限：小于 _IMAGE_PART_TOKENS * _IMAGE_BYTES_PER_TOKEN（= 200 KiB）的图仍按
# 它计，更大的图按字节折算。保留地板的两个理由：① 模型侧对任意一张图的固定开销本就在
# 这个量级，往下折算会低估；② 体积未知（存量记录 / ref 且无 byte_size）时的回落值，
# 保证存量数据与不接 BlobStore 的宿主行为不劣化。
_IMAGE_PART_TOKENS = 1600

# ⚠️ 本系数建模的是**字节压力，不是计费 token**。provider 侧会把图降采样，单图真实计费
# 大约就封顶在 1600 附近——拿本口径去算成本是误用。但预算机制的职责是「判断这个请求能不能
# 发出去」，而那由**字节**决定（请求体超上限会被 provider 直接拒），所以估算必须建模字节。
#
# 标定（Phase 3c Task D，依据全部取自仓内实际取值）：
#   · Anthropic 单次请求体上限约 32 MB；base64 膨胀 4/3 → 可容纳原始字节约 24 MiB。
#   · 仓内典型窗口 context_limit = 180_000（core/state/models.py、core/control/types.py、
#     core/control/reducers.py 三处默认值），reserved_output_tokens = 8_192
#     → effective_limit = 171_808。
#   · 令「预算耗尽」与「请求体触顶」对齐：25_165_824 B / 171_808 tok ≈ 146.5 B/tok。
#   · 向下取到 2 的幂 128：取整方向使估算**偏高**（与 estimate_tokens 的「保证单边高估」
#     同向——低估会触发 provider 400，高估只浪费窗口），且 128 = 移位，纯整数运算。
# 校验（compact_token_ratio 默认 0.8，protocols/template.py:79）：
#   · 单张满额图（core/content.py 的 _MAX_IMAGE_BYTES = 5 MiB）= 40_960 tok。
#   · 4 张 = 163_840 tok = 95% of 171_808 → 早已越过 0.8 触发比，compact 必然先行介入。
#   · 5 张 = 204_800 tok > 171_808 → 地板仍超限时 budget.py 抛 ContextOverflowError。
#   · 满预算 171_808 tok = 21 MiB 原始字节 ≈ 28 MB base64，对 32 MB 硬顶留约 13% 余量。
# 即「若干张满额图即超出典型预算」，而旧口径下 5 张满额图账面才 8_000 tok（不到 5%）。
_IMAGE_BYTES_PER_TOKEN = 128


def _dumps_for_estimate(obj: Any) -> str:
    """把 tool_call 参数序列化成供估算的文本；dict/list 走 json，异常回退 str。"""
    if isinstance(obj, str):
        return obj
    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(obj)


def image_part_count(content: "str | list[ContentPart] | None") -> int:
    """content 中图片（非文本 part）的个数。

    对 str / None / 空一律返回 0，与 image_tokens 同口径——两者共用同一判据，
    确保「token 补偿」与「图片数」永不互相矛盾（budget.py 报错文案据此报数，
    不得靠对 image_tokens 整除反推）。
    """
    if not content or isinstance(content, str):
        return 0
    return sum(1 for p in content if not hasattr(p, "text"))


def image_byte_size(part: Any) -> int | None:
    """一个图片 part 的原始（解码后）字节数；无从得知时返回 None。

    两条来源，按可靠度排序：
    1. ``byte_size`` 字段——由 ``core.content`` 在**还有字节**的时候填上
       （validate 解码 inline base64 / normalize 外部化拿到 raw bytes），
       并经 ``content_to_jsonable`` 往返持久化。ref 形态只有这一条路。
    2. inline base64 的载荷长度反解：``len(data) * 3 // 4`` 减去 padding。
       刻意**不解码**——``image_tokens`` 在装配/压缩热路径上被逐条调用，
       为估算去 b64decode 一张 5 MiB 的图是不可接受的开销。

    两条都够不着（ref / url 且无 ``byte_size``，即存量记录与不接 BlobStore 的
    宿主）→ None，由调用方回落到 ``_IMAGE_PART_TOKENS``。
    """
    size = getattr(part, "byte_size", None)
    if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
        return size
    if getattr(part, "source_type", "base64") != "base64":
        return None                        # ref / url：data 不是载荷，长度无意义
    data = getattr(part, "data", "") or ""
    if not isinstance(data, str) or not data:
        return None
    pad = 2 if data.endswith("==") else (1 if data.endswith("=") else 0)
    return max(0, (len(data) * 3) // 4 - pad)


def image_tokens(content: "str | list[ContentPart] | None") -> int:
    """content 中图片（非文本 part）的 token 补偿。不含文本、不含 framing。

    对 str / None / 空一律返回 0——这保证调用方在纯文本路径上是恒等变换，
    可以安全地加在既有的文本计数之后而不改变既有口径。**这条不变量不可破**：
    所有文本口径都建立在它之上。

    单张图 = ``max(_IMAGE_PART_TOKENS, 字节数 // _IMAGE_BYTES_PER_TOKEN)``，
    字节数未知时取 ``_IMAGE_PART_TOKENS``（等价于旧口径，故存量数据与不接
    BlobStore 的宿主行为不劣化）。

    **为什么按字节**（Phase 3c Task D，用户裁定 D2）：原口径是 ``1600 × 张数``，
    对唯一真正变化的维度——体积——毫无反应。5 MiB 截图与 50 KiB 缩略图同价，于是
    唯一能阻止请求体无限膨胀的机制（token 预算）对真实失败模式完全失明：几张大截图
    账面才几千 token（远不触发 compact），实际请求体已 30 MB+ 被 provider 拒，
    而每次 act 都重发全部历史图 → 会话永久卡死且无法自愈。按字节估算之后，
    「预算 → compact → fold → 图片离开视图 → 请求缩小」这条既有链自己就闭合了。

    ⚠️ **本函数建模的是字节压力，不是计费 token**——provider 会降采样，单图真实计费
    大约封顶在 1600。别拿它算成本，见 ``_IMAGE_BYTES_PER_TOKEN`` 的标定说明。

    判据与 ``image_part_count`` 同为 ``not hasattr(p, "text")``（spec §13 冻结），
    两者对「哪些 part 是图」永不互相矛盾；但**数值上不再是 1600 的整数倍**，
    ``budget.py`` 报图片数须继续走 ``image_part_count``，不得对本函数整除反推。

    刻意不接受 count 回调：图片不过 tokenizer。
    """
    if not content or isinstance(content, str):
        return 0
    total = 0
    for part in content:
        if hasattr(part, "text"):
            continue
        size = image_byte_size(part)
        if size is None:
            total += _IMAGE_PART_TOKENS
        else:
            total += max(_IMAGE_PART_TOKENS, size // _IMAGE_BYTES_PER_TOKEN)
    return total


def estimate_content_tokens(content: "str | list[ContentPart] | None", *, count: Callable[[str], int] | None = None) -> int:
    """一条 content 的估算：文本 + 图片 part 固定常数 + 每条 framing 开销。往大了估。

    None/空同样容错（与 content_to_text 对齐）：契约上 content 应为 str | list，但个别路径
    （旧/导入的 memory 记录、None 工具结果等）可能透传 None，估算期须容错而非迭代 None 崩溃。

    count：文本费率经 count 回调走 tokenizer；None 回退未校准启发式（纯单测/无 llm 场景）。
    framing 常数不过回调。

    注意：本函数带 _MSG_FRAMING_TOKENS 补偿，**不是**纯文本恒等的。只在本就计入
    framing 的路径（prepare / llm_gateway）使用；装配与 compact 的估算点请改用
    「既有文本计数 + image_tokens(content)」，见 spec 2026-08-20-multimodal-design §6.5。
    """
    count = count or estimate_tokens
    return _MSG_FRAMING_TOKENS + count(content_to_text(content)) + image_tokens(content)


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
