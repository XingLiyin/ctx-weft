"""LocalAgentTemplateProvider：单根目录扫描 + 默认 facet 合并 + subagents refs（spec 方案 B）。"""

from __future__ import annotations

from pathlib import Path

from ctx_weft.protocols.capability import AgentCapability
from ctx_weft.protocols.context import ProviderContext
from ctx_weft.providers.agent_template_local import (
    PROVIDER_NAME, LocalAgentTemplateProvider, TemplateLoader,
)

_CTX = ProviderContext(session_id="s1", tenant_id="default")


def _make_dir(root: Path, name: str, *, extra_fm: str = "", compact: str | None = None,
              metadata: str | None = None, role: str | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SOUL.md").write_text(
        f"---\nname: {name}\nversion: 1.0.0\ndescription: {name} agent\n{extra_fm}---\n{name} soul",
        encoding="utf-8",
    )
    if compact is not None:
        (d / "COMPACT.md").write_text(compact, encoding="utf-8")
    if metadata is not None:
        (d / "METADATA.md").write_text(metadata, encoding="utf-8")
    if role is not None:
        (d / "ROLE.md").write_text(role, encoding="utf-8")
    return d


async def test_list_scans_root_and_shapes_capabilities(tmp_path: Path) -> None:
    _make_dir(tmp_path, "default")
    _make_dir(tmp_path, "planner")
    (tmp_path / "not_a_template").mkdir()  # 无 SOUL.md → 忽略
    prov = LocalAgentTemplateProvider(tmp_path)
    caps = await prov.list(_CTX)
    assert {c.id for c in caps} == {"agent:default", "agent:planner"}
    cap = next(c for c in caps if c.id == "agent:planner")
    assert isinstance(cap, AgentCapability)
    assert cap.template_name == "planner"
    assert cap.version == "1.0.0"
    assert cap.description == "planner agent"
    assert PROVIDER_NAME == "agent"


async def test_get_template_miss_returns_none(tmp_path: Path) -> None:
    _make_dir(tmp_path, "default")
    prov = LocalAgentTemplateProvider(tmp_path)
    assert await prov.get_template("nope", None, _CTX) is None


async def test_no_facet_is_ever_borrowed_from_another_template(tmp_path: Path) -> None:
    """缺 facet **不从 default 借**（2026-09-28 移出：那是宿主的部署约定）。

    缺 facet 时的兜底全在 core 自己手里——compact / recognize_intent 由 IdentitySource 回退
    act，observe 家族不回退、走装配层的通用 observer 文案。所以这里只钉一件事：本 provider
    交出去的 template 就是磁盘上那一份，没有任何东西被别的模板填过。
    """
    _make_dir(tmp_path, "default", role="DEF-ROLE", compact="DEF-COMPACT", metadata="DEF-MD")
    _make_dir(tmp_path, "foo")
    prov = LocalAgentTemplateProvider(tmp_path)
    t = await prov.get_template("foo", None, _CTX)
    assert set(t.identity) == {"act"}
    assert t.identity["act"].text == "foo soul"


async def test_a_template_keeps_its_own_facets(tmp_path: Path) -> None:
    _make_dir(tmp_path, "default", role="DEF-ROLE")
    _make_dir(tmp_path, "foo", role="FOO-ROLE", compact="FOO-COMPACT")
    prov = LocalAgentTemplateProvider(tmp_path)
    t = await prov.get_template("foo", None, _CTX)
    assert t.identity["observe"].text == "FOO-ROLE"
    assert t.identity["compact"].text == "FOO-COMPACT"


async def test_retrieve_defaults_empty(tmp_path: Path) -> None:
    _make_dir(tmp_path, "default")
    prov = LocalAgentTemplateProvider(tmp_path)
    assert await prov.retrieve(_CTX) == []


# ── loader：subagents frontmatter → 原样 capability_id（迁自 host test_loader_subagents）──

def _refs(tmp_path: Path, subagents_block: str) -> set[tuple[str, str]]:
    d = _make_dir(tmp_path, "default", extra_fm=subagents_block)
    template = TemplateLoader().load(d)
    return {(r.capability_id, r.mode) for r in template.capability_refs}


def test_subagents_used_as_exact_capability_id(tmp_path: Path) -> None:
    refs = _refs(tmp_path, "subagents:\n  - agent:planner\n")
    assert ("agent:planner", "required") in refs
    assert ("agent:agent:planner", "required") not in refs  # 无前缀补全


def test_bare_subagent_value_is_not_auto_prefixed(tmp_path: Path) -> None:
    refs = _refs(tmp_path, "subagents:\n  - planner\n")
    assert ("planner", "required") in refs
    assert ("agent:planner", "required") not in refs
