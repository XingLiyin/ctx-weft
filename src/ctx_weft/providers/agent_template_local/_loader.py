"""TemplateLoader — 目录 → AgentTemplate 解析（core 资产，spec 2026-07-22 方案 B 自 host 上移）。

目录结构：
  agents/
    default/
      SOUL.md    frontmatter(name/version/description/tools/loop_config) + body(actor soul)
      ROLE.md    frontmatter(tools) + body(observer role)（可选；缺了 = 这个 agent 没有
                 observer，见 core/loop/steps/observe.py::has_observe_role 的三个下游）

SOUL.md frontmatter 示例：
  ---
  name: default
  version: 1.0.0
  description: Default multi-purpose agent
  tools:
    - fs:shell
    - control:submit_task
    - control:submit_plan
    - skill_executor:list_files
  loop_config:
    max_turns_per_act: 10
    max_spawn_depth: 4
  ---
  You are a helpful AI assistant...

ROLE.md frontmatter 示例：
  ---
  tools:
    - control:submit_task_assessment
  ---
  You are an objective observer...
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from ctx_weft.protocols.template import (
    AgentTemplate,
    CapabilityRef,
    IdentityFacet,
    LoopConfig,
    MemoryConfig,
)

logger = logging.getLogger(__name__)




class TemplateLoader:
    """解析 template 目录（SOUL.md + ROLE.md）→ AgentTemplate。"""

    def load(self, template_dir: Path) -> AgentTemplate:
        """读取目录下的 SOUL.md（必须）和 ROLE.md（可选），组装 AgentTemplate。"""
        soul_path = template_dir / "SOUL.md"
        if not soul_path.exists():
            raise FileNotFoundError(f"SOUL.md not found in {template_dir}")

        soul_fm, soul_body = _parse_md(soul_path.read_text(encoding="utf-8"))

        name = str(soul_fm.get("name", template_dir.name)).strip()
        version = str(soul_fm.get("version", "1.0.0")).strip()
        description = str(soul_fm.get("description", "")).strip()

        # capability_refs: SOUL tools → purposes=["act"]; ROLE tools → purposes=["observe"]
        cap_refs: list[CapabilityRef] = []
        soul_required, soul_forbidden = _parse_tools(soul_fm)
        for tool_id in soul_required:
            cap_refs.append(CapabilityRef(capability_id=tool_id, mode="required"))
        for tool_id in soul_forbidden:
            cap_refs.append(CapabilityRef(capability_id=tool_id, mode="forbidden"))

        # subagents frontmatter → required refs。值即完整的 provider:name（如 "agent:planner"），
        # 与 tools 一致：原样作为 capability_id，由 CapabilityResolver 按 cap.id **精准匹配**，
        # 不做任何前缀补全。
        for sub in (soul_fm.get("subagents") or []):
            sid = str(sub).strip()
            if sid:
                cap_refs.append(CapabilityRef(capability_id=sid, mode="required"))

        identity: dict[str, IdentityFacet] = {}
        if soul_body:
            identity["act"] = IdentityFacet(text=soul_body)

        role_path = template_dir / "ROLE.md"
        if role_path.exists():
            role_fm, role_body = _parse_md(role_path.read_text(encoding="utf-8"))
            role_required, role_forbidden = _parse_tools(role_fm)
            for tool_id in role_required:
                cap_refs.append(CapabilityRef(capability_id=tool_id, mode="required"))
            for tool_id in role_forbidden:
                cap_refs.append(CapabilityRef(capability_id=tool_id, mode="forbidden"))
            if role_body:
                identity["observe"] = IdentityFacet(text=role_body)

        # 额外 purpose facet：body-only persona（不贡献 capability_refs）。
        for fname, purpose in (("COMPACT.md", "compact"), ("METADATA.md", "recognize_intent")):
            fpath = template_dir / fname
            if not fpath.exists():
                continue
            _, facet_body = _parse_md(fpath.read_text(encoding="utf-8"))
            if facet_body:
                identity[purpose] = IdentityFacet(text=facet_body)

        loop_cfg = _parse_loop_config(soul_fm.get("loop_config") or {})
        mem_cfg = _parse_memory_config(soul_fm.get("memory_config") or {})

        template_id = soul_fm.get("id") or name

        return AgentTemplate(
            id=template_id,
            name=name,
            version=version,
            description=description,
            identity=identity,  # type: ignore[arg-type]
            capability_refs=cap_refs,
            loop_config=loop_cfg,
            memory_config=mem_cfg,
        )

    def scan(self, agents_dir: Path) -> list[tuple[Path, AgentTemplate]]:
        """扫描 agents_dir，返回 (template_dir, AgentTemplate) 列表。"""
        if not agents_dir.exists():
            return []
        result = []
        for d in sorted(agents_dir.iterdir()):
            if not d.is_dir() or not (d / "SOUL.md").exists():
                continue
            try:
                result.append((d, self.load(d)))
            except Exception:
                logger.exception("TemplateLoader: failed to load '%s'", d)
        return result


# ── 解析工具 ──────────────────────────────────────────────────────────────────

def _parse_md(content: str) -> tuple[dict[str, Any], str]:
    """分离 YAML frontmatter 和 body 文本。

    frontmatter 走 `yaml.safe_load`（2026-10-09）。此前是一个手写的 YAML 子集解析器，
    从 host 上移 loader 时一起带过来的，**没有任何地方写过为什么手写**。它的问题不是
    「支持的语法少」，而是不支持的语法一律**静默解析错**：

      - `#` 注释行会把紧随其后的列表项吃掉（ROLE.md 的 `required:` 里加一行注释，
        `report_observation` 就此消失，只摘要档没有 terminal tool 可调）；
      - 行尾注释并进值（`name: default  # 默认` → agent 名字带上注释）；
      - 行内列表 `tools: [a, b]` 变成一个字符串，整条 tools 失效；
      - 一切标量都是 str，于是 `use_long_term: false` → `"false"` → `bool()` 判真，
        那个配置项**一直是失效的**。

    换掉之前对两仓现存的 24 个 facet 文件做过比对：PyYAML 一个都解析不了的是 0 个，唯一
    差异是 `mcp_servers:` 空值（`''` vs `None`），而 loader 不读它。

    **解析失败直接抛**（不回落空 dict）：frontmatter 坏了意味着 name/version/tools 全部
    丢失，静默当成「没有配置」比报错危险。`TemplateLoader.scan` 已经 per-template 捕获并
    `logger.exception`，所以一个坏模板只会被跳过，不会拖垮运行时。
    """
    if not content.startswith("---"):
        return {}, content.strip()
    end = content.find("\n---", 3)
    if end == -1:
        return {}, content.strip()
    fm = yaml.safe_load(content[3:end]) or {}
    if not isinstance(fm, dict):
        raise ValueError(
            f"frontmatter must be a YAML mapping, got {type(fm).__name__}")
    body = content[end + 4:].strip()
    return fm, body


def _str_list(fm: dict, key: str) -> list[str]:
    raw = fm.get(key)
    if isinstance(raw, list):
        return [str(s).strip() for s in raw if s]
    return []


def _parse_tools(fm: dict) -> tuple[list[str], list[str]]:
    """解析 tools 字段，兼容两种格式：

    扁平列表（旧格式）：
      tools:
        - control:submit_task

    分组格式（新格式）：
      tools:
        required:
          - control:update_task_metadata
        forbidden:
          - control:submit_task
    """
    raw = fm.get("tools")
    if isinstance(raw, list):
        return [str(s).strip() for s in raw if s], []
    if isinstance(raw, dict):
        required = [str(s).strip() for s in (raw.get("required") or []) if s]
        forbidden = [str(s).strip() for s in (raw.get("forbidden") or []) if s]
        return required, forbidden
    return [], []


def _parse_loop_config(raw: dict) -> LoopConfig:
    return LoopConfig(
        max_turns_per_act=int(raw.get("max_turns_per_act", 50)),
        max_turns_per_observe=int(raw.get("max_turns_per_observe", 5)),
        # 这是**配置 schema 的字段名**（模板作者面向），不是判别值。它与
        # CancelReason.FAILURE_THRESHOLD 今天恰好同名，但两者是各自独立演进的契约：
        # 前者对模板作者、后者对 host。若把 key 绑上枚举，日后改判别值的名字会让
        # 加载器静默去读不存在的 YAML key、回落默认值——配置失效且无测试可抓。
        # 故此处**刻意保留裸字面量**，与相邻的 max_turns_per_act 等一致。
        failure_threshold=int(raw.get("failure_threshold", 3)),
        max_spawn_depth=int(raw.get("max_spawn_depth", 4)),
        compact_token_ratio=float(raw.get("compact_token_ratio", 0.8)),
        compact_target_ratio=float(raw.get("compact_target_ratio", 0.4)),
        compact_message_delta=int(raw.get("compact_message_delta", 20)),
        compact_keep_last=int(raw.get("compact_keep_last", 6)),
        collapse_keep_last=int(raw.get("collapse_keep_last", 3)),
        context_limit_stop_ratio=float(raw.get("context_limit_stop_ratio", 0.9)),
        max_context_recoveries=int(raw.get("max_context_recoveries", 2)),
    )


def _parse_memory_config(raw: dict) -> MemoryConfig:
    return MemoryConfig(
        short_window_size=int(raw.get("short_window_size", 20)),
        summary_threshold=int(raw.get("summary_threshold", 20)),
        use_long_term=bool(raw.get("use_long_term", True)),
        subscribed_blackboard_topics=raw.get("subscribed_blackboard_topics") or [],
    )
