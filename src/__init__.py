"""
Declarative Agent & Skill Loader and Telegram Bot Entrypoints for Gemina.
"""
from src.loader import AgentDefinition, DeclarativeLoader, SkillDefinition, load_agent, load_skill

__all__ = [
    "AgentDefinition",
    "SkillDefinition",
    "DeclarativeLoader",
    "load_agent",
    "load_skill",
]
