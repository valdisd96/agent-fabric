from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from fabric.cli import app
from fabric.render import SKILL_NAMES, SKILL_OUTPUT_FILENAME

runner = CliRunner()


def _skill_path(project: Path, name: str) -> Path:
    return project / ".claude" / "skills" / name / SKILL_OUTPUT_FILENAME


def test_cli_sync_writes(project: Path) -> None:
    result = runner.invoke(app, ["sync", str(project)])
    assert result.exit_code == 0, result.output
    for name in SKILL_NAMES:
        assert _skill_path(project, name).exists()


def test_cli_sync_check_clean_returns_zero(project: Path) -> None:
    runner.invoke(app, ["sync", str(project)])
    result = runner.invoke(app, ["sync", str(project), "--check"])
    assert result.exit_code == 0, result.output
    assert "clean" in result.output


def test_cli_sync_check_drift_returns_one(project: Path) -> None:
    runner.invoke(app, ["sync", str(project)])
    edited = _skill_path(project, "plan-exec")
    edited.write_text("changed\n")

    result = runner.invoke(app, ["sync", str(project), "--check"])
    assert result.exit_code == 1, result.output
    assert "plan-exec" in result.output


def test_cli_sync_invalid_config_returns_two(tmp_path: Path) -> None:
    project = tmp_path / "bad"
    fabric_dir = project / ".fabric"
    fabric_dir.mkdir(parents=True)
    (fabric_dir / "config.yaml").write_text("project:\n  name: x\n  repo: bad\n")
    result = runner.invoke(app, ["sync", str(project)])
    assert result.exit_code == 2


def test_cli_sync_unknown_project_returns_two(tmp_path: Path) -> None:
    result = runner.invoke(app, ["sync", str(tmp_path / "missing")])
    assert result.exit_code == 2
