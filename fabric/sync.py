"""`fabric sync` — re-render all fabric-managed skills into a project.

Two modes:
  - default: write the rendered output to `<project>/.claude/skills/<name>/SKILL.md`.
  - `--check`: render to memory and compare against on-disk; report drift.

Idempotency: a clean tree should sync to a no-op (sync; sync; → no diff).
Tested by `tests/test_sync.py::test_sync_is_idempotent`.

Project lookup: the `project` argument is treated as a registered name first
(via `fabric.registry.find`), then as a filesystem path. So both
`fabric sync teach-me-eng-bot` and `fabric sync .` work.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path

from fabric.config import ConfigError, load_project_config
from fabric.registry import find
from fabric.render import (
    SKILL_NAMES,
    SKILL_OUTPUT_FILENAME,
    RenderError,
    render_skill,
)


class SyncError(Exception):
    """Raised when sync cannot proceed (project not found, config invalid, etc.)."""


@dataclass
class SkillDrift:
    name: str
    expected: str  # what the template renders to now
    actual: str  # what's currently on disk

    def unified_diff(self, project_path: Path) -> str:
        rel = (
            project_path
            / ".claude"
            / "skills"
            / self.name
            / SKILL_OUTPUT_FILENAME
        )
        return "".join(
            difflib.unified_diff(
                self.actual.splitlines(keepends=True),
                self.expected.splitlines(keepends=True),
                fromfile=f"{rel} (on disk)",
                tofile=f"{rel} (rendered)",
            )
        )


@dataclass
class SyncResult:
    project_path: Path
    written: list[str] = field(default_factory=list)
    drift: list[SkillDrift] = field(default_factory=list)


def resolve_project(arg: str) -> Path:
    """Treat `arg` as a registered name first, then as a path."""
    entry = find(arg)
    if entry is not None:
        return Path(entry.path)
    candidate = Path(arg).expanduser().resolve()
    if candidate.is_dir():
        return candidate
    raise SyncError(
        f"project '{arg}' not found in registry and is not a directory on disk"
    )


def sync(
    project: str | Path,
    *,
    check: bool = False,
    fabric_root: Path | None = None,
) -> SyncResult:
    """Render all fabric-managed skills for `project`.

    Writes to disk unless `check=True`, in which case drift is collected and
    returned without modifying the project.
    """
    project_path = (
        resolve_project(project) if isinstance(project, str) else Path(project).resolve()
    )
    if not project_path.is_dir():
        raise SyncError(f"{project_path}: not a directory")

    try:
        config = load_project_config(project_path)
    except ConfigError as e:
        raise SyncError(str(e)) from e

    result = SyncResult(project_path=project_path)
    skills_dir = project_path / ".claude" / "skills"

    for name in SKILL_NAMES:
        try:
            rendered = render_skill(
                name, project_path, config, fabric_root=fabric_root
            )
        except RenderError as e:
            raise SyncError(str(e)) from e

        out_file = skills_dir / name / SKILL_OUTPUT_FILENAME
        actual = out_file.read_text() if out_file.exists() else ""

        if check:
            if actual != rendered:
                result.drift.append(
                    SkillDrift(name=name, expected=rendered, actual=actual)
                )
            continue

        if actual == rendered:
            continue
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(rendered)
        result.written.append(name)

    return result


__all__ = [
    "SkillDrift",
    "SyncError",
    "SyncResult",
    "resolve_project",
    "sync",
]
