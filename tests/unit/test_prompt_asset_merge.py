from pathlib import Path

import pytest

from ctx_weft.core.assembler import prompts


def _asset_root() -> Path:
    return Path(prompts.__file__).with_name("prompt_assets")


def test_assets_expose_only_complete_steps_and_runtime_macros() -> None:
    root = _asset_root()
    assert {path.name for path in root.iterdir()} == {"README.md", "steps", "runtime"}
    assert sorted(path.name for path in (root / "steps").glob("[a-z]*.en.jinja")) == [
        "act.en.jinja", "compact.en.jinja", "metadata.en.jinja", "observe.en.jinja",
    ]


@pytest.mark.parametrize("locale", ["en", "zh"])
def test_purpose_templates_depend_only_on_the_purpose_surface(locale: str) -> None:
    root = _asset_root()
    for path in (root / "steps").glob(f"*.{locale}.jinja"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "{% include" in line or "{% import" in line:
                assert '"steps/' in line


def test_runtime_macros_are_locale_symmetric() -> None:
    assert prompts.RUNTIME_KEYS
    assert {path.name.replace(".en.", ".") for path in prompts.runtime_asset_paths("en")} == {
        path.name.replace(".zh.", ".") for path in prompts.runtime_asset_paths("zh")
    }


def test_runtime_values_are_not_reinterpreted_as_jinja() -> None:
    literal = "<tag>{{ finish_task }}</tag>"
    rendered = prompts.runtime_prompt("human_note", note=literal, result="OK")
    assert literal in rendered
