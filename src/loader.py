"""
Lightweight Declarative Markdown Agent & Skill Loader (`src/loader.py`).
Parses `agents/*.md` and `skills/*.md` files (extracting YAML frontmatter metadata
and structured Markdown sections: Goal, Backstory, Instructions) and dynamically
assembles system prompts for the Gemini API.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AGENTS_DIR = REPO_ROOT / "agents"
DEFAULT_SKILLS_DIR = REPO_ROOT / "skills"


def parse_yaml_frontmatter(raw_text: str) -> tuple[dict[str, Any], str]:
    """
    Lightweight, zero-dependency YAML frontmatter and Markdown body parser.
    Supports scalar key-values (strings, ints, floats, booleans) and bullet lists (`- item`).
    """
    text = raw_text.lstrip("\ufeff")
    if not text.startswith("---"):
        return {}, text.strip()

    match = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)(.*)", text, re.DOTALL)
    if not match:
        return {}, text.strip()

    yaml_block, body = match.group(1), match.group(2).strip()
    metadata: dict[str, Any] = {}
    current_list_key: Optional[str] = None

    for raw_line in yaml_block.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- ") and current_list_key:
            item_val = line[2:].strip().strip("\"'")
            metadata[current_list_key].append(item_val)
            continue

        if ":" in line:
            key, val = line.split(":", 1)
            key = key.strip()
            val = val.strip()
            if not val:
                metadata[key] = []
                current_list_key = key
            else:
                current_list_key = None
                clean_val = val.strip("\"'")
                if clean_val.lower() in ("true", "false"):
                    metadata[key] = clean_val.lower() == "true"
                else:
                    try:
                        if "." in clean_val:
                            metadata[key] = float(clean_val)
                        else:
                            metadata[key] = int(clean_val)
                    except ValueError:
                        metadata[key] = clean_val

    return metadata, body


def extract_markdown_sections(body: str) -> dict[str, str]:
    """
    Extracts top-level `# Heading` or `## Heading` sections (e.g. Goal, Backstory, Instructions)
    into a normalized dictionary `{ "goal": "...", "backstory": "...", "instructions": "..." }`.
    """
    sections: dict[str, list[str]] = {}
    current_heading: Optional[str] = None

    for line in body.splitlines():
        heading_match = re.match(r"^#{1,2}\s+(.+?)\s*$", line)
        if heading_match:
            current_heading = heading_match.group(1).strip().lower()
            sections.setdefault(current_heading, [])
        elif current_heading is not None:
            sections[current_heading].append(line)

    return {k: "\n".join(v).strip() for k, v in sections.items()}


@dataclass
class SkillDefinition:
    """Represents a modular capability parsed from `skills/<skill_name>.md`."""
    name: str
    description: str
    version: str
    metadata: dict[str, Any]
    body: str
    source_path: str

    def render_for_prompt(self) -> str:
        return f"### [SKILL: {self.name}] ({self.description})\n{self.body}"


@dataclass
class AgentDefinition:
    """Represents a declarative agent persona parsed from `agents/<agent_name>.md`."""
    name: str
    role: str
    model: str
    fallback_model: str
    temperature: float
    skill_names: list[str]
    goal: str
    backstory: str
    instructions: str
    raw_body: str
    metadata: dict[str, Any]
    source_path: str
    loaded_skills: list[SkillDefinition] = field(default_factory=list)

    def compile_system_prompt(self, extra_instructions: str = "") -> str:
        """
        Dynamically compiles the agent's YAML metadata, Goal, Backstory, Instructions,
        and attached Markdown Skills into a unified system prompt for the Gemini API.
        """
        blocks = [
            f"# AGENT PERSONA: {self.name}",
            f"**Role:** {self.role}",
        ]
        if self.goal:
            blocks.append(f"## Goal\n{self.goal}")
        if self.backstory:
            blocks.append(f"## Backstory\n{self.backstory}")
        if self.instructions:
            blocks.append(f"## Instructions\n{self.instructions}")
        elif self.raw_body:
            blocks.append(self.raw_body)

        if self.loaded_skills:
            skill_blocks = "\n\n".join(skill.render_for_prompt() for skill in self.loaded_skills)
            blocks.append(f"## Active Declarative Skills\n{skill_blocks}")

        if extra_instructions:
            blocks.append(f"## Task-Specific Execution Directives\n{extra_instructions.strip()}")

        return "\n\n".join(blocks).strip()


class DeclarativeLoader:
    """Loads and caches declarative Markdown agents (`agents/*.md`) and skills (`skills/*.md`)."""

    def __init__(
        self,
        agents_dir: str | Path = DEFAULT_AGENTS_DIR,
        skills_dir: str | Path = DEFAULT_SKILLS_DIR,
    ):
        self.agents_dir = Path(agents_dir)
        self.skills_dir = Path(skills_dir)

    def _resolve_file(self, base_dir: Path, name_or_filename: str) -> Path:
        candidate = Path(name_or_filename)
        if candidate.is_file():
            return candidate
        stem = name_or_filename[:-3] if name_or_filename.endswith(".md") else name_or_filename
        hyphen_stem = stem.replace("_", "-")
        for rel in (
            Path(stem) / "SKILL.md",
            Path(hyphen_stem) / "SKILL.md",
            Path(f"{stem}.md"),
            Path(f"{hyphen_stem}.md"),
        ):
            path = base_dir / rel
            if path.is_file():
                return path
        raise FileNotFoundError(f"Declarative markdown file not found for '{name_or_filename}' in {base_dir}")

    def load_skill(self, skill_name: str) -> SkillDefinition:
        path = self._resolve_file(self.skills_dir, skill_name)
        raw_text = path.read_text(encoding="utf-8")
        meta, body = parse_yaml_frontmatter(raw_text)
        return SkillDefinition(
            name=str(meta.get("name", path.stem)),
            description=str(meta.get("description", "")),
            version=str(meta.get("version", "1.0.0")),
            metadata=meta,
            body=body,
            source_path=str(path),
        )

    def load_agent(self, agent_name: str, attach_skills: bool = True) -> AgentDefinition:
        path = self._resolve_file(self.agents_dir, agent_name)
        raw_text = path.read_text(encoding="utf-8")
        meta, body = parse_yaml_frontmatter(raw_text)
        sections = extract_markdown_sections(body)

        raw_model = str(meta.get("model", "gemini-4-argon"))
        # Automatically upgrade deprecated gemini-2.5-pro to gemini-4-argon
        model = "gemini-4-argon" if raw_model == "gemini-2.5-pro" else raw_model

        skills_field = meta.get("skills", [])
        if isinstance(skills_field, str):
            skill_names = [s.strip() for s in skills_field.split(",") if s.strip()]
        elif isinstance(skills_field, list):
            skill_names = [str(s).strip() for s in skills_field if str(s).strip()]
        else:
            skill_names = []

        loaded_skills: list[SkillDefinition] = []
        if attach_skills:
            for s_name in skill_names:
                try:
                    loaded_skills.append(self.load_skill(s_name))
                except FileNotFoundError:
                    print(f"[DeclarativeLoader Warning] Skill '{s_name}' referenced by '{path.name}' not found.")

        return AgentDefinition(
            name=str(meta.get("name", path.stem)),
            role=str(meta.get("role", "AI Agent")),
            model=model,
            fallback_model=str(meta.get("fallback_model", "qwen2.5-coder:14b")),
            temperature=float(meta.get("temperature", 0.1)),
            skill_names=skill_names,
            goal=sections.get("goal", ""),
            backstory=sections.get("backstory", ""),
            instructions=sections.get("instructions", ""),
            raw_body=body,
            metadata=meta,
            source_path=str(path),
            loaded_skills=loaded_skills,
        )


_default_loader = DeclarativeLoader()


def load_skill(skill_name: str) -> SkillDefinition:
    return _default_loader.load_skill(skill_name)


def load_agent(agent_name: str, attach_skills: bool = True) -> AgentDefinition:
    return _default_loader.load_agent(agent_name, attach_skills=attach_skills)
