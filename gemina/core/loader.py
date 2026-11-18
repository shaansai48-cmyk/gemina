"""
Re-export of DeclarativeLoader inside gemina.core.loader for framework-wide access.
"""
from src.loader import (
    AgentDefinition,
    DeclarativeLoader,
    SkillDefinition,
    extract_markdown_sections,
    load_agent,
    load_skill,
    parse_yaml_frontmatter,
)

__all__ = [
    "AgentDefinition",
    "SkillDefinition",
    "DeclarativeLoader",
    "parse_yaml_frontmatter",
    "extract_markdown_sections",
    "load_agent",
    "load_skill",
]
