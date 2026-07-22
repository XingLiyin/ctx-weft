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
              metadata: str | None = None) -> Path:
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


async def test_default_facet_merge(tmp_path: Path) -> None:
    _make_dir(tmp_path, "default", compact="DEF-COMPACT", metadata="DEF-MD")
    _make_dir(tmp_path, "foo")
    prov = LocalAgentTemplateProvider(tmp_path)
    t = await prov.get_template("foo", None, _CTX)
    assert t.identity["compact"].text == "DEF-COMPACT"
    assert t.identity["recognize_intent"].text == "DEF-MD"
    assert t.identity["act"].text == "foo soul"  # 自己的 facet 不被覆盖


async def test_default_itself_not_self_merged(tmp_path: Path) -> None:
    _make_dir(tmp_path, "default", compact="DEF-COMPACT")
    prov = LocalAgentTemplateProvider(tmp_path)
    t = await prov.get_template("default", None, _CTX)
    assert t.identity["compact"].text == "DEF-COMPACT"


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
