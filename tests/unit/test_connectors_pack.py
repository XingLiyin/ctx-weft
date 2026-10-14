"""连接词包的契约，以及「文案只许住在这几个地方」这条守卫。

2026-10-08 的整理：模型可见的散文此前散在 composer / act_guidance / finish_pair / _history /
observe / observing / prepare / verdict / capability_gateway / act / runtime / finalize 十二个
文件里，同一句话有两到三份副本且已经开始漂。现在它们有确定的归宿：

  内置的模型可见文案（编译文案、结构标签、块缺省、运行期事实）
                              → assembler/composer_assets/**/*.jinja
  文案的 typed catalog/API    → assembler/prompts.py（不持有正文）
  Provider 自有文案           → 留在 provider（包括 core/capabilities、core/media）

本文件守的是这件事本身。最后那条 `test_no_prose_literals_outside_their_homes` 是**防回流**的
——没有它，下一次改动又会就地写一句，三个月后再散回去。
"""
from __future__ import annotations

import ast
import pathlib

import pytest
from jinja2 import UndefinedError

from ctx_weft.core.assembler.prompts import (
    AUTO_PLACEHOLDERS,
    BLOCK_FALLBACK,
    CONNECTORS,
    HEADINGS,
    RUNTIME_KEYS,
    connector,
    load_prompt_assets,
    prompt_asset_paths,
    prompt_pack,
    runtime_asset_paths,
    runtime_prompt,
)

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "ctx_weft"


# ── 包本身 ────────────────────────────────────────────────────────────────────


def test_every_registered_connector_has_text_and_vice_versa() -> None:
    """声明与正文必须一一对应——缺键会在装配时静默少一段，多键是改名后的遗留。"""
    assert set(prompt_pack().texts) == set(CONNECTORS)


def test_assets_are_partitioned_by_semantic_role() -> None:
    """文件路径就是语义 key，不再经过自定义 YAML catalog。"""
    assert load_prompt_assets("en") == prompt_pack().texts
    assert all(path.name.endswith((".en.jinja", ".zh.jinja"))
               for path in prompt_asset_paths())


def test_headings_and_runtime_facts_are_asset_owned() -> None:
    """兼容映射只能由 catalog 派生，不能在 prompts.py 重新养第二份正文。"""
    texts = load_prompt_assets("en")
    assert HEADINGS == {
        key.removeprefix("common.heading."): connector(key)
        for key in CONNECTORS
        if key.startswith("common.heading.")
    }
    assert runtime_asset_paths()
    assert "sample" in runtime_prompt("final_reply_note", ref="sample")
    assert set(texts) >= {
        *(f"common.heading.{name}" for name in HEADINGS),
    }


def test_connectors_never_hardcode_an_auto_injected_name() -> None:
    """工具限定名和共享结构标签一律走占位符注入。

    手写字面量的下场有先例：ROLE.md 里那个指向**不存在工具**的名字撒了 6 天谎，没有任何
    报错（见 `core/utils/headings.py` 的注释）。改名时占位符会跟着常量动，字面量不会。
    """
    for key, text in prompt_pack().texts.items():
        for name in AUTO_PLACEHOLDERS.values():
            assert name not in text, f"连接词 {key!r} 写死了自动注入值 {name!r}，改用占位符"


def test_the_digest_sections_match_what_the_parser_expects() -> None:
    """五小节标题是解析契约：cue 说「写这五节」，`_DIGEST_SECTION_RE` 按同样的标题切回来。

    这条断言本该在 `prompts._validate` 里（启动期拦住），但 `prompt_pack()` 在 composer 的
    模块级被调用，而 `loop.steps.compact` 会 import `assembler.assembler`→`composer`——在
    那里 import 它就是一个导入环。所以它搬到这里：同样的保护，不把一个运行期环换进来。
    """
    import re

    from ctx_weft.core.loop.steps.compact import DIGEST_SECTIONS

    for key in ("compact.prompt.task", "compact.prompt.agent"):
        found = tuple(re.findall(r"^## (\w+) ", prompt_pack().texts[key], re.M))
        assert found == DIGEST_SECTIONS, f"{key} 的小节与解析器不一致"


def test_an_unknown_key_or_placeholder_is_a_hard_error() -> None:
    """拼错的键 / 多传的占位符当场报错，不静默渲染出半句话。"""
    with pytest.raises(KeyError):
        connector("review.boundary.does_not_exist")
    with pytest.raises(KeyError):
        connector("common.task_anchor", nope="x")
    with pytest.raises(UndefinedError):
        connector("common.task_anchor")


def test_the_only_block_fallback_is_compact() -> None:
    """块缺省只剩一条。理由见 `prompts.BLOCK_FALLBACK` 上方那段注释。"""
    assert set(BLOCK_FALLBACK) == {"compact"}


@pytest.mark.parametrize("locale", ("en", "zh"))
def test_compact_role_fallback_owns_principles_not_submission(locale: str) -> None:
    """COMPACT facet 讲压缩标准，不讲这一次的形状和提交方式。"""
    role = load_prompt_assets(locale)["compact.role_fallback"]

    assert all(heading not in role for heading in (
        "## Goal", "## Constraints", "## Done", "## Remaining", "## Evidence"))
    assert "{" not in role, "role fallback 不应点名任何工具占位符"
    if locale == "en":
        assert "evidence visible in the conversation" in role
        assert "record replaces the material it covers" in role
        assert "Submit the digest as plain text" not in role
    else:
        assert "对话中可见的证据" in role
        assert "记录会替代它覆盖的原始内容" in role
        assert "以纯文本提交摘要" not in role


@pytest.mark.parametrize("locale", ("en", "zh"))
def test_compact_digest_owns_scope_shape_and_submission(locale: str) -> None:
    """digest cue 讲本次覆盖什么、怎么写、从哪个通道交。"""
    texts = load_prompt_assets(locale)
    for key in ("compact.prompt.task", "compact.prompt.agent"):
        digest = texts[key]
        assert all(heading in digest for heading in (
            "## Goal", "## Constraints", "## Done", "## Remaining", "## Evidence"))
        if locale == "en":
            assert "Submit the digest as plain text" in digest
            assert "call no tools" in digest
            assert "begin directly with `## Goal`" in digest
            assert "Do not mention where the covered range starts or ends" in digest
            assert "input boundary markers such as `{{ progress_heading }}`" in digest
        else:
            assert "以纯文本提交摘要" in digest
            assert "不调用任何工具" in digest
            assert "直接从 `## Goal` 开始" in digest
            assert "不要提及覆盖范围从哪里开始或到哪里结束" in digest
            assert "`{{ progress_heading }}` 之类用于定位原文的边界标记" in digest


@pytest.mark.parametrize("locale", ("en", "zh"))
def test_review_scope_markers_are_input_only(locale: str) -> None:
    """段界用于取材，不能泄漏进最终 act_recap。"""
    texts = load_prompt_assets(locale)
    entry = (SRC / "core/assembler/composer_assets/entries" / f"observe.{locale}.jinja").read_text(encoding="utf-8")
    assert entry.count(f'include "review/context.{locale}.jinja"') == 1
    for key in ("review.judge", "review.recap"):
        assert "review/context" not in texts[key]
    scope = texts["review.context"]
    if locale == "en":
        assert "markers locate the source range only" in scope
        assert "they are not recap content" in scope
        assert "Write the recap itself directly" in scope
    else:
        assert "标记只用于定位原文范围" in scope
        assert "不属于复述内容" in scope
        assert "直接写复述结果" in scope


# ── 直接 Jinja 文件格式 ───────────────────────────────────────────────────────


def test_template_validation_rejects_invalid_or_legacy_jinja() -> None:
    """语法错误和旧占位符在加载文件时直接失败。"""
    from ctx_weft.core.assembler.prompts import _template_variables

    with pytest.raises(ValueError, match="invalid Jinja"):
        _template_variables("{% if live %}", "broken.jinja")
    with pytest.raises(ValueError, match="legacy placeholder"):
        _template_variables("{live}", "legacy.jinja")


def test_each_template_keeps_locales_side_by_side() -> None:
    """语言版本位于同一目录，用文件后缀区分，编辑器可直接按文件打开。"""
    for path in prompt_asset_paths("en"):
        assert path.with_name(path.name.replace(".en.jinja", ".zh.jinja")).is_file()


def test_composer_assets_contain_jinja_without_a_custom_recipe_manifest() -> None:
    """assets 只承载可高亮的 Jinja；结构化装配不再复制成一份定制 YAML。"""
    root = SRC / "core" / "assembler" / "composer_assets"
    assert not list(root.rglob("*.yaml"))
    assert not list((root / "system").glob("*.jinja"))
    assert not list((root / "entries").glob("shared.*.jinja"))
    assert not list((root / "entries").glob("judge.*.jinja"))
    assert not list((root / "entries").glob("recap.*.jinja"))
    assert (root / "entries" / "observe.en.jinja").is_file()
    assert (root / "entries" / "observe.zh.jinja").is_file()
    assert (root / "review" / "judge.en.jinja").is_file()
    assert (root / "review" / "judge.zh.jinja").is_file()


def test_multiline_values_survive_the_round_trip() -> None:
    """直接文件原样承载 markdown 标题、分隔线与空行。"""
    texts = prompt_pack().texts
    assert "\n## Goal " in texts["compact.prompt.task"], "digest 的小节行被切断了"
    assert texts["act.skill_runtime_note"].startswith("---"), "以 --- 开头的段被吃了"
    assert "\n\n" in texts["review.recap"], "正文中间的空行丢了"
    for key, text in texts.items():
        assert text == text.strip(), f"{key} 带了意外的首尾空白"


# ── 防回流守卫 ────────────────────────────────────────────────────────────────

#: 允许继续持有模型可见散文的目录/文件，每条都有理由。
_HOMES = (
    "capabilities/",             # 工具与参数 description（与 extract_schema 读的参数名绑定）
    "media/",                    # 媒体能力的 description + 图片占位符（它自己要解析回来）
)

# 长度启发式抓不到的历史散落项。精确扫 AST 常量/f-string 片段，避免标题、placeholder 和
# resume 短语重新长回使用点。正文只允许出现在 Jinja asset 中。
_MOVED_SHORT_LITERALS = {
    "## Project Background",
    "## Your Current Role",
    "the task: ",
    "the task above",
    "(skill: ",
    "(no reply)",
    "(no final output)",
    "[Context compacted]",
    "[Experience compacted]",
    "Human declined: ",
    "Human rejected the request.",
    "(no response)",
    "(no output)",
    "[Blocked by human: ",
}

#: 启发式扫描的个别假阳性（都不是 prompt），逐条记明。
_NOT_PROMPTS = {
    "models/errors.py",            # 异常消息的 f-string 片段
    "orchestrator/task/hooks.py",  # 文档字符串里写的类型签名
}

_LOGGER_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log", "warn"}


def _prose_literals(path: pathlib.Path) -> list[tuple[int, str]]:
    """文件里的「模型可见散文」候选：长度 ≥60、空格 ≥8、非中文、非 docstring 的字面量。

    排掉 `raise` / `assert` / logger 调用里的——那些是给人看的日志与异常，不进 prompt。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs: set[str] = set()
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docs.add(doc)
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def is_diagnostic(node: ast.AST) -> bool:
        cur = parents.get(node)
        while cur is not None:
            if isinstance(cur, (ast.Raise, ast.Assert)):
                return True
            if isinstance(cur, ast.Call):
                func = cur.func
                if isinstance(func, ast.Attribute) and func.attr in _LOGGER_METHODS:
                    return True
                if isinstance(func, ast.Name) and func.id.endswith(("Error", "Exception")):
                    return True
            cur = parents.get(cur)
        return False

    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        text = node.value
        if len(text) < 60 or text.count(" ") < 8 or text in docs:
            continue
        if is_diagnostic(node):
            continue
        hits.append((node.lineno, text))
    return hits


def test_no_prose_literals_outside_their_homes() -> None:
    """core 里不得再就地写一句模型会读到的话。

    这是整理的**防回流**条款。新增一句模型可见的文案时，先决定它是什么性质的：
      - 编译文案、结构标签、块缺省、运行期事实 → `assembler/composer_assets/`
      - Provider 自有内容                    → 跟着 provider（包括 capabilities/、media/）
    """
    offenders: list[str] = []
    for path in sorted(SRC.joinpath("core").rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if any(home in rel for home in _HOMES) or rel.split("core/", 1)[-1] in _NOT_PROMPTS:
            continue
        for lineno, text in _prose_literals(path):
            offenders.append(f"{rel}:{lineno}  {text[:70]!r}")
    assert not offenders, (
        "这些散文还住在使用点上，给它们找个归宿（见本测试的 docstring）:\n  "
        + "\n  ".join(offenders)
    )


def test_moved_short_prompt_literals_do_not_return_to_python() -> None:
    """短文案同样不得逃过上面的长度启发式。"""
    offenders: list[str] = []
    for path in sorted(SRC.joinpath("core").rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if any(home in rel for home in _HOMES):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and node.value in _MOVED_SHORT_LITERALS):
                offenders.append(f"{rel}:{node.lineno}  {node.value!r}")
    assert not offenders, "短 prompt 又散回 Python 使用点了:\n  " + "\n  ".join(offenders)


def test_the_homes_are_actually_populated() -> None:
    """反向守卫：上面那条白名单不能靠「把东西搬空」通过——归宿里必须真有内容。"""
    assert len(prompt_pack().texts) > 30
    assert len(RUNTIME_KEYS) > 10
    assert len(HEADINGS) > 8
