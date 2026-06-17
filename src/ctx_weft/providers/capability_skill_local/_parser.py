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

import yaml


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

    fm = yaml.safe_load(content[3:end]) or {}
    if not isinstance(fm, dict):  # 标量/列表型 frontmatter 视为无元数据
        fm = {}
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
