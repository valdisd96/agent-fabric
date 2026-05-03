"""Project registry — `~/.fabric/projects.yaml`.

A flat list of `{name, path, repo}` triples. The CLI's `register` command
populates it; future scheduler / dispatcher / sync code reads it to know
which repos to act on.

Path resolution:
  - `$FABRIC_HOME` overrides the default (`~/.fabric`); the systemd unit
    on the Pi sets it to `/var/lib/fabric`.
  - All `path` entries are stored as absolute paths (resolved at register
    time) so the registry is portable to a working directory other than
    where it was written.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from fabric.config import ConfigError, FabricConfig, load_project_config


class RegistryError(Exception):
    """Raised when registry I/O fails or input is unusable."""


@dataclass(frozen=True)
class ProjectEntry:
    name: str
    path: str
    repo: str


def fabric_home() -> Path:
    """Directory holding the registry, state DB, and runtime files."""
    raw = os.environ.get("FABRIC_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".fabric"


def registry_path() -> Path:
    return fabric_home() / "projects.yaml"


def load_registry() -> list[ProjectEntry]:
    p = registry_path()
    if not p.exists():
        return []
    try:
        raw = yaml.safe_load(p.read_text())
    except yaml.YAMLError as e:
        raise RegistryError(f"{p}: invalid YAML: {e}") from e
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RegistryError(f"{p}: top level must be a list, got {type(raw).__name__}")
    entries: list[ProjectEntry] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise RegistryError(f"{p}: entry {i} must be a mapping")
        try:
            entries.append(ProjectEntry(name=item["name"], path=item["path"], repo=item["repo"]))
        except KeyError as e:
            raise RegistryError(f"{p}: entry {i} missing required key {e}") from e
    return entries


def save_registry(entries: list[ProjectEntry]) -> None:
    p = registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload: list[dict[str, Any]] = [asdict(e) for e in entries]
    p.write_text(yaml.safe_dump(payload, sort_keys=False))


@dataclass
class RegisterResult:
    entry: ProjectEntry
    replaced: bool  # True if an existing entry with the same name was overwritten


def register(repo_path: str | Path) -> RegisterResult:
    """Validate `<repo_path>/.fabric/config.yaml` and add it to the registry.

    Re-registering an existing `name` updates `path` and `repo` in place.
    Returns the resulting entry plus whether a previous entry was replaced.
    """
    abs_path = Path(repo_path).expanduser().resolve()
    if not abs_path.is_dir():
        raise RegistryError(f"{abs_path}: not a directory")

    try:
        config: FabricConfig = load_project_config(abs_path)
    except ConfigError as e:
        raise RegistryError(str(e)) from e

    new_entry = ProjectEntry(
        name=config.project.name,
        path=str(abs_path),
        repo=config.project.repo,
    )

    entries = load_registry()
    replaced = False
    for i, existing in enumerate(entries):
        if existing.name == new_entry.name:
            entries[i] = new_entry
            replaced = True
            break
    if not replaced:
        entries.append(new_entry)

    save_registry(entries)
    return RegisterResult(entry=new_entry, replaced=replaced)


def find(name: str) -> ProjectEntry | None:
    """Return the entry for `name`, or None if not registered."""
    for e in load_registry():
        if e.name == name:
            return e
    return None


__all__ = [
    "ProjectEntry",
    "RegisterResult",
    "RegistryError",
    "fabric_home",
    "find",
    "load_registry",
    "register",
    "registry_path",
    "save_registry",
]
