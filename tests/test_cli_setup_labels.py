from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from fabric.cli import app
from fabric import github as gh
from fabric.labels import all_labels
from fabric.registry import register

runner = CliRunner()


def _register(project: Path) -> None:
    register(project)


def _all_clean_labels(area_names: list[str]) -> list[gh.LabelDetail]:
    return [
        gh.LabelDetail(name=s.name, color=s.color, description=s.description)
        for s in all_labels(area_names)
    ]


def test_setup_labels_check_clean_returns_zero(
    project: Path,
    isolated_fabric_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register(project)

    from fabric.config import load_project_config

    cfg = load_project_config(project)
    clean = _all_clean_labels(cfg.labels.area_labels)

    monkeypatch.setattr(gh, "list_labels", lambda repo, **kw: clean)

    result = runner.invoke(app, ["setup-labels", "teach-me-eng-bot", "--check"])
    assert result.exit_code == 0, result.output
    assert "labels are clean" in result.output


def test_setup_labels_check_drift_returns_one(
    project: Path,
    isolated_fabric_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register(project)

    monkeypatch.setattr(gh, "list_labels", lambda repo, **kw: [])

    result = runner.invoke(app, ["setup-labels", "teach-me-eng-bot", "--check"])
    assert result.exit_code == 1, result.output
    assert "drift detected" in result.output


def test_setup_labels_check_does_not_call_apply(
    project: Path,
    isolated_fabric_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--check is purely a diff; it must never invoke any write API."""
    _register(project)
    monkeypatch.setattr(gh, "list_labels", lambda repo, **kw: [])

    calls: list[str] = []

    def boom(*a, **kw) -> None:  # type: ignore[no-untyped-def]
        calls.append("called")
        raise AssertionError("apply_label_diff must not be called under --check")

    import fabric.labels as labels_mod
    monkeypatch.setattr(labels_mod, "apply_label_diff", boom)

    result = runner.invoke(app, ["setup-labels", "teach-me-eng-bot", "--check"])
    assert result.exit_code == 1
    assert calls == []
