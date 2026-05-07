"""Pydantic schema + loader for `.fabric/config.yaml`.

The schema is the public contract between the fabric and managed projects;
managed projects pin a `fabric_version` and depend on its stability.
DESIGN.md "Per-project layout & config" is the source of truth for shape.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class ConfigError(Exception):
    """Raised when a project's `.fabric/config.yaml` is missing or invalid."""


_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Project(_Strict):
    name: str = Field(min_length=1)
    repo: str
    trusted_authors: list[str] = Field(default_factory=list)

    @field_validator("repo")
    @classmethod
    def _check_repo(cls, v: str) -> str:
        if not _REPO_RE.match(v):
            raise ValueError("must be in 'owner/repo' form")
        return v


class Build(_Strict):
    setup_cmd: str | None = None
    test_cmd: str
    lint_cmd: str | None = None


class Module(_Strict):
    path: str = Field(min_length=1)
    role: str = Field(min_length=1)


class Safety(_Strict):
    blocked_paths: list[str] = Field(default_factory=list)
    destructive_db_patterns: list[str] = Field(default_factory=list)
    notes: str = ""


class Labels(_Strict):
    state_prefix: str = "state:"
    type_prefix: str = "type:"
    area_prefix: str = "area:"
    area_labels: list[str] = Field(default_factory=list)


class Pipeline(_Strict):
    cycle_limit: int = Field(default=5, gt=0)
    retry_count: int = Field(default=3, ge=0)
    retry_backoff_seconds: list[int] = Field(default_factory=lambda: [60, 300, 900])
    # Rolling-window cap: at most `dispatch_cap` dispatches in any
    # `dispatch_window_hours`-long window. Tracks Anthropic's 5-hour rate
    # limit by default (30/5h ≈ Pro plan headroom). Window is rolling, not
    # session-based — slightly more conservative than what Anthropic enforces,
    # which is fine for staying under the wall.
    dispatch_cap: int = Field(default=30, gt=0)
    dispatch_window_hours: int = Field(default=5, gt=0)
    downgrade_low_priority: bool = False

    @field_validator("retry_backoff_seconds")
    @classmethod
    def _check_backoff(cls, v: list[int]) -> list[int]:
        if any(s < 0 for s in v):
            raise ValueError("retry_backoff_seconds entries must be non-negative")
        return v


class FabricConfig(_Strict):
    project: Project
    build: Build
    modules: list[Module] = Field(default_factory=list)
    safety: Safety = Field(default_factory=Safety)
    labels: Labels = Field(default_factory=Labels)
    pipeline: Pipeline = Field(default_factory=Pipeline)
    fabric_version: str


def load_config(path: str | Path) -> FabricConfig:
    """Load a `.fabric/config.yaml` from disk and validate it.

    Raises `ConfigError` with field-path context on any problem.
    """
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"{p}: file not found")
    try:
        raw = yaml.safe_load(p.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"{p}: invalid YAML: {e}") from e
    if raw is None:
        raise ConfigError(f"{p}: file is empty")
    if not isinstance(raw, dict):
        raise ConfigError(f"{p}: top level must be a mapping, got {type(raw).__name__}")
    try:
        return FabricConfig.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"{p}: {_format_validation_error(e)}") from e


def _format_validation_error(e: ValidationError) -> str:
    parts: list[str] = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "<root>"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


def load_project_config(project_path: str | Path) -> FabricConfig:
    """Load `<project_path>/.fabric/config.yaml`."""
    return load_config(Path(project_path) / ".fabric" / "config.yaml")


__all__ = [
    "Build",
    "ConfigError",
    "FabricConfig",
    "Labels",
    "Module",
    "Pipeline",
    "Project",
    "Safety",
    "load_config",
    "load_project_config",
]
