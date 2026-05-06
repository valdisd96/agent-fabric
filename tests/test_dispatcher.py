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
    assert state.quota_today("teach-me-eng-bot") == 1


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
    # good.yaml's pipeline.daily_dispatch_cap == 30; pre-fill the quota
    state.upsert_project(name="teach-me-eng-bot", path=str(project), repo="me/x")
    for _ in range(30):
        state.inc_quota("teach-me-eng-bot")

    d = Dispatcher(spawner=FakeSpawner())
    with pytest.raises(QuotaExceeded, match="30/30"):
        _aiorun(d.dispatch("teach-me-eng-bot", 1, "plan-exec"))


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
            "daily_dispatch_cap: 30",
            "daily_dispatch_cap: 30\n  downgrade_low_priority: true",
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
