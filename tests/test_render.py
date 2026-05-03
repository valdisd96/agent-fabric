from __future__ import annotations

from pathlib import Path

import pytest

from fabric.config import load_project_config
from fabric.render import (
    SKILL_NAMES,
    RenderError,
    render_skill,
    template_sources,
)


def test_render_skill_uses_default_when_no_overlay(project: Path, fabric_root: Path) -> None:
    config = load_project_config(project)
    out = render_skill("plan-exec", project, config, fabric_root=fabric_root)
    # Frontmatter `name:` is the most stable hook into the rendered content.
    assert "name: plan-exec" in out
    # plan-exec's template substitutes the smoke command (setup_cmd && test_cmd).
    assert config.build.test_cmd in out


def test_overlay_wins_over_default(project: Path, fabric_root: Path) -> None:
    overlay = project / ".fabric" / "skills" / "plan-exec"
    overlay.mkdir(parents=True)
    (overlay / "SKILL.md.j2").write_text("OVERLAY: {{ project.name }}\n")

    config = load_project_config(project)
    out = render_skill("plan-exec", project, config, fabric_root=fabric_root)
    assert out.startswith("OVERLAY:")
    assert config.project.name in out


def test_overlay_only_affects_named_skill(project: Path, fabric_root: Path) -> None:
    overlay = project / ".fabric" / "skills" / "plan-exec"
    overlay.mkdir(parents=True)
    (overlay / "SKILL.md.j2").write_text("OVERLAY\n")

    config = load_project_config(project)
    other = render_skill("test-writer", project, config, fabric_root=fabric_root)
    assert "OVERLAY" not in other
    assert "test-writer" in other


def test_missing_template_raises(project: Path, tmp_path: Path) -> None:
    config = load_project_config(project)
    bare_fabric = tmp_path / "bare-fabric"
    (bare_fabric / "skill_templates").mkdir(parents=True)
    with pytest.raises(RenderError, match="not found"):
        render_skill("plan-exec", project, config, fabric_root=bare_fabric)


def test_undefined_variable_in_overlay_raises(project: Path, fabric_root: Path) -> None:
    overlay = project / ".fabric" / "skills" / "plan-exec"
    overlay.mkdir(parents=True)
    (overlay / "SKILL.md.j2").write_text("{{ does_not_exist }}\n")

    config = load_project_config(project)
    with pytest.raises(RenderError, match="render failed"):
        render_skill("plan-exec", project, config, fabric_root=fabric_root)


def test_all_default_templates_render(project: Path, fabric_root: Path) -> None:
    config = load_project_config(project)
    for name in SKILL_NAMES:
        out = render_skill(name, project, config, fabric_root=fabric_root)
        assert out.endswith("\n"), f"{name} should preserve trailing newline"
        assert len(out) > 0


def test_template_sources_paths(project: Path, fabric_root: Path) -> None:
    sources = template_sources(project, fabric_root=fabric_root)
    assert sources.overlay == project / ".fabric" / "skills"
    assert sources.default == fabric_root / "skill_templates"
