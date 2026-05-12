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
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO, Any, Mapping

import yaml

from fabric.config import ConfigError, FabricConfig, load_project_config


SYSTEMD_ENV_FILE = Path("/etc/fabric/env")


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


def parse_env_file_fabric_home(path: Path) -> str | None:
    """Return the FABRIC_HOME value from a systemd EnvironmentFile, or None.

    Tolerant of comments, blank lines, and surrounding whitespace; returns
    None if the file is missing or has no FABRIC_HOME line. Pure helper —
    no side effects, no logging."""
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if key.strip() == "FABRIC_HOME":
            return value.strip().strip('"').strip("'") or None
    return None


def warn_if_systemd_env_diverges(
    *,
    env: Mapping[str, str] | None = None,
    env_file: Path = SYSTEMD_ENV_FILE,
    stream: IO[str] | None = None,
) -> bool:
    """Warn when /etc/fabric/env declares FABRIC_HOME but the current env
    doesn't, or sets it to a different value. Returns True iff a warning
    was emitted.

    The bug this prevents: an operator runs `fabric register …` from a
    shell that didn't source /etc/fabric/env. Without FABRIC_HOME, the
    CLI writes the registry to ~/.fabric/projects.yaml while the running
    service reads $FABRIC_HOME/projects.yaml — the project never appears
    in /api/projects and the scheduler polls nothing."""
    env_map = os.environ if env is None else env
    declared = parse_env_file_fabric_home(env_file)
    if declared is None:
        return False
    current = env_map.get("FABRIC_HOME")
    if current == declared:
        return False
    out = sys.stderr if stream is None else stream
    if current is None:
        print(
            f"warning: {env_file} sets FABRIC_HOME={declared} but it is unset "
            f"in this shell. CLI writes will go to {Path.home() / '.fabric'} "
            f"which the systemd service does not read. "
            f"Run: export FABRIC_HOME={declared}",
            file=out,
        )
    else:
        print(
            f"warning: {env_file} sets FABRIC_HOME={declared} but the current "
            f"shell has FABRIC_HOME={current}. CLI writes and the service will "
            f"diverge.",
            file=out,
        )
    return True


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
    "SYSTEMD_ENV_FILE",
    "fabric_home",
    "find",
    "load_registry",
    "parse_env_file_fabric_home",
    "register",
    "registry_path",
    "save_registry",
    "warn_if_systemd_env_diverges",
]
