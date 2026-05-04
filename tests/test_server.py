from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from fabric import state
from fabric.dispatcher import Dispatcher
from fabric.registry import register
from fabric.scheduler import Scheduler
from fabric.server import create_app

FIXTURES = Path(__file__).parent / "fixtures"


# ---------- helpers (mirrors test_dispatcher / test_scheduler patterns) ----------


def _write_project(
    root: Path, *, name: str = "teach-me-eng-bot",
    repo: str = "valdisd96/teach-me-eng-bot",
) -> Path:
    fabric_dir = root / ".fabric"
    fabric_dir.mkdir(parents=True, exist_ok=True)
    cfg = (FIXTURES / "good.yaml").read_text()
    cfg = cfg.replace("name: teach-me-eng-bot", f"name: {name}")
    cfg = cfg.replace("repo: valdisd96/teach-me-eng-bot", f"repo: {repo}")
    (fabric_dir / "config.yaml").write_text(cfg)
    return root


@dataclass
class _AsyncByteLines:
    lines: list[bytes]

    def __aiter__(self) -> "_AsyncByteLines":
        return self

    async def __anext__(self) -> bytes:
        if not self.lines:
            raise StopAsyncIteration
        return self.lines.pop(0)


@dataclass
class FakeProc:
    stdout: _AsyncByteLines
    exit_code: int = 0

    async def wait(self) -> int:
        return self.exit_code


@dataclass
class FakeSpawner:
    lines: list[str] = field(default_factory=list)
    exit_code: int = 0
    calls: list[list[str]] = field(default_factory=list)

    async def __call__(self, args: list[str], **kwargs: Any) -> FakeProc:
        self.calls.append(list(args))
        return FakeProc(
            stdout=_AsyncByteLines([(line + "\n").encode() for line in self.lines]),
            exit_code=self.exit_code,
        )


@dataclass
class _GhRunResult:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class FakeGhRunner:
    list_issues_payload: list[dict[str, Any]] = field(default_factory=list)
    issue_view_payload: dict[str, Any] | None = None
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, args: list[str], **kwargs: Any) -> _GhRunResult:
        self.calls.append(list(args))
        if args[1:4] == ["issue", "list", "--repo"]:
            return _GhRunResult(stdout=json.dumps(self.list_issues_payload))
        if args[1:3] == ["issue", "view"]:
            return _GhRunResult(
                stdout=json.dumps(self.issue_view_payload or {"comments": []})
            )
        if args[1:3] == ["issue", "comment"]:
            return _GhRunResult(stdout="https://example/c/1\n")
        return _GhRunResult(stdout="")


# ---------- fixtures ----------


@pytest.fixture
def setup(isolated_state_db: Path, tmp_path: Path) -> dict[str, Any]:
    project = _write_project(tmp_path / "proj")
    register(project)
    spawner = FakeSpawner()
    gh_runner = FakeGhRunner()
    dispatcher = Dispatcher(spawner=spawner, gh_runner=gh_runner)
    scheduler = Scheduler(dispatcher=dispatcher, gh_runner=gh_runner)
    app = create_app(
        scheduler=scheduler,
        dispatcher=dispatcher,
        gh_runner=gh_runner,
        tick_interval_s=3600.0,
    )
    return {
        "app": app,
        "dispatcher": dispatcher,
        "spawner": spawner,
        "gh_runner": gh_runner,
        "project": project,
    }


@pytest.fixture
def client(setup: dict[str, Any]) -> TestClient:
    with TestClient(setup["app"]) as c:
        yield c


# ---------- liveness ----------


def test_healthz_returns_ok(client: TestClient) -> None:
    """Liveness probe — must not depend on the DB, gh, or claude. Reverse
    proxies and uptime monitors hit this on every poll."""
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


# ---------- read paths ----------


def test_get_status_no_projects(client: TestClient) -> None:
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["paused"] is False
    assert body["paused_reason"] is None


def test_get_projects_returns_registered(client: TestClient) -> None:
    state.upsert_project(name="teach-me-eng-bot", path="/p", repo="me/x")
    r = client.get("/api/projects")
    assert r.status_code == 200
    names = [p["name"] for p in r.json()]
    assert "teach-me-eng-bot" in names


def test_get_issues_filters_by_project(client: TestClient) -> None:
    state.upsert_project(name="teach-me-eng-bot", path="/p", repo="me/x")
    state.upsert_issue(project="teach-me-eng-bot", number=1, state_label="state:needs-planning")
    state.upsert_issue(project="teach-me-eng-bot", number=2, state_label="state:in-review")

    r = client.get("/api/projects/teach-me-eng-bot/issues")
    assert r.status_code == 200
    nums = sorted(i["number"] for i in r.json())
    assert nums == [1, 2]


def test_get_issue_returns_404_when_unknown(client: TestClient) -> None:
    r = client.get("/api/issues/teach-me-eng-bot/999")
    assert r.status_code == 404


def test_get_dispatches(client: TestClient) -> None:
    state.upsert_project(name="teach-me-eng-bot", path="/p", repo="me/x")
    state.record_dispatch(project="teach-me-eng-bot", issue=1, stage="plan-exec")

    r = client.get("/api/dispatches?limit=10")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["project"] == "teach-me-eng-bot"


def test_get_log_returns_content(client: TestClient, tmp_path: Path) -> None:
    state.upsert_project(name="teach-me-eng-bot", path="/p", repo="me/x")
    log = tmp_path / "log.txt"
    log.write_text("line1\nline2\nline3\n")
    did = state.record_dispatch(
        project="teach-me-eng-bot", issue=1, stage="plan-exec",
        log_path=str(log),
    )
    state.complete_dispatch(did, exit_code=0, log_path=str(log))

    r = client.get(f"/api/dispatches/{did}/log")
    assert r.status_code == 200
    assert r.json()["content"].startswith("line1")

    r2 = client.get(f"/api/dispatches/{did}/log?tail=1")
    assert r2.json()["content"].strip() == "line3"


def test_get_log_404_when_dispatch_unknown(client: TestClient) -> None:
    r = client.get("/api/dispatches/999/log")
    assert r.status_code == 404


# ---------- write paths ----------


def test_post_pause_with_reason(client: TestClient) -> None:
    r = client.post("/api/pause", json={"reason": "vacation"})
    assert r.status_code == 200
    assert state.get_setting("paused") == "1"
    assert state.get_setting("paused_reason") == "vacation"


def test_post_resume_clears_state(client: TestClient) -> None:
    state.set_setting("paused", "1")
    state.set_setting("paused_reason", "vacation")
    r = client.post("/api/resume")
    assert r.status_code == 200
    assert state.get_setting("paused") == "0"
    assert state.get_setting("paused_reason") is None


def test_post_comment_calls_gh(client: TestClient, setup: dict[str, Any]) -> None:
    r = client.post(
        "/api/issues/teach-me-eng-bot/1/comment",
        json={"body": "hello world"},
    )
    assert r.status_code == 200
    assert "url" in r.json()
    # gh runner saw an `issue comment` invocation
    assert any(call[1:3] == ["issue", "comment"] for call in setup["gh_runner"].calls)


def test_post_comment_404_unknown_project(client: TestClient) -> None:
    r = client.post("/api/issues/nope/1/comment", json={"body": "x"})
    assert r.status_code == 404


def test_post_label_invokes_add_and_remove(
    client: TestClient, setup: dict[str, Any]
) -> None:
    r = client.post(
        "/api/issues/teach-me-eng-bot/1/label",
        json={"add": ["state:in-progress"], "remove": ["state:needs-planning"]},
    )
    assert r.status_code == 200
    add_called = any("--add-label" in c for c in setup["gh_runner"].calls)
    remove_called = any("--remove-label" in c for c in setup["gh_runner"].calls)
    assert add_called and remove_called


def test_post_dispatch_invokes_dispatcher(
    client: TestClient, setup: dict[str, Any]
) -> None:
    r = client.post(
        "/api/issues/teach-me-eng-bot/1/dispatch",
        json={"stage": "plan-exec"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["exit_code"] == 0
    # spawner saw a claude -p invocation
    assert any(c[:2] == ["claude", "-p"] for c in setup["spawner"].calls)


def test_post_dispatch_404_unknown_project(client: TestClient) -> None:
    r = client.post("/api/issues/nope/1/dispatch", json={"stage": "plan-exec"})
    assert r.status_code == 404


def test_post_dispatch_429_quota_exceeded(
    client: TestClient, setup: dict[str, Any]
) -> None:
    state.upsert_project(name="teach-me-eng-bot", path=str(setup["project"]), repo="me/x")
    for _ in range(30):
        state.inc_quota("teach-me-eng-bot")

    r = client.post(
        "/api/issues/teach-me-eng-bot/1/dispatch",
        json={"stage": "plan-exec"},
    )
    assert r.status_code == 429


def test_post_review_validates_action(client: TestClient) -> None:
    r = client.post(
        "/api/prs/teach-me-eng-bot/1/review", json={"action": "approve", "body": "ok"}
    )
    assert r.status_code == 200


def test_post_merge_calls_gh(client: TestClient, setup: dict[str, Any]) -> None:
    r = client.post(
        "/api/prs/teach-me-eng-bot/1/merge", json={"method": "squash"}
    )
    assert r.status_code == 200
    assert any("--squash" in c for c in setup["gh_runner"].calls)


# ---------- websocket ----------


def test_ws_live_streams_dispatcher_events(
    client: TestClient, setup: dict[str, Any]
) -> None:
    dispatcher = setup["dispatcher"]
    with client.websocket_connect("/ws/live") as ws:
        # WS handler subscribed; the new queue is the last subscriber.
        assert len(dispatcher._subscribers) == 1
        dispatcher._subscribers[0].put_nowait({"kind": "dispatch_started", "id": 7})
        msg = ws.receive_json()
        assert msg == {"kind": "dispatch_started", "id": 7}
    # On disconnect the WS handler unsubscribes
    assert dispatcher._subscribers == []


def test_ws_live_unsubscribe_idempotent(setup: dict[str, Any]) -> None:
    """Direct unit on the new Dispatcher.unsubscribe()."""
    dispatcher = setup["dispatcher"]
    q = dispatcher.subscribe()
    dispatcher.unsubscribe(q)
    dispatcher.unsubscribe(q)  # second call must not raise
    assert dispatcher._subscribers == []


# ---------- lifespan tick ----------


def test_lifespan_runs_one_tick_at_startup(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    """The 60s tick loop fires once at startup before sleeping."""
    project = _write_project(tmp_path / "proj")
    register(project)
    spawner = FakeSpawner()
    gh_runner = FakeGhRunner()
    dispatcher = Dispatcher(spawner=spawner, gh_runner=gh_runner)
    scheduler = Scheduler(dispatcher=dispatcher, gh_runner=gh_runner)

    app = create_app(
        scheduler=scheduler,
        dispatcher=dispatcher,
        gh_runner=gh_runner,
        tick_interval_s=3600.0,
    )
    with TestClient(app):
        # lifespan started → first tick should have called gh.list_issues at least once
        # We can't reliably block on the tick within sync TestClient — issue the smallest
        # delay by hitting an API endpoint and giving the loop a chance.
        pass
    # After the TestClient context exits, the tick task was cancelled cleanly.
    # Assert we saw at least one gh CLI invocation from the tick.
    assert any(
        c[1:4] == ["issue", "list", "--repo"] for c in gh_runner.calls
    )
