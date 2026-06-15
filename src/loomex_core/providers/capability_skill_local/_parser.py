"""SKILL.md 解析工具。

SKILL.md 格式：
  ---
  name: my_skill
  description: >
    一段折叠字符串
  triggers:
    - keyword1
    - keyword2
  version: 1.0
  ---

  ## Instructions

  正文内容（instructions），frontmatter 之后的全部文本。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SkillFileMetadata:
    """从 SKILL.md frontmatter 解析出的元数据（仅用于 provider 内部）。"""
    name: str
    description: str
    triggers: list[str] = field(default_factory=list)
    version: str = "1.0"


def parse_skill_md(content: str) -> tuple[SkillFileMetadata, str]:
    """解析 SKILL.md，返回 (metadata, instructions_body)。

    frontmatter 缺失时：name 为空串，body 为整个内容。
    """
    if not content.startswith("---"):
        return SkillFileMetadata(name="", description=""), content.strip()

    end = content.find("\n---", 3)
    if end == -1:
        return SkillFileMetadata(name="", description=""), content.strip()

    fm = _parse_yaml(content[3:end].strip())
    body = content[end + 4:].strip()

    return SkillFileMetadata(
        name=str(fm.get("name", "")).strip(),
        description=str(fm.get("description", "")).strip(),
        triggers=fm.get("triggers") or [],
        version=str(fm.get("version", "1.0")),
    ), body


def load_skill_md(skill_dir: Path) -> tuple[SkillFileMetadata, str]:
    """读取并解析 skill_dir/SKILL.md，返回 (metadata, body)。"""
    content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    return parse_skill_md(content)


# ── 轻量 YAML 解析（仅支持 SKILL.md 所需子集）────────────────────────────────

def _parse_yaml(text: str) -> dict[str, Any]:
    """支持：简单键值、折叠多行字符串（> |）、列表（- item）、带引号值。"""
    result: dict[str, Any] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line or line.startswith("#") or line.startswith(" "):
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue

        key, _, raw = line.partition(":")
        key = key.strip()
        raw = raw.strip()

        if raw == ">":
            # 折叠块：合并为单行
            parts: list[str] = []
            i += 1
            while i < len(lines) and lines[i].startswith("  "):
                parts.append(lines[i].strip())
                i += 1
            result[key] = " ".join(parts)
            continue

        if raw == "|":
            # 字面量块：保留换行
            parts = []
            i += 1
            while i < len(lines) and lines[i].startswith("  "):
                parts.append(lines[i][2:])  # 去掉固定 2 格缩进，保留行内容
                i += 1
            result[key] = "\n".join(parts)
            continue

        if not raw:
            items: list[str] = []
            i += 1
            while i < len(lines) and lines[i].strip().startswith("- "):
                items.append(lines[i].strip()[2:].strip())
                i += 1
            result[key] = items
            continue

        result[key] = raw.strip('"').strip("'")
        i += 1
    return result
