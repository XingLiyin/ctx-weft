"""Jinja environment for complete purpose prompts and runtime messages."""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, nodes

from ctx_weft.core.capabilities.control_tools import (
    ASK_USER_NAME,
    SWITCH_TASK_NAME,
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

__all__ = ["RUNTIME_KEYS", "render_purpose", "runtime_asset_paths", "runtime_prompt"]


AUTO_PLACEHOLDERS: dict[str, str] = {
    "finish_task": FINISH_TASK_NAME,
    "switch_task": SWITCH_TASK_NAME,
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

PROMPT_LOCALES = ("en", "zh")
_ASSET_ROOT = Path(__file__).with_name("prompt_assets")
_JINJA = Environment(
    loader=FileSystemLoader(_ASSET_ROOT),
    undefined=StrictUndefined,
    extensions=["jinja2.ext.do"],
    autoescape=False,
    trim_blocks=False,
    lstrip_blocks=False,
    keep_trailing_newline=False,
)


def render_purpose(name: str, *, locale: str = "en", **values: object) -> None:
    """Execute one purpose template against a PromptBuilder."""
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise ValueError(f"invalid prompt purpose: {name!r}")
    if locale not in PROMPT_LOCALES:
        raise ValueError(f"unsupported prompt locale: {locale!r}")
    template_name = f"steps/{name}.{locale}.jinja"
    context = values.get("ctx")
    expanded = (
        {**AUTO_PLACEHOLDERS, **context, **values}
        if isinstance(context, dict)
        else {**AUTO_PLACEHOLDERS, **values}
    )
    try:
        rendered = _JINJA.get_template(template_name).render(**expanded)
    except Exception as exc:
        exc.add_note(f"prompt purpose {template_name}")
        raise
    if rendered.strip():
        raise ValueError(f"prompt purpose {template_name!r} emitted stray text")


def runtime_asset_paths(locale: str = "en") -> tuple[Path, ...]:
    """Return runtime macro files for one locale."""
    if locale not in PROMPT_LOCALES:
        raise ValueError(f"unsupported prompt locale: {locale}")
    return tuple(sorted((_ASSET_ROOT / "runtime").glob(f"*.{locale}.jinja")))


@cache
def _runtime_definitions(locale: str) -> dict[str, tuple[str, tuple[str, ...], int]]:
    definitions: dict[str, tuple[str, tuple[str, ...], int]] = {}
    for path in runtime_asset_paths(locale):
        template_name = path.relative_to(_ASSET_ROOT).as_posix()
        parsed = _JINJA.parse(path.read_text(encoding="utf-8"))
        for node in parsed.body:
            if not isinstance(node, nodes.Macro) or node.name.startswith("_"):
                continue
            if node.name in definitions:
                raise ValueError(
                    f"duplicate runtime macro {node.name!r} in {template_name}")
            definitions[node.name] = (
                template_name,
                tuple(argument.name for argument in node.args),
                len(node.defaults),
            )
    if not definitions:
        raise ValueError(f"no runtime macros for locale {locale!r}")
    return definitions


def _validate_runtime_locales() -> frozenset[str]:
    base = _runtime_definitions("en")
    signatures = {key: value[1:] for key, value in base.items()}
    for locale in PROMPT_LOCALES:
        current = _runtime_definitions(locale)
        if {key: value[1:] for key, value in current.items()} != signatures:
            raise ValueError(f"runtime macro signatures differ for locale {locale!r}")
    return frozenset(base)


RUNTIME_KEYS = _validate_runtime_locales()


def runtime_prompt(name: str, *, locale: str = "en", **values: object) -> str:
    """Render a named runtime message; supplied values are never re-interpolated."""
    if locale not in PROMPT_LOCALES:
        raise ValueError(f"unsupported prompt locale: {locale!r}")
    if name not in RUNTIME_KEYS:
        raise KeyError(f"unknown runtime prompt {name!r}")
    template_name, arguments, _ = _runtime_definitions(locale)[name]
    unknown = set(values) - set(arguments)
    if unknown:
        value = sorted(unknown)[0]
        raise KeyError(f"prompt template {name!r} takes no placeholder {value!r}")
    module = _JINJA.get_template(template_name).make_module(AUTO_PLACEHOLDERS)
    return getattr(module, name)(**values)
