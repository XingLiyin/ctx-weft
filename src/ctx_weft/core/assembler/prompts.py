"""Composer 模板与模型可见框架文案的 catalog。

装配一条 prompt 要四样东西，本模块管其中三样（第一样不在这里）：

  1. **块** —— 四块人格文本（SOUL / ROLE / COMPACT / METADATA），来源是 template 的
     facet，由 `sources/identity.py` 取、`composer` 渲染。**不在本模块**。
  2. **编译模板** —— 按语义 key 分在 `composer_assets/<key>.<locale>.jinja`。文件内容就是
     最终 prompt，不再包在 YAML 字符串里；分支直接使用标准 Jinja。
  3. **结构标签** —— 模型的定位锚点（`## Current Task` 之类），正文同样来自 assets；
     其中几个是跨层甚至跨仓契约（见 `core/utils/headings.py`），由常量自动注入，避免漂移。
  4. **块缺省** —— facet 缺失时 core 顶上的文案。**只有一条**（压缩纪律），理由见
     `BLOCK_FALLBACK`。

另有 **运行期事实**（`runtime_prompt`）：写进 memory 或作为工具结果返回、模型会读到，但不参与
装配。它们和编译模板的差别是「陈述系统做了什么」而非「指导这一次做什么」；正文仍集中在
`composer_assets/runtime/*.jinja`，使用同一 Jinja 环境显式传参渲染，不再还原成单花括号字符串。

为什么集中：改造前这些字符串散在 composer / act_guidance / finish_pair / _history /
observe / observing / prepare / verdict / capability_gateway / act 十个文件里，同一句话
有两到三份副本且已经开始漂（`act.resume` 的兜底版少了「别重做」那半句，`## Progress So
Far` 在 `control_tools` 里是第三份裸字面量）。集中之后「这句话是什么性质的」由它住哪儿
回答。

**不在这里的两类**（都是故意的）：

- Provider 自有内容 —— 身份 facet、工具/参数描述、工具结果、分页和恢复说明都由宿主或
  provider 决定，不集中到这里。也包括 core/capabilities 和 core/media 内的内置 provider。
  assembler 只负责这些数据外面的框架标题、连接语与排版，不改写 provider 正文。
- 解析契约 —— `DIGEST_SECTIONS` / `COLLAPSE_DELIM` / `INTERRUPTED_MARK` 等住各自的解析
  点。digest 的小节标题通过测试与解析器的 `DIGEST_SECTIONS` 核对，避免跨层循环导入。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from jinja2 import (
    Environment,
    FileSystemLoader,
    StrictUndefined,
    TemplateSyntaxError,
    UndefinedError,
    meta,
    nodes,
)
from jinja2.runtime import Macro

from ctx_weft.core.capabilities.control_tools import (
    ASK_USER_NAME,
    DELEGATE_TASK_NAME,
    FINISH_TASK_NAME,
    REPORT_OBSERVATION_NAME,
    UPDATE_TASK_METADATA_NAME,
)
from ctx_weft.core.capabilities.skill_executor import (
    EXEC_SCRIPT_NAME,
    LIST_FILES_NAME,
    READ_FILE_NAME,
)
from ctx_weft.core.utils.headings import (
    FINAL_OUTPUT_HEADING,
    PROGRESS_SO_FAR_HEADING,
    SUBTASKS_HEADING,
)

__all__ = [
    "BLOCK_FALLBACK",
    "CONNECTORS",
    "HEADINGS",
    "RUNTIME_KEYS",
    "runtime_asset_paths",
    "runtime_prompt",
    "PromptPack",
    "PromptTemplate",
    "load_prompt_template",
    "connector",
    "prompt_pack",
    "render_template_file",
]


# ── 结构标签 ──────────────────────────────────────────────────────────────────
#
# 模型的定位锚点。正文来自 common asset；跨层契约仍由 `core/utils/headings.py` 定义，
# Template catalog. Prompt bodies live at composer_assets/<key>.<locale>.jinja.
# Runtime variables are derived from the Jinja AST instead of repeated in Python.

AUTO_PLACEHOLDERS: dict[str, str] = {
    "finish_task": FINISH_TASK_NAME,
    "delegate_task": DELEGATE_TASK_NAME,
    "ask_user": ASK_USER_NAME,
    "report_observation": REPORT_OBSERVATION_NAME,
    "update_task_metadata": UPDATE_TASK_METADATA_NAME,
    "exec_script": EXEC_SCRIPT_NAME,
    "read_file": READ_FILE_NAME,
    "list_files": LIST_FILES_NAME,
    "progress_heading": PROGRESS_SO_FAR_HEADING,
    "subtasks_heading": SUBTASKS_HEADING,
    "final_output_heading": FINAL_OUTPUT_HEADING,
}

# Fragment key -> inputs read by its Jinja template and included fragments.
CONNECTORS: dict[str, frozenset[str]]

PROMPT_LOCALES = ("en", "zh")
_ASSET_ROOT = Path(__file__).with_name("composer_assets")
_JINJA = Environment(
    loader=FileSystemLoader(_ASSET_ROOT),
    undefined=StrictUndefined,
    autoescape=False,
    trim_blocks=False,
    lstrip_blocks=False,
    keep_trailing_newline=False,
)
_LEGACY_PLACEHOLDER_RE = re.compile(r"(?<!\{)\{([a-z_][a-z0-9_]*)\}(?!\})")


def _template_variables(text: str, name: str, visited: frozenset[str] = frozenset()) -> set[str]:
    """Include fragment inputs in the contract; locally bound names stay local."""
    if legacy := _LEGACY_PLACEHOLDER_RE.findall(text):
        raise ValueError(
            f"prompt template {name!r} uses legacy placeholder(s) {sorted(set(legacy))}; "
            "use Jinja {{ variable }} syntax")
    try:
        parsed = _JINJA.parse(text)
    except TemplateSyntaxError as exc:
        raise ValueError(f"prompt template {name!r} has invalid Jinja: {exc}") from exc
    variables = set(meta.find_undeclared_variables(parsed))
    local_names = {node.name for node in parsed.find_all(nodes.Name) if node.ctx == "store"}
    for dependency in meta.find_referenced_templates(parsed):
        if dependency is not None and dependency not in visited | {name}:
            source = _JINJA.loader.get_source(_JINJA, dependency)[0]
            variables.update(_template_variables(source, dependency, visited | {name}) - local_names)
    return variables


def _without_file_newline(text: str) -> str:
    """Template files end with a source newline; prompt values intentionally do not."""
    return text[:-1] if text.endswith("\n") else text


def _localized_template_name(key: str, locale: str) -> str:
    return f"{key.replace('.', '/')}.{locale}.jinja"


def _template_key(path: Path, locale: str) -> str:
    relative = path.relative_to(_ASSET_ROOT)
    suffix = f".{locale}.jinja"
    if not relative.name.endswith(suffix):
        raise ValueError(f"not a localized prompt template: {relative.as_posix()}")
    leaf = relative.name.removesuffix(suffix)
    return ".".join((*relative.parts[:-1], leaf))


def prompt_asset_paths(locale: str | None = None) -> tuple[Path, ...]:
    """Return localized whole-text fragments (excluding entry/runtime macro modules)."""
    if locale is not None and locale not in PROMPT_LOCALES:
        raise ValueError(
            f"unsupported prompt locale {locale!r}; expected one of {PROMPT_LOCALES}")
    if locale is not None:
        return tuple(sorted(p for p in _ASSET_ROOT.rglob(f"*.{locale}.jinja")
                            if p.relative_to(_ASSET_ROOT).parts[0] not in {"entries", "runtime"}))
    return tuple(sorted(
        path
        for known_locale in PROMPT_LOCALES
        for path in prompt_asset_paths(known_locale)
    ))


def load_prompt_assets(locale: str = "en") -> dict[str, str]:
    """Load one locale directly from its Jinja files."""
    if locale not in PROMPT_LOCALES:
        raise ValueError(
            f"unsupported prompt locale {locale!r}; expected one of {PROMPT_LOCALES}")
    merged: dict[str, str] = {}
    for path in prompt_asset_paths(locale):
        key = _template_key(path, locale)
        if key in merged:
            raise ValueError(f"duplicate prompt asset key {key!r}")
        text = _without_file_newline(path.read_text(encoding="utf-8"))
        _template_variables(text, path.relative_to(_ASSET_ROOT).as_posix())
        merged[key] = text
    if not merged:
        raise ValueError(f"no prompt templates found for locale {locale!r}")
    return merged


def _build_connector_registry() -> dict[str, frozenset[str]]:
    """Derive the runtime context contract and verify every locale matches it."""
    catalogs = {locale: load_prompt_assets(locale) for locale in PROMPT_LOCALES}
    base = catalogs[PROMPT_LOCALES[0]]
    base_keys = set(base)
    auto = set(AUTO_PLACEHOLDERS)
    for locale, texts in catalogs.items():
        if set(texts) != base_keys:
            raise ValueError(
                f"prompt locale {locale!r} key mismatch: "
                f"missing {sorted(base_keys - set(texts))}, "
                f"extra {sorted(set(texts) - base_keys)}")
        for key, text in texts.items():
            expected = _template_variables(base[key], _localized_template_name(key, "en"))
            actual = _template_variables(text, _localized_template_name(key, locale))
            if actual != expected:
                raise ValueError(
                    f"prompt locale {locale!r} variables differ for {key!r}: "
                    f"missing {sorted(expected - actual)}, extra {sorted(actual - expected)}")
    return {
        key: frozenset(
            _template_variables(text, _localized_template_name(key, "en")) - auto)
        for key, text in base.items()
    }


CONNECTORS = _build_connector_registry()


@dataclass(frozen=True)
class PromptPack:
    """Rendering facade over one locale's whole-text Jinja fragments."""

    texts: dict[str, str]

    def __call__(self, key: str, **values: object) -> str:
        try:
            text = self.texts[key]
        except KeyError:
            raise KeyError(f"unknown connector {key!r}") from None
        return _render_text(text, CONNECTORS[key], key, values)


def _validate_render_values(
    allowed: frozenset[str], key: str, values: dict[str, object],
) -> None:
    for name in values:
        if name not in allowed:
            raise KeyError(f"prompt template {key!r} takes no placeholder {name!r}")


def _render_text(
    text: str,
    allowed: frozenset[str],
    key: str,
    values: dict[str, object],
) -> str:
    _validate_render_values(allowed, key, values)
    try:
        return _JINJA.from_string(text).render(**AUTO_PLACEHOLDERS, **values)
    except TemplateSyntaxError as exc:
        raise ValueError(f"prompt template {key!r} has invalid Jinja: {exc}") from exc


def _render_named(
    template_name: str,
    allowed: frozenset[str],
    key: str,
    values: dict[str, object],
) -> str:
    _validate_render_values(allowed, key, values)
    try:
        return _JINJA.get_template(template_name).render(**AUTO_PLACEHOLDERS, **values)
    except TemplateSyntaxError as exc:
        raise ValueError(f"prompt template {template_name!r} has invalid Jinja: {exc}") from exc


def render_template_file(template_name: str, **values: object) -> str:
    """Render a whole-text Jinja file, such as a resource or directive layout."""
    parts = template_name.split("/")
    if (
        not template_name.endswith(".jinja")
        or not parts
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError(f"unsafe prompt template path: {template_name!r}")
    path = _ASSET_ROOT.joinpath(*parts)
    if not path.is_file():
        raise ValueError(f"prompt template not found: {template_name}")
    text = _without_file_newline(path.read_text(encoding="utf-8"))
    allowed = frozenset(
        _template_variables(text, template_name) - set(AUTO_PLACEHOLDERS))
    return _render_named(template_name, allowed, template_name, values)


class PromptTemplate:
    """A standard Jinja module exposing the four prompt editing macros."""

    signatures = {"system": ("ctx",), "before": ("message", "slots", "ctx"),
                  "after": ("message", "slots", "ctx"), "instruction": ("ctx",)}

    def __init__(self, name: str):
        self.name = name
        self.template = _JINJA.get_template(name)
        self._check_dependencies(name, set())
        # Validate resolved exports, including ordinary Jinja macro aliases.
        # Context-dependent top-level setup is checked with real inputs at render time.
        try:
            module = self.template.make_module(AUTO_PLACEHOLDERS)
        except UndefinedError:
            pass
        else:
            self._validate_module(module)

    def _validate_module(self, module) -> None:
        for macro, arguments in self.signatures.items():
            exported = getattr(module, macro, None)
            if not isinstance(exported, Macro) or exported.arguments != arguments:
                raise ValueError(f"{self.name}: expected macro {macro}{arguments}")

    @staticmethod
    def _check_dependencies(name, visited):
        if name in visited:
            return
        visited.add(name)
        source = _JINJA.loader.get_source(_JINJA, name)[0]
        for dependency in meta.find_referenced_templates(_JINJA.parse(source)):
            if dependency is not None:
                PromptTemplate._check_dependencies(dependency, visited)

    def render(self, macro, ctx, *args):
        # A fresh module per invocation avoids sharing rendered state across requests.
        try:
            module = self.template.make_module({**AUTO_PLACEHOLDERS, **ctx, "ctx": ctx})
            self._validate_module(module)
            return getattr(module, macro)(*args, ctx)
        except Exception as exc:
            exc.add_note(f"prompt entry {self.name}, macro {macro}")
            raise

    def system(self, ctx):
        return self.render("system", ctx)

    def before(self, message, slots, ctx):
        return self.render("before", ctx, message, slots)

    def after(self, message, slots, ctx):
        return self.render("after", ctx, message, slots)

    def instruction(self, ctx):
        return self.render("instruction", ctx)


@lru_cache(maxsize=None)
def load_prompt_template(name: str, locale: str = "en") -> PromptTemplate:
    if locale not in PROMPT_LOCALES:
        raise ValueError(f"unsupported prompt locale: {locale}")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise ValueError(f"invalid prompt entry name: {name!r}")
    return PromptTemplate(f"entries/{name}.{locale}.jinja")


def _validate(texts: dict[str, str]) -> None:
    """Validate cross-template contracts at startup."""
    if set(texts) != set(CONNECTORS):
        raise ValueError("loaded prompt template keys differ from the validated catalog")
    section_sets = {
        key: tuple(re.findall(r"^## (\w+) ", texts[key], re.M))
        for key in ("compact.prompt.task", "compact.prompt.agent")
    }
    if len(set(section_sets.values())) != 1:
        raise ValueError(f"digest connectors declare different sections: {section_sets}")


@lru_cache(maxsize=1)
def prompt_pack() -> PromptPack:
    texts = load_prompt_assets("en")
    _validate(texts)
    return PromptPack(texts=texts)


def connector(key: str, **values: object) -> str:
    return prompt_pack()(key, **values)


HEADINGS = {
    name: connector(f"common.heading.{name}")
    for name in (
        "current_task", "current_message", "opening_message", "project_background",
        "capabilities", "skills", "tools", "subagents", "skill_instructions", "role",
        "progress_so_far", "subtasks", "final_output",
    )
}
def runtime_asset_paths(locale: str = "en") -> tuple[Path, ...]:
    """Runtime files are event groups of named Jinja macros, not whole prompts."""
    if locale not in PROMPT_LOCALES:
        raise ValueError(f"unsupported prompt locale: {locale}")
    return tuple(sorted((_ASSET_ROOT / "runtime").glob(f"*.{locale}.jinja")))


@lru_cache(maxsize=None)
def _runtime_definitions(locale: str) -> dict[str, tuple[str, tuple[str, ...], int]]:
    definitions = {}
    for path in runtime_asset_paths(locale):
        name = path.relative_to(_ASSET_ROOT).as_posix()
        parsed = _JINJA.parse(path.read_text(encoding="utf-8"))
        for node in parsed.body:
            if not isinstance(node, nodes.Macro) or node.name.startswith("_"):
                continue
            if node.name in definitions:
                raise ValueError(f"duplicate runtime macro {node.name!r} in {name}")
            definitions[node.name] = (name, tuple(arg.name for arg in node.args), len(node.defaults))
    if not definitions:
        raise ValueError(f"no runtime macros for locale {locale!r}")
    return definitions


def _validate_runtime_locales() -> frozenset[str]:
    base = _runtime_definitions("en")
    for locale in PROMPT_LOCALES:
        definitions = _runtime_definitions(locale)
        if {key: value[1:] for key, value in definitions.items()} != {
            key: value[1:] for key, value in base.items()
        }:
            raise ValueError(f"runtime macro signatures differ for locale {locale!r}")
    return frozenset(base)


RUNTIME_KEYS = _validate_runtime_locales()


def runtime_prompt(name: str, *, locale: str = "en", **values: object) -> str:
    """Render a runtime fact with Jinja; input values are never re-interpolated."""
    if name not in RUNTIME_KEYS:
        raise KeyError(f"unknown runtime prompt {name!r}")
    template_name, arguments, _ = _runtime_definitions(locale)[name]
    _validate_render_values(frozenset(arguments), name, values)
    module = _JINJA.get_template(template_name).make_module(AUTO_PLACEHOLDERS)
    return getattr(module, name)(**values)


BLOCK_FALLBACK = {"compact": connector("compact.role_fallback")}
