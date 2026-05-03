from __future__ import annotations

from pathlib import Path

import pytest

from fabric.config import ConfigError, FabricConfig, load_config

FIXTURES = Path(__file__).parent / "fixtures"


def test_load_good_config() -> None:
    cfg = load_config(FIXTURES / "good.yaml")
    assert isinstance(cfg, FabricConfig)
    assert cfg.project.name == "teach-me-eng-bot"
    assert cfg.project.repo == "valdisd96/teach-me-eng-bot"
    assert cfg.build.test_cmd == "python -m pytest -q"
    assert cfg.modules[0].path == "bot.py"
    assert cfg.labels.area_labels == ["bot", "vocab"]
    assert cfg.pipeline.cycle_limit == 5
    assert cfg.fabric_version == "0.0.1"


def test_missing_file_raises() -> None:
    with pytest.raises(ConfigError, match="file not found"):
        load_config(FIXTURES / "does_not_exist.yaml")


def test_bad_repo_format_includes_field_path() -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(FIXTURES / "bad_repo_format.yaml")
    assert "project.repo" in str(exc.value)
    assert "owner/repo" in str(exc.value)


def test_missing_required_field_includes_field_path() -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(FIXTURES / "bad_missing_test_cmd.yaml")
    assert "build.test_cmd" in str(exc.value)


def test_extra_field_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(FIXTURES / "bad_extra_field.yaml")
    msg = str(exc.value)
    assert "project" in msg
    assert "unexpected_field" in msg


def test_invalid_pipeline_value_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(FIXTURES / "bad_negative_cycle_limit.yaml")
    assert "pipeline.cycle_limit" in str(exc.value)


def test_empty_file_rejected(tmp_path: Path) -> None:
    p = tmp_path / "empty.yaml"
    p.write_text("")
    with pytest.raises(ConfigError, match="empty"):
        load_config(p)


def test_invalid_yaml_rejected(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("project: [unclosed")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(p)
