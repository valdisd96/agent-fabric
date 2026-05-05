"""Jinja2 skill renderer.

Each fabric-managed skill ships as `skill_templates/<name>/SKILL.md.j2` in
the fabric repo. A managed project may override any skill by placing a
sibling file at `<project>/.fabric/skills/<name>/SKILL.md.j2` — overlays
beat defaults at the whole-skill level (no section-level inheritance, by
design — see DESIGN.md "Override grain").

The Jinja environment uses StrictUndefined so a typo in a template (or a
missing config field) raises rather than silently rendering an empty
string. `keep_trailing_newline=True` preserves the file's final newline,
which matters for the byte-for-byte parity check in Phase 0C.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jinja2 import (
    ChoiceLoader,
    Environment,
    FileSystemLoader,
    StrictUndefined,
    TemplateNotFound,
)

from fabric.config import FabricConfig

#: Skills the fabric ships templates for. `dev-flow` is intentionally
#: project-internal and not in this list.
SKILL_NAMES: tuple[str, ...] = (
    "plan-exec",
    "test-writer",
    "review-pr",
    "clarify-issue",
    "epic-decompose",
    "qualify-issue",
)

SKILL_TEMPLATE_FILENAME = "SKILL.md.j2"
SKILL_OUTPUT_FILENAME = "SKILL.md"


class RenderError(Exception):
    """Raised when a skill template is missing or fails to render."""


def default_fabric_root() -> Path:
    """Directory containing `skill_templates/`.

    Templates ship as package data inside `fabric/`, so the default is the
    `fabric` package directory itself. This works identically for editable
    installs (`pip install -e .`) and built wheels.
    """
    return Path(__file__).resolve().parent


@dataclass(frozen=True)
class TemplateSources:
    """Search roots for `<name>/SKILL.md.j2`, in precedence order."""

    overlay: Path  # <project>/.fabric/skills
    default: Path  # <fabric_root>/skill_templates

    def loader(self) -> ChoiceLoader:
        return ChoiceLoader(
            [
                FileSystemLoader(str(self.overlay)),
                FileSystemLoader(str(self.default)),
            ]
        )


def template_sources(project_path: Path, fabric_root: Path | None = None) -> TemplateSources:
    root = fabric_root or default_fabric_root()
    return TemplateSources(
        overlay=project_path / ".fabric" / "skills",
        default=root / "skill_templates",
    )


def _make_env(sources: TemplateSources) -> Environment:
    return Environment(
        loader=sources.loader(),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,
        trim_blocks=False,
        lstrip_blocks=False,
    )


def render_skill(
    name: str,
    project_path: Path,
    config: FabricConfig,
    *,
    fabric_root: Path | None = None,
) -> str:
    """Render skill `name` for `project_path` against `config`.

    Looks first in the project's `.fabric/skills/<name>/SKILL.md.j2`, then
    in the fabric's bundled `skill_templates/<name>/SKILL.md.j2`. Missing
    in both is `RenderError`.
    """
    sources = template_sources(project_path, fabric_root)
    env = _make_env(sources)
    template_name = f"{name}/{SKILL_TEMPLATE_FILENAME}"
    try:
        template = env.get_template(template_name)
    except TemplateNotFound as e:
        raise RenderError(
            f"skill template not found: {name} "
            f"(searched {sources.overlay} then {sources.default})"
        ) from e
    try:
        return template.render(**config.model_dump())
    except Exception as e:
        raise RenderError(f"render failed for {name}: {e}") from e


__all__ = [
    "RenderError",
    "SKILL_NAMES",
    "SKILL_OUTPUT_FILENAME",
    "SKILL_TEMPLATE_FILENAME",
    "TemplateSources",
    "default_fabric_root",
    "render_skill",
    "template_sources",
]
