"""`_configure_root_logger` controls whether `fabric.*` log lines ever
reach stderr / journalctl. Cover the two things future-me would want
preserved: default level is INFO, `FABRIC_LOG_LEVEL` overrides."""

from __future__ import annotations

import logging

import pytest

from fabric.cli import _configure_root_logger


@pytest.fixture(autouse=True)
def _restore_root_logger() -> None:
    """`logging.basicConfig(force=True)` mutates the root logger globally;
    restore default state after each test so suite ordering is irrelevant."""
    original_level = logging.root.level
    original_handlers = logging.root.handlers[:]
    yield
    logging.root.setLevel(original_level)
    logging.root.handlers = original_handlers


def test_default_level_is_info() -> None:
    level = _configure_root_logger(env={})
    assert level == "INFO"
    assert logging.root.level == logging.INFO


def test_fabric_log_level_env_var_wins() -> None:
    level = _configure_root_logger(env={"FABRIC_LOG_LEVEL": "WARNING"})
    assert level == "WARNING"
    assert logging.root.level == logging.WARNING


def test_lowercase_level_is_normalized() -> None:
    _configure_root_logger(env={"FABRIC_LOG_LEVEL": "debug"})
    assert logging.root.level == logging.DEBUG


def test_fabric_dispatcher_logger_inherits_level() -> None:
    """The whole point: after configure, `fabric.dispatcher` lines at INFO
    actually emit. Without `basicConfig`, they'd be filtered out."""
    _configure_root_logger(env={})
    assert logging.getLogger("fabric.dispatcher").isEnabledFor(logging.INFO)
