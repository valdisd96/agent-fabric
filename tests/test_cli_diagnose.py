"""End-to-end CLI tests for `fabric diagnose`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from fabric import state
from fabric.cli import app
from fabric.dispatcher import Dispatcher, DispatchError, DispatchResult

runner = CliRunner()


def _seed_failed_deployment(project: str = "teach-me-eng-bot") -> int:
    state.upsert_project(name=project, path="/tmp/x", repo="me/x")
    return state.record_deployment(
        project=project, sha="deadbeef", status="failed",
    ).id


def test_diagnose_invokes_dispatcher_and_prints_summary(
    isolated_state_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    deployment_id = _seed_failed_deployment()

    calls: list[tuple[str, int]] = []

    async def fake(
        self: Any, project: str, deployment_id_arg: int, **kwargs: Any
    ) -> DispatchResult:
        calls.append((project, deployment_id_arg))
        return DispatchResult(
            dispatch_id=42,
            exit_code=0,
            log_path=tmp_path / "diag.log",
            duration_s=1.5,
        )

    monkeypatch.setattr(Dispatcher, "dispatch_deploy_diagnose", fake)

    res = runner.invoke(app, ["diagnose", "teach-me-eng-bot", str(deployment_id)])
    assert res.exit_code == 0, res.output
    assert calls == [("teach-me-eng-bot", deployment_id)]
    assert "id=42" in res.output
    assert "exit=0" in res.output


def test_diagnose_returns_one_on_dispatch_error(
    isolated_state_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake(
        self: Any, project: str, deployment_id: int, **kwargs: Any
    ) -> DispatchResult:
        raise DispatchError("deployment 999 not found")

    monkeypatch.setattr(Dispatcher, "dispatch_deploy_diagnose", fake)

    res = runner.invoke(app, ["diagnose", "teach-me-eng-bot", "999"])
    assert res.exit_code == 1
    assert "not found" in (res.output + (res.stderr or ""))
