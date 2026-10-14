"""frontmatter 解析走真 YAML（2026-10-09 换掉手写的子集解析器）。

换掉的理由不是「手写那版支持的语法少」，而是**不支持的语法一律静默解析错**——下面每一条
都曾经是、或差一点就是一个没人看得见的配置失效。手写那版是 loader 从 host 上移时一起带
过来的，仓里没有任何地方写过为什么要手写；而同仓的 `capability_skill_local` 早就直接
`yaml.safe_load` 了。

换之前对两仓现存的 24 个 facet 文件做过比对：PyYAML 一个都解析不了的是 0 个，唯一差异是
`mcp_servers:` 空值（`''` vs `None`），而 loader 不读它。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ctx_weft.providers.agent_template_local import TemplateLoader


def _soul(d: Path, body: str = "soul body", *, fm: str = "") -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "SOUL.md").write_text(f"---\n{fm}---\n{body}", encoding="utf-8")
    return d


def _refs(d: Path) -> list[str]:
    return [r.capability_id for r in TemplateLoader().load(d).capability_refs]


def test_a_comment_does_not_swallow_the_next_list_item(tmp_path: Path) -> None:
    """**踩过的那一个。**

    手写解析器把 `#` 开头的行当「跳过」，但跳过之后它已经不在「正在收集列表项」的状态里，
    于是注释之后的列表项一并消失。现场是 ROLE.md 的 `required:` 里加了一行注释，
    `report_observation` 就此从模板里蒸发——只摘要档没有 terminal tool 可调。而宿主那条
    「声明了两个 observer 工具」的守护查的是**文件原文**，所以照样绿（已一并改成查解析结果）。
    """
    d = _soul(tmp_path / "a", fm="name: a\nversion: 1.0.0\n")
    (d / "ROLE.md").write_text(
        "---\n"
        "tools:\n"
        "  required:\n"
        "    - control:report_observation\n"
        "    # 这一行注释不该吃掉下一项\n"
        "    - control:report_observation\n"
        "---\n"
        "role body",
        encoding="utf-8",
    )
    ids = _refs(d)
    assert "control:report_observation" in ids
    assert "control:report_observation" in ids


def test_an_end_of_line_comment_does_not_leak_into_the_value(tmp_path: Path) -> None:
    """手写版把 `name: a  # 注释` 整条当成值。

    `name` 不是个无关紧要的字段：`id` 默认由它派生，而 `id` 是**可路由**的那一个（恢复期
    按 `provider:name` 前缀精确找回 agent）。带上注释就路由不到了，且没有任何报错。
    """
    d = _soul(tmp_path / "b", fm="name: beta  # 开发用\nversion: 1.0.0\n")
    tpl = TemplateLoader().load(d)
    assert tpl.name == "beta"
    assert tpl.id == "beta"


def test_an_inline_list_is_a_list(tmp_path: Path) -> None:
    """`tools: [a, b]` 是合法 YAML；手写版把它当成一个字符串，于是 `_parse_tools` 里
    `isinstance(raw, list)` 不成立、`isinstance(raw, dict)` 也不成立，整条 tools 静默失效。
    """
    d = _soul(tmp_path / "c", fm=(
        "name: c\nversion: 1.0.0\n"
        "tools: [control:finish_task, control:ask_user]\n"))
    assert _refs(d) == ["control:finish_task", "control:ask_user"]


def test_a_boolean_is_a_boolean(tmp_path: Path) -> None:
    """**这一条是个一直活着的 bug，换解析器顺带修掉。**

    手写版的标量一律是 `str`，于是 `use_long_term: false` 解析成 `"false"`，而
    `_parse_memory_config` 那行 `bool(raw.get("use_long_term", True))` 对非空字符串判真——
    这个配置项从来没有生效过。`loop_config` 没踩到只是因为它那边逐字段 `int()/float()` 强转，
    而 `memory_config` 的布尔没人兜。
    """
    d = _soul(tmp_path / "d", fm=(
        "name: d\nversion: 1.0.0\nmemory_config:\n  use_long_term: false\n"))
    assert TemplateLoader().load(d).memory_config.use_long_term is False


def test_numbers_come_through_typed(tmp_path: Path) -> None:
    """`loop_config` 的 `int()/float()` 强转本来就能吃 str，但别依赖那层——它只覆盖
    loop_config 一处（见上一条）。"""
    d = _soul(tmp_path / "e", fm=(
        "name: e\nversion: 1.0.0\n"
        "loop_config:\n  max_turns_per_act: 7\n  compact_token_ratio: 0.75\n"))
    cfg = TemplateLoader().load(d).loop_config
    assert cfg.max_turns_per_act == 7
    assert cfg.compact_token_ratio == 0.75


def test_a_broken_frontmatter_raises_instead_of_loading_a_blank_template(
    tmp_path: Path,
) -> None:
    """坏 frontmatter 直接抛，**不回落空 dict**。

    回落意味着 name / version / tools 全丢，装出一个「看起来正常、其实什么都没配」的模板
    ——那比报错难查得多。`TemplateLoader.scan` 已经 per-template 捕获并 `logger.exception`，
    所以一个坏模板只会被跳过、不拖垮运行时；这条一并钉住。
    """
    # 用 tab 缩进（YAML 明令禁止 tab 作缩进，而编辑器里看不出来，是最容易误写的一种）
    d = _soul(tmp_path / "f", fm="name: f\ntools:\n\t- control:finish_task\n")
    with pytest.raises(Exception):
        TemplateLoader().load(d)
    assert TemplateLoader().scan(tmp_path) == []


def test_a_non_mapping_frontmatter_is_rejected(tmp_path: Path) -> None:
    """frontmatter 必须是 mapping。一段裸标量/列表能被 YAML 解析成功，但后面全部
    `fm.get(...)` 会炸 AttributeError——在入口处给一句清楚的错更省事。"""
    d = tmp_path / "g"
    d.mkdir()
    (d / "SOUL.md").write_text("---\n- just\n- a list\n---\nbody", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a YAML mapping"):
        TemplateLoader().load(d)
