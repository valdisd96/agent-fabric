from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from fabric import state
from fabric.dispatcher import Dispatcher, DispatchError, QuotaExceeded
from fabric.registry import register

FIXTURES = Path(__file__).parent / "fixtures"


# ---------- helpers ----------


def _aiorun(coro: Any) -> Any:
    return asyncio.run(coro)


def _make_project(root: Path) -> Path:
    fabric_dir = root / ".fabric"
    fabric_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "good.yaml", fabric_dir / "config.yaml")
    return root


@dataclass
class _AsyncByteLines:
    """Async iterator over a list of bytes lines (for proc.stdout)."""

    lines: list[bytes]

    def __aiter__(self) -> "_AsyncByteLines":
        return self

    async def __anext__(self) -> bytes:
        if not self.lines:
            raise StopAsyncIteration
        return self.lines.pop(0)


@dataclass
class _OverflowingByteLines:
    """Yields some lines then raises asyncio.LimitOverrunError, simulating
    a single stdout line bigger than the StreamReader's buffer limit."""

    lines: list[bytes]

    def __aiter__(self) -> "_OverflowingByteLines":
        return self

    async def __anext__(self) -> bytes:
        if self.lines:
            return self.lines.pop(0)
        raise asyncio.LimitOverrunError("simulated", consumed=2**16)


@dataclass
class FakeProc:
    stdout: Any  # _AsyncByteLines or _OverflowingByteLines
    exit_code: int = 0
    terminate_called: bool = False
    kill_called: bool = False

    async def wait(self) -> int:
        return self.exit_code

    def terminate(self) -> None:
        self.terminate_called = True

    def kill(self) -> None:
        self.kill_called = True


@dataclass
class FakeSpawner:
    """Records argv + kwargs per call, returns scripted FakeProc instances."""

    lines: list[str] = field(default_factory=list)
    exit_code: int = 0
    overflow_after_lines: bool = False
    calls: list[list[str]] = field(default_factory=list)
    kwargs_calls: list[dict[str, Any]] = field(default_factory=list)
    procs: list[FakeProc] = field(default_factory=list)
    on_call: Any = None

    async def __call__(self, args: list[str], **kwargs: Any) -> FakeProc:
        self.calls.append(list(args))
        self.kwargs_calls.append(dict(kwargs))
        if self.on_call is not None:
            await self.on_call()
        bytes_lines = [(line + "\n").encode() for line in self.lines]
        stdout: Any = (
            _OverflowingByteLines(bytes_lines)
            if self.overflow_after_lines
            else _AsyncByteLines(bytes_lines)
        )
        proc = FakeProc(stdout=stdout, exit_code=self.exit_code)
        self.procs.append(proc)
        return proc


@dataclass
class _GhRunResult:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class FakeGhRunner:
    responses: list[_GhRunResult] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)
    inputs: list[str | None] = field(default_factory=list)

    def __call__(self, args: list[str], **kwargs: Any) -> _GhRunResult:
        self.calls.append(list(args))
        self.inputs.append(kwargs.get("input"))
        if not self.responses:
            return _GhRunResult()
        return self.responses.pop(0)

    def queue(self, stdout: str = "", returncode: int = 0) -> None:
        self.responses.append(_GhRunResult(returncode=returncode, stdout=stdout))


# ---------- dispatch happy path ----------


def test_dispatch_runs_subprocess_and_writes_log(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    project = _make_project(tmp_path / "proj")
    register(project)
    spawner = FakeSpawner(lines=["hello", "world"], exit_code=0)
    d = Dispatcher(spawner=spawner)

    result = _aiorun(d.dispatch("teach-me-eng-bot", 1, "plan-exec"))

    assert result.exit_code == 0
    assert result.log_path.exists()
    log = result.log_path.read_text()
    assert "hello" in log and "world" in log


def test_dispatch_argv_uses_default_model_and_project_path(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    project = _make_project(tmp_path / "proj")
    register(project)
    spawner = FakeSpawner()
    d = Dispatcher(spawner=spawner)

    _aiorun(d.dispatch("teach-me-eng-bot", 7, "plan-exec"))

    args = spawner.calls[0]
    assert args[:3] == ["claude", "-p", "--model"]
    assert "claude-opus-4-7" in args
    assert "--add-dir" in args
    assert str(project.resolve()) in args
    assert any("plan-exec" in a and "issue #7" in a for a in args)


def test_dispatch_argv_enables_stream_json_verbose_logging(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    """`--output-format stream-json --verbose` makes claude emit one JSON
    event per tool call / assistant message / tool result. Without
    --verbose, stream-json suppresses tool events; without stream-json,
    we get only the final summary message (the pre-existing thin logs)."""
    project = _make_project(tmp_path / "proj")
    register(project)
    spawner = FakeSpawner()
    d = Dispatcher(spawner=spawner)

    _aiorun(d.dispatch("teach-me-eng-bot", 7, "plan-exec"))

    args = spawner.calls[0]
    assert "--output-format" in args
    fmt_idx = args.index("--output-format")
    assert args[fmt_idx + 1] == "stream-json"
    assert "--verbose" in args


def test_dispatch_argv_isolates_prompt_from_variadic_add_dir(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    """Regression: `--add-dir <directories...>` is variadic. The prompt
    must sit after a `--` separator so claude does not absorb it as a
    second directory and abort with "Input must be provided …"."""

    project = _make_project(tmp_path / "proj")
    register(project)
    spawner = FakeSpawner()
    d = Dispatcher(spawner=spawner)

    _aiorun(d.dispatch("teach-me-eng-bot", 7, "plan-exec"))

    args = spawner.calls[0]
    assert "--" in args, "prompt must follow a -- separator"
    sep_idx = args.index("--")
    add_dir_idx = args.index("--add-dir")
    assert add_dir_idx < sep_idx, "--add-dir must come before -- so its argument is unambiguous"
    prompt_idx = next(
        i for i, a in enumerate(args) if "plan-exec" in a and "issue #7" in a
    )
    assert prompt_idx > sep_idx, "prompt must come after --"


def test_dispatch_argv_sets_bypass_permission_mode(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    """Unattended dispatch needs `--permission-mode bypassPermissions` —
    the default mode prompts on every tool call, which never resolves
    without a TTY and silently produces empty output."""

    project = _make_project(tmp_path / "proj")
    register(project)
    spawner = FakeSpawner()
    d = Dispatcher(spawner=spawner)

    _aiorun(d.dispatch("teach-me-eng-bot", 1, "plan-exec"))

    args = spawner.calls[0]
    assert "--permission-mode" in args
    mode_idx = args.index("--permission-mode")
    assert args[mode_idx + 1] == "bypassPermissions"


def test_dispatch_records_started_completed_and_quota(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    project = _make_project(tmp_path / "proj")
    register(project)
    d = Dispatcher(spawner=FakeSpawner(exit_code=0))

    _aiorun(d.dispatch("teach-me-eng-bot", 1, "plan-exec"))

    latest = state.latest_dispatch("teach-me-eng-bot", 1)
    assert latest is not None
    assert latest.exit_code == 0
    assert latest.ended_at is not None
    assert latest.log_path is not None
    assert state.dispatches_in_window("teach-me-eng-bot", 5) == 1


def test_dispatch_propagates_nonzero_exit(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    project = _make_project(tmp_path / "proj")
    register(project)
    d = Dispatcher(spawner=FakeSpawner(lines=["boom"], exit_code=2))

    res = _aiorun(d.dispatch("teach-me-eng-bot", 1, "plan-exec"))

    assert res.exit_code == 2
    latest = state.latest_dispatch("teach-me-eng-bot", 1)
    assert latest is not None and latest.exit_code == 2


def test_dispatch_logs_start_and_end_at_info(
    isolated_state_db: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """journalctl -u fabric should narrate every dispatch — silence on a
    successful tick is fine, but a dispatch landing must produce visible
    log lines. Otherwise the operator has no way to tell from the journal
    alone whether anything happened."""
    project = _make_project(tmp_path / "proj")
    register(project)
    d = Dispatcher(spawner=FakeSpawner(lines=["ok"], exit_code=0))

    with caplog.at_level("INFO", logger="fabric.dispatcher"):
        _aiorun(d.dispatch("teach-me-eng-bot", 7, "plan-exec"))

    starts = [r for r in caplog.records if "dispatch start" in r.message]
    ends = [r for r in caplog.records if "dispatch end" in r.message]
    assert len(starts) == 1, [r.message for r in caplog.records]
    assert len(ends) == 1
    assert "issue=7" in starts[0].message
    assert "stage=plan-exec" in starts[0].message
    assert "exit=0" in ends[0].message


def test_dispatch_logs_failure_at_warning(
    isolated_state_db: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Non-zero exits should hit WARNING and include the log path so the
    operator can jump straight to the per-dispatch transcript."""
    project = _make_project(tmp_path / "proj")
    register(project)
    d = Dispatcher(spawner=FakeSpawner(lines=["boom"], exit_code=1))

    with caplog.at_level("INFO", logger="fabric.dispatcher"):
        _aiorun(d.dispatch("teach-me-eng-bot", 1, "plan-exec"))

    failures = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "dispatch FAILED" in r.message
    ]
    assert len(failures) == 1, [r.message for r in caplog.records]
    assert "exit=1" in failures[0].message
    assert "log=" in failures[0].message


# ---------- error paths ----------


def test_dispatch_unknown_project_raises(isolated_state_db: Path) -> None:
    d = Dispatcher(spawner=FakeSpawner())
    with pytest.raises(DispatchError, match="not registered"):
        _aiorun(d.dispatch("nope", 1, "plan-exec"))


def test_dispatch_quota_cap_raises(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    project = _make_project(tmp_path / "proj")
    register(project)
    # good.yaml's pipeline.dispatch_cap == 30 over a 5h window; pre-fill 30
    # dispatches inside the window so the next one trips the cap.
    state.upsert_project(name="teach-me-eng-bot", path=str(project), repo="me/x")
    from datetime import datetime, timedelta, timezone
    inside = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(
        timespec="seconds"
    )
    for _ in range(30):
        state.record_dispatch(
            project="teach-me-eng-bot", issue=1, stage="plan-exec",
            started_at=inside,
        )

    d = Dispatcher(spawner=FakeSpawner())
    with pytest.raises(QuotaExceeded, match=r"30/30 in last 5h"):
        _aiorun(d.dispatch("teach-me-eng-bot", 1, "plan-exec"))


def test_dispatch_quota_window_refills(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    """Dispatches older than the rolling window should not count against the cap."""
    project = _make_project(tmp_path / "proj")
    register(project)
    state.upsert_project(name="teach-me-eng-bot", path=str(project), repo="me/x")
    from datetime import datetime, timedelta, timezone
    # 30 old dispatches outside the 5h window — shouldn't block a new one.
    aged_out = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat(
        timespec="seconds"
    )
    for _ in range(30):
        state.record_dispatch(
            project="teach-me-eng-bot", issue=1, stage="plan-exec",
            started_at=aged_out,
        )

    d = Dispatcher(spawner=FakeSpawner(exit_code=0))
    res = _aiorun(d.dispatch("teach-me-eng-bot", 2, "plan-exec"))
    assert res.exit_code == 0


# ---------- model resolution ----------


def test_dispatch_explicit_model_override(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    project = _make_project(tmp_path / "proj")
    register(project)
    spawner = FakeSpawner()
    d = Dispatcher(spawner=spawner)

    _aiorun(d.dispatch("teach-me-eng-bot", 1, "plan-exec", model="some-other-model"))

    assert "some-other-model" in spawner.calls[0]


def test_dispatch_does_not_downgrade_when_flag_off(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    """good.yaml has downgrade_low_priority unset (default False)."""
    project = _make_project(tmp_path / "proj")
    register(project)
    state.upsert_project(name="teach-me-eng-bot", path=str(project), repo="me/x")
    state.upsert_issue(project="teach-me-eng-bot", number=1, priority_label="priority:low")

    spawner = FakeSpawner()
    d = Dispatcher(spawner=spawner)
    _aiorun(d.dispatch("teach-me-eng-bot", 1, "plan-exec"))

    assert "claude-opus-4-7" in spawner.calls[0]
    assert "claude-sonnet-4-6" not in spawner.calls[0]


def test_dispatch_downgrades_low_priority_when_flag_set(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    project = _make_project(tmp_path / "proj")
    # Patch the config to enable the downgrade
    cfg_path = project / ".fabric" / "config.yaml"
    text = cfg_path.read_text()
    cfg_path.write_text(
        text.replace(
            "dispatch_cap: 30",
            "dispatch_cap: 30\n  downgrade_low_priority: true",
        )
    )
    register(project)
    state.upsert_project(name="teach-me-eng-bot", path=str(project), repo="me/x")
    state.upsert_issue(project="teach-me-eng-bot", number=1, priority_label="priority:low")

    spawner = FakeSpawner()
    d = Dispatcher(spawner=spawner)
    _aiorun(d.dispatch("teach-me-eng-bot", 1, "plan-exec"))

    assert "claude-sonnet-4-6" in spawner.calls[0]


# ---------- pubsub ----------


def test_dispatch_publishes_started_stdout_ended_events(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    project = _make_project(tmp_path / "proj")
    register(project)
    d = Dispatcher(spawner=FakeSpawner(lines=["a", "b"]))

    async def _run() -> list[dict[str, Any]]:
        q = d.subscribe()
        await d.dispatch("teach-me-eng-bot", 1, "plan-exec")
        events: list[dict[str, Any]] = []
        while not q.empty():
            events.append(q.get_nowait())
        return events

    events = _aiorun(_run())
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "dispatch_started"
    assert kinds[-1] == "dispatch_ended"
    stdout_events = [e for e in events if e["kind"] == "dispatch_stdout"]
    assert [e["line"] for e in stdout_events] == ["a", "b"]


def test_subscribe_drops_oldest_when_queue_full(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    project = _make_project(tmp_path / "proj")
    register(project)
    # 5 stdout lines, queue maxsize=2 → only the last 2 stdout + dispatch_ended fit
    d = Dispatcher(spawner=FakeSpawner(lines=["1", "2", "3", "4", "5"]))

    async def _run() -> list[dict[str, Any]]:
        q = d.subscribe(maxsize=2)
        await d.dispatch("teach-me-eng-bot", 1, "plan-exec")
        events: list[dict[str, Any]] = []
        while not q.empty():
            events.append(q.get_nowait())
        return events

    events = _aiorun(_run())
    # exactly maxsize survive
    assert len(events) == 2


# ---------- single-flight ----------


def test_dispatch_serializes_concurrent_calls(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    project = _make_project(tmp_path / "proj")
    register(project)

    in_flight = 0
    max_in_flight = 0
    enter = asyncio.Event()
    proceed = asyncio.Event()

    async def gate() -> None:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        enter.set()
        await proceed.wait()
        in_flight -= 1

    async def _run() -> None:
        spawner = FakeSpawner(lines=["x"], on_call=gate)
        d = Dispatcher(spawner=spawner)
        t1 = asyncio.create_task(d.dispatch("teach-me-eng-bot", 1, "plan-exec"))
        await enter.wait()
        # second call should block on the semaphore
        t2 = asyncio.create_task(d.dispatch("teach-me-eng-bot", 2, "plan-exec"))
        await asyncio.sleep(0)
        proceed.set()
        await asyncio.gather(t1, t2)

    _aiorun(_run())
    assert max_in_flight == 1


# ---------- bump_cycle (max-of-both) ----------


def _detail_json(comments: list[dict[str, Any]] | None = None) -> str:
    return json.dumps(
        {
            "number": 1,
            "title": "x",
            "url": "u",
            "createdAt": "t",
            "state": "OPEN",
            "comments": comments or [],
        }
    )


def test_bump_cycle_first_call_writes_one(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    project = _make_project(tmp_path / "proj")
    register(project)
    gh_runner = FakeGhRunner()
    gh_runner.queue(stdout=_detail_json())  # _read_cycle_from_gh
    gh_runner.queue(stdout=json.dumps({"comments": []}))  # set_html_comment view
    gh_runner.queue(stdout="https://example/c1\n")  # set_html_comment create

    d = Dispatcher(gh_runner=gh_runner)
    n = _aiorun(d.bump_cycle("teach-me-eng-bot", 1))

    assert n == 1
    row = state.get_issue("teach-me-eng-bot", 1)
    assert row is not None and row.cycle_count == 1


def test_bump_cycle_takes_max_of_db_and_gh(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    project = _make_project(tmp_path / "proj")
    register(project)
    # DB says 2; GH comment says 5 → next should be 6
    state.upsert_project(name="teach-me-eng-bot", path=str(project), repo="me/x")
    state.upsert_issue(project="teach-me-eng-bot", number=1)
    state.set_cycle_count("teach-me-eng-bot", 1, 2)

    gh_runner = FakeGhRunner()
    gh_runner.queue(
        stdout=json.dumps(
            {
                "number": 1,
                "title": "x",
                "url": "u",
                "comments": [
                    {
                        "body": "<!-- agent-fabric:cycle -->\n5",
                        "url": "https://example/i/1#issuecomment-100",
                    }
                ],
            }
        )
    )
    # set_html_comment lookup + edit
    gh_runner.queue(
        stdout=json.dumps(
            {
                "comments": [
                    {
                        "body": "<!-- agent-fabric:cycle -->\n5",
                        "url": "https://example/i/1#issuecomment-100",
                    }
                ]
            }
        )
    )
    gh_runner.queue(stdout="")  # PATCH

    d = Dispatcher(gh_runner=gh_runner)
    n = _aiorun(d.bump_cycle("teach-me-eng-bot", 1))

    assert n == 6
    row = state.get_issue("teach-me-eng-bot", 1)
    assert row is not None and row.cycle_count == 6


def test_bump_cycle_ignores_unparseable_comment(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    project = _make_project(tmp_path / "proj")
    register(project)
    state.upsert_project(name="teach-me-eng-bot", path=str(project), repo="me/x")
    state.upsert_issue(project="teach-me-eng-bot", number=1)
    state.set_cycle_count("teach-me-eng-bot", 1, 4)

    gh_runner = FakeGhRunner()
    gh_runner.queue(
        stdout=json.dumps(
            {
                "number": 1,
                "title": "x",
                "url": "u",
                "comments": [
                    {
                        "body": "<!-- agent-fabric:cycle -->\nnot-a-number",
                        "url": "https://example/i/1#issuecomment-100",
                    }
                ],
            }
        )
    )
    gh_runner.queue(stdout=json.dumps({"comments": []}))
    gh_runner.queue(stdout="https://example/new\n")

    d = Dispatcher(gh_runner=gh_runner)
    n = _aiorun(d.bump_cycle("teach-me-eng-bot", 1))

    # falls back to DB-only counter (4 + 1)
    assert n == 5


# ---------- stream overflow recovery ----------


def test_dispatch_passes_large_stream_limit_to_spawner(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    """Asyncio's default 64 KB buffer overflows on big stream-json events
    from the agent. We pass a 16 MB limit so the read survives any
    realistic tool-result payload."""
    project = _make_project(tmp_path / "proj")
    register(project)
    spawner = FakeSpawner()
    d = Dispatcher(spawner=spawner)

    _aiorun(d.dispatch("teach-me-eng-bot", 1, "plan-exec"))

    assert spawner.kwargs_calls, "spawner not called"
    limit = spawner.kwargs_calls[0].get("limit")
    assert limit is not None
    assert limit >= 16 * 1024 * 1024


def test_dispatch_recovers_from_stdout_overflow(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    """If a single stdout line exceeds the 16 MB reader limit anyway,
    the dispatcher must terminate the subprocess (not orphan it),
    record the dispatch row as failed (exit_code=-1), and return
    cleanly so the scheduler can retry / block via the normal path.
    Without this fix the row stayed open and parallel `claude -p`
    subprocesses ran simultaneously, breaking the single-flight
    invariant."""
    project = _make_project(tmp_path / "proj")
    register(project)
    spawner = FakeSpawner(
        lines=["pre-overflow line"],
        exit_code=0,
        overflow_after_lines=True,
    )
    d = Dispatcher(spawner=spawner)

    result = _aiorun(d.dispatch("teach-me-eng-bot", 1, "plan-exec"))

    assert result.exit_code == -1, "expected synthetic overflow failure code"
    assert spawner.procs, "spawner did not return any procs"
    assert spawner.procs[-1].terminate_called, "subprocess was not terminated"

    # Dispatch row is closed (ended_at populated) so consecutive_failures
    # accounting and single-flight stay coherent.
    rows = state.dispatches_for_issue("teach-me-eng-bot", 1)
    assert rows, "no dispatch row recorded"
    assert all(r.ended_at for r in rows), "row left with NULL ended_at"
    assert rows[-1].exit_code == -1


def test_dispatch_overflow_log_captures_truncation_marker(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    """The on-disk log gets a clear marker at the truncation point so an
    operator reading the log knows why output stopped."""
    project = _make_project(tmp_path / "proj")
    register(project)
    spawner = FakeSpawner(
        lines=["normal output line"],
        overflow_after_lines=True,
    )
    d = Dispatcher(spawner=spawner)
    result = _aiorun(d.dispatch("teach-me-eng-bot", 1, "plan-exec"))

    log_text = result.log_path.read_text()
    assert "normal output line" in log_text
    assert "stdout line exceeded reader limit" in log_text
    assert "subprocess terminated" in log_text


# ---------- dispatch_deploy_diagnose ----------


def _record_failed_deployment(
    *,
    project: str = "teach-me-eng-bot",
    sha: str = "deadbeef",
    journal_tail: str = "ModuleNotFoundError: redis",
) -> state.DeploymentRow:
    state.upsert_project(name=project, path="/p", repo="me/x")
    return state.record_deployment(
        project=project,
        sha=sha,
        status="failed",
        deployed_at="2026-05-06T18:05:00+00:00",
        service_unit=f"{project}.service",
        workflow_run_url="https://github.com/me/x/actions/runs/2",
        journal_tail=journal_tail,
    )


def test_dispatch_diagnose_runs_subprocess_and_writes_log(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    project = _make_project(tmp_path / "proj")
    register(project)
    failed = _record_failed_deployment()
    spawner = FakeSpawner(lines=["diagnosing", "filed issue #99"], exit_code=0)
    d = Dispatcher(spawner=spawner)

    result = _aiorun(d.dispatch_deploy_diagnose("teach-me-eng-bot", failed.id))

    assert result.exit_code == 0
    assert result.log_path.exists()
    text = result.log_path.read_text()
    assert "diagnosing" in text and "filed issue #99" in text


def test_dispatch_diagnose_argv_includes_bundle_json(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    """The agent reads the failure context from the prompt — the bundle
    must be inlined as JSON, with the failed sha and journal tail visible."""
    project = _make_project(tmp_path / "proj")
    register(project)
    failed = _record_failed_deployment(
        sha="cafebabe1234567",
        journal_tail="Traceback ... ImportError: redis",
    )
    spawner = FakeSpawner()
    d = Dispatcher(spawner=spawner)

    _aiorun(d.dispatch_deploy_diagnose("teach-me-eng-bot", failed.id))

    args = spawner.calls[0]
    assert "claude" in args[0] and "-p" in args
    # prompt sits after the `--` separator (same as regular dispatch)
    sep_idx = args.index("--")
    prompt = args[sep_idx + 1]
    assert "deploy-diagnose" in prompt
    assert "cafebabe1234567" in prompt
    assert "ImportError: redis" in prompt
    # And we still pass --add-dir for the project
    assert "--add-dir" in args
    assert str(project.resolve()) in args


def test_dispatch_diagnose_includes_previous_good_sha(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    project = _make_project(tmp_path / "proj")
    register(project)
    state.upsert_project(name="teach-me-eng-bot", path="/p", repo="me/x")
    state.record_deployment(
        project="teach-me-eng-bot", sha="goodsha", status="success",
        deployed_at="2026-05-05T10:00:00+00:00",
    )
    failed = state.record_deployment(
        project="teach-me-eng-bot", sha="badsha", status="failed",
        deployed_at="2026-05-06T10:00:00+00:00",
    )
    spawner = FakeSpawner()
    d = Dispatcher(spawner=spawner)

    _aiorun(d.dispatch_deploy_diagnose("teach-me-eng-bot", failed.id))

    prompt = spawner.calls[0][-1]
    assert "goodsha" in prompt, "previous_good_sha must be in the bundle"
    assert "badsha" in prompt


def test_dispatch_diagnose_uses_opus_model_by_default(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    """Diagnose is reasoning-heavy — never downgrade to sonnet, even if the
    project's config has downgrade_low_priority set. There is no priority
    label on a deployment row."""
    project = _make_project(tmp_path / "proj")
    register(project)
    failed = _record_failed_deployment()
    spawner = FakeSpawner()
    d = Dispatcher(spawner=spawner)

    _aiorun(d.dispatch_deploy_diagnose("teach-me-eng-bot", failed.id))

    args = spawner.calls[0]
    model_idx = args.index("--model")
    assert args[model_idx + 1] == "claude-opus-4-7"


def test_dispatch_diagnose_records_issue_zero_with_deployment_id(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    """Dispatch row uses issue=0 sentinel but carries the deployment_id so
    the audit trail is unambiguous."""
    project = _make_project(tmp_path / "proj")
    register(project)
    failed = _record_failed_deployment()
    d = Dispatcher(spawner=FakeSpawner(exit_code=0))

    result = _aiorun(d.dispatch_deploy_diagnose("teach-me-eng-bot", failed.id))

    rows = state.recent_dispatches(project="teach-me-eng-bot")
    diag = next(r for r in rows if r.id == result.dispatch_id)
    assert diag.issue == 0
    assert diag.stage == "deploy-diagnose"
    assert diag.deployment_id == failed.id
    assert diag.triggered_by == f"auto:deploy-failure-{failed.id}"


def test_dispatch_diagnose_links_deployment_row(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    """The deployments row gets `diagnose_dispatch_id` set so the dashboard
    can navigate from a failure to its diagnose log."""
    project = _make_project(tmp_path / "proj")
    register(project)
    failed = _record_failed_deployment()
    d = Dispatcher(spawner=FakeSpawner(exit_code=0))

    result = _aiorun(d.dispatch_deploy_diagnose("teach-me-eng-bot", failed.id))

    refreshed = state.get_deployment(failed.id)
    assert refreshed is not None
    assert refreshed.diagnose_dispatch_id == result.dispatch_id


def test_dispatch_diagnose_raises_on_unknown_deployment(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    project = _make_project(tmp_path / "proj")
    register(project)
    d = Dispatcher(spawner=FakeSpawner())
    with pytest.raises(DispatchError, match="deployment 9999 not found"):
        _aiorun(d.dispatch_deploy_diagnose("teach-me-eng-bot", 9999))


def test_dispatch_diagnose_rejects_cross_project_deployment(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    """A deployment id that exists but belongs to a different project must
    not be diagnosed under the wrong name — that would render the wrong repo
    paths and file the issue against the wrong tracker."""
    project = _make_project(tmp_path / "proj")
    register(project)
    state.upsert_project(name="other", path="/q", repo="me/other")
    other_failure = state.record_deployment(
        project="other", sha="x", status="failed",
    )
    d = Dispatcher(spawner=FakeSpawner())
    with pytest.raises(DispatchError, match="belongs to project 'other'"):
        _aiorun(d.dispatch_deploy_diagnose("teach-me-eng-bot", other_failure.id))


def test_dispatch_diagnose_respects_rolling_quota(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    project = _make_project(tmp_path / "proj")
    register(project)
    failed = _record_failed_deployment()
    # good.yaml fixture caps at 30 in any 5h rolling window
    from datetime import datetime, timedelta, timezone
    inside = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(
        timespec="seconds"
    )
    for _ in range(30):
        state.record_dispatch(
            project="teach-me-eng-bot", issue=1, stage="plan-exec",
            started_at=inside,
        )
    d = Dispatcher(spawner=FakeSpawner())
    with pytest.raises(QuotaExceeded):
        _aiorun(d.dispatch_deploy_diagnose("teach-me-eng-bot", failed.id))
