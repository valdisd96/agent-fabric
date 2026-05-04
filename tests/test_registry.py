from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import io

from fabric.registry import (
    ProjectEntry,
    RegistryError,
    fabric_home,
    find,
    load_registry,
    parse_env_file_fabric_home,
    register,
    registry_path,
    warn_if_systemd_env_diverges,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _make_project(root: Path, fixture: str = "good.yaml") -> Path:
    """Create a fake project at `root` with `.fabric/config.yaml` from fixtures."""
    fabric_dir = root / ".fabric"
    fabric_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / fixture, fabric_dir / "config.yaml")
    return root


def test_fabric_home_uses_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FABRIC_HOME", str(tmp_path / "custom"))
    assert fabric_home() == tmp_path / "custom"


def test_fabric_home_defaults_to_dot_fabric(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FABRIC_HOME", raising=False)
    assert fabric_home() == Path.home() / ".fabric"


def test_load_registry_returns_empty_when_missing(isolated_fabric_home: Path) -> None:
    assert load_registry() == []


def test_register_writes_entry(isolated_fabric_home: Path, tmp_path: Path) -> None:
    project = _make_project(tmp_path / "proj")
    result = register(project)
    assert result.replaced is False
    assert result.entry.name == "teach-me-eng-bot"
    assert result.entry.repo == "valdisd96/teach-me-eng-bot"
    assert Path(result.entry.path) == project.resolve()
    assert registry_path().exists()
    assert load_registry() == [result.entry]


def test_register_dedupes_by_name_and_updates_path(
    isolated_fabric_home: Path, tmp_path: Path
) -> None:
    first = _make_project(tmp_path / "first")
    register(first)

    second = _make_project(tmp_path / "second")
    result = register(second)

    assert result.replaced is True
    assert Path(result.entry.path) == second.resolve()

    entries = load_registry()
    assert len(entries) == 1
    assert Path(entries[0].path) == second.resolve()


def test_register_rejects_missing_config(
    isolated_fabric_home: Path, tmp_path: Path
) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(RegistryError, match="file not found"):
        register(bare)


def test_register_rejects_invalid_config(
    isolated_fabric_home: Path, tmp_path: Path
) -> None:
    project = _make_project(tmp_path / "bad", fixture="bad_repo_format.yaml")
    with pytest.raises(RegistryError) as exc:
        register(project)
    assert "project.repo" in str(exc.value)


def test_register_rejects_non_directory(
    isolated_fabric_home: Path, tmp_path: Path
) -> None:
    f = tmp_path / "f.txt"
    f.write_text("hi")
    with pytest.raises(RegistryError, match="not a directory"):
        register(f)


def test_find_returns_entry(isolated_fabric_home: Path, tmp_path: Path) -> None:
    project = _make_project(tmp_path / "proj")
    register(project)
    entry = find("teach-me-eng-bot")
    assert isinstance(entry, ProjectEntry)
    assert entry.repo == "valdisd96/teach-me-eng-bot"


def test_find_returns_none_for_unknown(isolated_fabric_home: Path) -> None:
    assert find("nope") is None


# ---- /etc/fabric/env consistency check ---------------------------------


def test_parse_env_file_fabric_home_extracts_value(tmp_path: Path) -> None:
    f = tmp_path / "env"
    f.write_text(
        "# header comment\n"
        "FABRIC_HOME=/var/lib/fabric\n"
        "FABRIC_PORT=7878\n"
    )
    assert parse_env_file_fabric_home(f) == "/var/lib/fabric"


def test_parse_env_file_fabric_home_handles_quotes_and_whitespace(tmp_path: Path) -> None:
    f = tmp_path / "env"
    f.write_text('  FABRIC_HOME = "/var/lib/fabric"  \n')
    assert parse_env_file_fabric_home(f) == "/var/lib/fabric"


def test_parse_env_file_fabric_home_returns_none_when_unset(tmp_path: Path) -> None:
    f = tmp_path / "env"
    f.write_text("FABRIC_PORT=7878\n")
    assert parse_env_file_fabric_home(f) is None


def test_parse_env_file_fabric_home_returns_none_when_missing(tmp_path: Path) -> None:
    assert parse_env_file_fabric_home(tmp_path / "absent") is None


def test_warn_when_env_unset_but_systemd_declares(tmp_path: Path) -> None:
    f = tmp_path / "env"
    f.write_text("FABRIC_HOME=/var/lib/fabric\n")
    buf = io.StringIO()
    emitted = warn_if_systemd_env_diverges(env={}, env_file=f, stream=buf)
    assert emitted is True
    msg = buf.getvalue()
    assert "/var/lib/fabric" in msg
    assert "export FABRIC_HOME=/var/lib/fabric" in msg


def test_warn_when_env_diverges_from_systemd(tmp_path: Path) -> None:
    f = tmp_path / "env"
    f.write_text("FABRIC_HOME=/var/lib/fabric\n")
    buf = io.StringIO()
    emitted = warn_if_systemd_env_diverges(
        env={"FABRIC_HOME": "/tmp/other"}, env_file=f, stream=buf
    )
    assert emitted is True
    assert "/var/lib/fabric" in buf.getvalue()
    assert "/tmp/other" in buf.getvalue()


def test_no_warn_when_env_matches_systemd(tmp_path: Path) -> None:
    f = tmp_path / "env"
    f.write_text("FABRIC_HOME=/var/lib/fabric\n")
    buf = io.StringIO()
    emitted = warn_if_systemd_env_diverges(
        env={"FABRIC_HOME": "/var/lib/fabric"}, env_file=f, stream=buf
    )
    assert emitted is False
    assert buf.getvalue() == ""


def test_no_warn_when_systemd_env_absent(tmp_path: Path) -> None:
    buf = io.StringIO()
    emitted = warn_if_systemd_env_diverges(
        env={}, env_file=tmp_path / "absent", stream=buf
    )
    assert emitted is False
    assert buf.getvalue() == ""
