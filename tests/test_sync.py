from __future__ import annotations

from pathlib import Path

import pytest

from fabric.registry import register
from fabric.render import SKILL_NAMES, SKILL_OUTPUT_FILENAME
from fabric.sync import SyncError, resolve_project, sync


def _skill_path(project: Path, name: str) -> Path:
    return project / ".claude" / "skills" / name / SKILL_OUTPUT_FILENAME


def test_sync_writes_all_skills(project: Path, fabric_root: Path) -> None:
    result = sync(project, fabric_root=fabric_root)
    assert sorted(result.written) == sorted(SKILL_NAMES)
    for name in SKILL_NAMES:
        assert _skill_path(project, name).is_file()


def test_sync_is_idempotent(project: Path, fabric_root: Path) -> None:
    sync(project, fabric_root=fabric_root)
    snapshot = {n: _skill_path(project, n).read_text() for n in SKILL_NAMES}

    second = sync(project, fabric_root=fabric_root)
    assert second.written == []
    for name, original in snapshot.items():
        assert _skill_path(project, name).read_text() == original


def test_check_clean_after_sync(project: Path, fabric_root: Path) -> None:
    sync(project, fabric_root=fabric_root)
    result = sync(project, check=True, fabric_root=fabric_root)
    assert result.drift == []
    assert result.written == []


def test_check_detects_drift(project: Path, fabric_root: Path) -> None:
    sync(project, fabric_root=fabric_root)
    edited = _skill_path(project, "plan-exec")
    edited.write_text(edited.read_text() + "MANUAL EDIT\n")

    result = sync(project, check=True, fabric_root=fabric_root)
    assert len(result.drift) == 1
    assert result.drift[0].name == "plan-exec"
    assert "MANUAL EDIT" in result.drift[0].actual
    assert "MANUAL EDIT" not in result.drift[0].expected


def test_check_does_not_write(project: Path, fabric_root: Path) -> None:
    skills_dir = project / ".claude" / "skills"
    assert not skills_dir.exists()
    result = sync(project, check=True, fabric_root=fabric_root)
    # Drift = "all of them missing"
    assert {d.name for d in result.drift} == set(SKILL_NAMES)
    assert not skills_dir.exists()


def test_drift_unified_diff_includes_paths(project: Path, fabric_root: Path) -> None:
    sync(project, fabric_root=fabric_root)
    edited = _skill_path(project, "plan-exec")
    edited.write_text("totally different\n")

    result = sync(project, check=True, fabric_root=fabric_root)
    diff = result.drift[0].unified_diff(project)
    assert "plan-exec" in diff
    assert "totally different" in diff


def test_sync_rejects_invalid_config(tmp_path: Path, fabric_root: Path) -> None:
    project = tmp_path / "bad"
    fabric_dir = project / ".fabric"
    fabric_dir.mkdir(parents=True)
    (fabric_dir / "config.yaml").write_text("project:\n  name: x\n  repo: invalid\n")
    with pytest.raises(SyncError):
        sync(project, fabric_root=fabric_root)


def test_sync_rejects_missing_project(tmp_path: Path, fabric_root: Path) -> None:
    with pytest.raises(SyncError):
        sync(tmp_path / "nope", fabric_root=fabric_root)


def test_resolve_project_by_path(tmp_path: Path) -> None:
    p = tmp_path / "real"
    p.mkdir()
    assert resolve_project(str(p)) == p.resolve()


def test_resolve_project_by_registered_name(
    isolated_fabric_home: Path, project: Path
) -> None:
    register(project)
    assert resolve_project("teach-me-eng-bot") == project.resolve()


def test_resolve_project_unknown_raises(isolated_fabric_home: Path) -> None:
    with pytest.raises(SyncError, match="not found"):
        resolve_project("definitely-not-a-project")
