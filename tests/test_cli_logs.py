"""End-to-end CLI tests for `fabric logs [--pretty]`."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from fabric import state
from fabric.cli import app

runner = CliRunner()


def _seed_dispatch(project: str, issue: int, log_path: Path) -> None:
    """Insert a dispatch row pointing at `log_path` so `fabric logs` finds
    it without needing a real claude run."""
    state.upsert_project(name=project, path="/tmp/x", repo="me/x")
    did = state.record_dispatch(
        project=project, issue=issue, stage="plan-exec",
        started_at="2026-05-05T20:00:00+00:00",
    )
    state.complete_dispatch(
        did, ended_at="2026-05-05T20:01:00+00:00", exit_code=0, log_path=str(log_path),
    )


def _stream_json_log(tmp_path: Path) -> Path:
    """A small but realistic stream-json dispatch log."""
    events = [
        {"type": "system", "subtype": "init",
         "session_id": "abcd1234zz", "model": "claude-opus-4-7"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Reading the issue body."},
            {"type": "tool_use", "name": "Read",
             "input": {"file_path": "issue.md"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "issue body text"},
        ]}},
        {"type": "result", "is_error": False,
         "duration_ms": 12345, "total_cost_usd": 0.0123},
    ]
    p = tmp_path / "dispatch.log"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return p


def test_logs_default_emits_raw_jsonl(
    isolated_state_db: Path, tmp_path: Path,
) -> None:
    """Without --pretty, `fabric logs` writes the raw JSONL byte-for-byte
    so it stays greppable / parseable / replayable."""
    log = _stream_json_log(tmp_path)
    _seed_dispatch("teach-me-eng-bot", 9, log)

    res = runner.invoke(app, ["logs", "teach-me-eng-bot", "9"])
    assert res.exit_code == 0
    # Raw mode contains the source JSONL verbatim and adds no pretty
    # artifacts (transcript markers, arrows, [init] / [result] tags).
    assert log.read_text() in res.output
    assert "[init]" not in res.output
    assert "[result]" not in res.output
    assert "→ " not in res.output


def test_logs_pretty_renders_transcript(
    isolated_state_db: Path, tmp_path: Path,
) -> None:
    """`--pretty` parses the JSONL and renders one human-readable line
    per event (init / assistant text / tool calls / tool results / result)."""
    log = _stream_json_log(tmp_path)
    _seed_dispatch("teach-me-eng-bot", 9, log)

    res = runner.invoke(app, ["logs", "teach-me-eng-bot", "9", "--pretty"])
    assert res.exit_code == 0
    assert "[init] model=claude-opus-4-7" in res.output
    assert "Reading the issue body." in res.output
    assert "→ Read(issue.md)" in res.output
    assert "← issue body text" in res.output
    assert "[result] ok duration=12345ms cost=$0.0123" in res.output


def test_logs_no_dispatch_returns_one(isolated_state_db: Path) -> None:
    res = runner.invoke(app, ["logs", "no-such-project", "1"])
    assert res.exit_code == 1
    assert "no dispatch found" in (res.output + (res.stderr or ""))
