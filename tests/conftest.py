from __future__ import annotations

import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def fabric_root() -> Path:
    """Directory containing `skill_templates/` — the `fabric` package dir."""
    return REPO_ROOT / "fabric"


@pytest.fixture
def isolated_fabric_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "fabric-home"
    monkeypatch.setenv("FABRIC_HOME", str(home))
    return home


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Create a fake project with a valid `.fabric/config.yaml`."""
    root = tmp_path / "proj"
    fabric_dir = root / ".fabric"
    fabric_dir.mkdir(parents=True)
    shutil.copy(FIXTURES / "good.yaml", fabric_dir / "config.yaml")
    return root


@pytest.fixture
def isolated_state_db(isolated_fabric_home: Path) -> Path:
    """Initialize an isolated state.db under FABRIC_HOME and return its path."""
    from fabric.state import init_db

    return init_db()
