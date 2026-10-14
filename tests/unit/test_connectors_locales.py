"""Composer asset 内相邻语言版本的同步守护。

中文版**今天没有接线**：`prompt_pack()` 仍只装载英文（locale 选择机制未做）。
中英文现在以相邻的 `<key>.en.jinja` / `<key>.zh.jinja` 文件存在；在接线之前这几条测试是
「两份别漂」的唯一机械保障。

**能钉住什么、钉不住什么**，说清楚免得有人把这里当成完整保险：

  能钉：键集一致、每键的占位符一致、digest 的五小节标题与解析器一致、没写死工具名、
        不该译的东西没被译掉。
  钉不住：**语义是否同步**。英文那边改了一句话的意思而中文没跟上，这里是绿的。所以改
        英文版时请同时改中文版——没有别的办法。
"""

from __future__ import annotations

import re

import pytest
from jinja2 import Environment, meta

from ctx_weft.core.assembler.prompts import (
    AUTO_PLACEHOLDERS,
    CONNECTORS,
    load_prompt_assets,
    prompt_asset_paths,
)

_JINJA = Environment()


def _variables(text: str) -> set[str]:
    return set(meta.find_undeclared_variables(_JINJA.parse(text)))

#: 英文目录是**运行时实际读的**，其余是译本。
_LOCALES = ("en", "zh")
#: 译本（英文版之外的全部）。日后加一门语言只需往 `_LOCALES` 里加一行。
_TRANSLATIONS = tuple(k for k in _LOCALES if k != "en")


def _texts(locale: str) -> dict[str, str]:
    return load_prompt_assets(locale)


def test_every_locale_file_parses_under_the_same_rules() -> None:
    """译本和英文版都作为真正的 Jinja 文件解析。"""
    for locale in _LOCALES:
        assert all(path.exists() for path in prompt_asset_paths(locale))
        assert _texts(locale), f"locale {locale} 解析出来是空的"


@pytest.mark.parametrize("locale", _TRANSLATIONS)
def test_the_key_set_matches_the_english_one(locale: str) -> None:
    """键集必须一致。

    少一个键 → 日后切到这门语言时那一段**静默消失**（装配层不会报错，它只是少拼一段）；
    多一个键 → 改名后的遗留，没人会再去维护它。
    """
    en, tr = _texts("en"), _texts(locale)
    assert set(tr) == set(en), (
        f"locale {locale} 与英文版键集不一致："
        f"缺 {sorted(set(en) - set(tr))}，多 {sorted(set(tr) - set(en))}")
    # 顺带：译本的键也必须都在注册表里（和英文版同一道门）
    assert set(tr) == set(CONNECTORS)


@pytest.mark.parametrize("locale", _TRANSLATIONS)
def test_each_entry_takes_the_same_placeholders(locale: str) -> None:
    """占位符逐键一致。

    译文里漏掉一个变量，渲染时那个值就不会出现——比如 `common.task_anchor` 漏了
    `{{ label }}`，任务锚就变成一句没有任务的空话。多写一个则会在 StrictUndefined 下报错。
    """
    en, tr = _texts("en"), _texts(locale)
    for key in sorted(en):
        want = _variables(en[key])
        got = _variables(tr[key])
        assert got == want, (
            f"locale {locale} 的 {key!r} 占位符不一致："
            f"缺 {sorted(want - got)}，多 {sorted(got - want)}")


@pytest.mark.parametrize("locale", _TRANSLATIONS)
def test_translations_never_hardcode_an_auto_injected_name(locale: str) -> None:
    """与英文版同一条纪律：工具名和共享结构标签都走占位符注入。"""
    for key, text in _texts(locale).items():
        for name in AUTO_PLACEHOLDERS.values():
            assert name not in text, f"locale {locale} 的 {key!r} 写死了自动注入值 {name!r}"


@pytest.mark.parametrize("locale", _TRANSLATIONS)
def test_the_digest_section_headings_stay_untranslated(locale: str) -> None:
    """**最容易译错的一处。**

    `compact.prompt.*` 里那五个 `## Goal` 式的小节标题是**解析契约**：cue 要模型按这五个
    标题写，而 `compact.py` 的 `_DIGEST_SECTION_RE` 按同样的标题把结果切回来。把标题译成
    中文，模型会照译文写，解析器一节都切不出来——而那个失败是静默的（摘要变成一团没有结构
    的文本）。标题留英文、描述译成中文才是对的。
    """
    from ctx_weft.core.loop.steps.compact import DIGEST_SECTIONS

    tr = _texts(locale)
    for key in ("compact.prompt.task", "compact.prompt.agent"):
        found = tuple(re.findall(r"^## (\w+) ", tr[key], re.M))
        assert found == DIGEST_SECTIONS, (
            f"locale {locale} 的 {key!r} 小节标题是 {found}，"
            f"解析器认的是 {DIGEST_SECTIONS}——标题不要译")


@pytest.mark.parametrize("locale", _TRANSLATIONS)
def test_the_prose_is_actually_translated(locale: str) -> None:
    """反向守卫：别拿英文原文凑数。

    逐键要求「至少有一个该语言的字」。没有这一条，复制一份英文过来就能让上面所有测试全绿
    ——而那正是「有一个中文版」这件事最容易变成的样子。
    """
    assert locale == "zh", f"新增语言请为 {locale!r} 补一条字符判据"
    for key, text in _texts(locale).items():
        if key.startswith("common.heading."):
            # 结构标签是跨层检索/解析契约；语言包中显式重复，表示有意保持稳定，而非漏译。
            continue
        assert any("一" <= ch <= "鿿" for ch in text), f"{key!r} 里没有一个中文字"
