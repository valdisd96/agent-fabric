from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from fabric import state
from fabric.dispatcher import Dispatcher
from fabric.registry import register
from fabric.scheduler import Scheduler

FIXTURES = Path(__file__).parent / "fixtures"


# ---------- helpers ----------


def _aiorun(coro: Any) -> Any:
    return asyncio.run(coro)


def _write_project(root: Path, *, name: str = "teach-me-eng-bot",
                   repo: str = "valdisd96/teach-me-eng-bot") -> Path:
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
    """Maps `(verb, sub)` keys to canned JSON; falls back to empty success."""

    list_issues_payload: list[dict[str, Any]] = field(default_factory=list)
    issue_view_payload: dict[str, Any] | None = None
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, args: list[str], **kwargs: Any) -> _GhRunResult:
        self.calls.append(list(args))
        # args[0] is "gh"
        if args[1:4] == ["issue", "list", "--repo"]:
            return _GhRunResult(stdout=json.dumps(self.list_issues_payload))
        if args[1:3] == ["issue", "view"]:
            payload = self.issue_view_payload or {"comments": []}
            return _GhRunResult(stdout=json.dumps(payload))
        # fall-through: comment, edit, etc. — succeed silently
        return _GhRunResult(stdout="https://example/c/1\n")


def _issue_payload(
    number: int,
    state_label: str | None = "state:needs-planning",
    *,
    priority: str | None = None,
    type_: str | None = None,
    author: str = "valdisd96",
    created_at: str = "2026-05-01T10:00:00Z",
) -> dict[str, Any]:
    labels: list[dict[str, str]] = []
    if state_label:
        labels.append({"name": state_label})
    if priority:
        labels.append({"name": priority})
    if type_:
        labels.append({"name": type_})
    return {
        "number": number,
        "title": f"Issue {number}",
        "url": f"https://example/i/{number}",
        "createdAt": created_at,
        "labels": labels,
        "author": {"login": author} if author else None,
    }


# ---------- fixtures ----------


@pytest.fixture
def base_setup(isolated_state_db: Path, tmp_path: Path) -> Path:
    """One project registered, no issues yet."""
    p = _write_project(tmp_path / "proj")
    register(p)
    return p


def _make_scheduler(
    *,
    spawner: FakeSpawner | None = None,
    gh_runner: FakeGhRunner | None = None,
    now: datetime | None = None,
) -> Scheduler:
    spawner = spawner or FakeSpawner()
    gh_runner = gh_runner or FakeGhRunner()
    dispatcher = Dispatcher(spawner=spawner, gh_runner=gh_runner)
    return Scheduler(
        dispatcher=dispatcher,
        gh_runner=gh_runner,
        now=(lambda: now) if now else lambda: datetime.now(timezone.utc),
    )


# ---------- pause ----------


def test_tick_skips_when_global_paused(base_setup: Path) -> None:
    state.set_setting("paused", "1")
    state.set_setting("paused_reason", "vacation")

    res = _aiorun(_make_scheduler().tick())

    assert res.winner is None
    assert res.skipped_reason is not None
    assert "vacation" in res.skipped_reason


def test_tick_skips_when_touch_file_exists(base_setup: Path, isolated_fabric_home: Path) -> None:
    (isolated_fabric_home / "PAUSED").touch()

    res = _aiorun(_make_scheduler().tick())

    assert res.winner is None
    assert res.skipped_reason is not None
    assert "PAUSED" in res.skipped_reason


def test_tick_per_project_paused_skips_only_that_project(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    a = _write_project(tmp_path / "a", name="a", repo="me/a")
    b = _write_project(tmp_path / "b", name="b", repo="me/b")
    register(a)
    register(b)
    # Both projects ensured in state via dispatcher self-heal — do it manually here
    state.upsert_project(name="a", path=str(a), repo="me/a")
    state.upsert_project(name="b", path=str(b), repo="me/b")
    state.set_project_paused("a", True)

    gh_runner = FakeGhRunner(list_issues_payload=[_issue_payload(1)])
    scheduler = _make_scheduler(gh_runner=gh_runner)

    res = _aiorun(scheduler.tick())

    # only b polled → b's issue should be the winner
    assert res.polled_projects == 1
    assert res.winner is not None
    assert res.winner.project == "b"


# ---------- polling + filters ----------


def test_tick_unlabeled_issue_dispatches_qualify_stage(base_setup: Path) -> None:
    """Open issue with no `state:*` label → qualify-issue stage dispatches.
    The fabric uses an internal `state:unqualified` pseudo-label so the
    issue flows through the existing candidate/dispatch machinery."""
    gh_runner = FakeGhRunner(list_issues_payload=[_issue_payload(1, state_label=None)])
    res = _aiorun(_make_scheduler(gh_runner=gh_runner).tick())
    assert res.winner is not None
    assert res.winner.issue == 1
    assert res.winner.stage == "qualify-issue"
    # The DB row carries the pseudo-label so the next tick's transition
    # logic can detect when qualify-issue flips it to a real state.
    row = state.get_issue("teach-me-eng-bot", 1)
    assert row is not None
    assert row.state_label == "state:unqualified"


def test_tick_state_draft_is_ignored(base_setup: Path) -> None:
    """`state:draft` is a terminal-ignored label — never dispatched, like
    `state:done`."""
    gh_runner = FakeGhRunner(list_issues_payload=[_issue_payload(1, "state:draft")])
    res = _aiorun(_make_scheduler(gh_runner=gh_runner).tick())
    assert res.winner is None
    assert res.candidates == 0


def test_tick_state_needs_decompose_dispatches_epic_decompose(base_setup: Path) -> None:
    """`state:needs-decompose` (the post-rename epic entry state) routes to
    the `epic-decompose` stage."""
    gh_runner = FakeGhRunner(
        list_issues_payload=[_issue_payload(1, "state:needs-decompose")]
    )
    res = _aiorun(_make_scheduler(gh_runner=gh_runner).tick())
    assert res.winner is not None
    assert res.winner.stage == "epic-decompose"


def test_tick_state_in_progress_non_epic_dispatches_plan_exec(
    base_setup: Path,
) -> None:
    """`state:in-progress` is the resume target after a non-epic
    clarification reply (and the marker plan-exec sets at step 2). The
    scheduler must dispatch plan-exec on it so the issue doesn't park
    silently — the bug PR #57 fixed for qualify-issue, here for
    plan-exec / clarify-issue."""
    gh_runner = FakeGhRunner(
        list_issues_payload=[
            _issue_payload(1, "state:in-progress", type_="type:feature"),
        ]
    )
    res = _aiorun(_make_scheduler(gh_runner=gh_runner).tick())
    assert res.winner is not None
    assert res.winner.issue == 1
    assert res.winner.stage == "plan-exec"


def test_tick_state_in_progress_epic_dispatches_epic_decompose(
    base_setup: Path,
) -> None:
    """A `type:epic` issue at `state:in-progress` (epic-decompose crashed
    after step 1 set the label) must resume into `epic-decompose`, not
    `plan-exec` — the latter would try to implement the unscoped epic."""
    gh_runner = FakeGhRunner(
        list_issues_payload=[
            _issue_payload(1, "state:in-progress", type_="type:epic"),
        ]
    )
    res = _aiorun(_make_scheduler(gh_runner=gh_runner).tick())
    assert res.winner is not None
    assert res.winner.stage == "epic-decompose"


def test_tick_state_in_progress_no_type_defaults_to_plan_exec(
    base_setup: Path,
) -> None:
    """If the issue carries no `type:*` label (qualify-issue should have
    added one but didn't, or the label was hand-removed), default to
    plan-exec rather than skipping — silent parking is what we're
    fixing."""
    gh_runner = FakeGhRunner(
        list_issues_payload=[_issue_payload(1, "state:in-progress")]
    )
    res = _aiorun(_make_scheduler(gh_runner=gh_runner).tick())
    assert res.winner is not None
    assert res.winner.stage == "plan-exec"


def test_tick_polls_and_upserts_issues(base_setup: Path) -> None:
    gh_runner = FakeGhRunner(
        list_issues_payload=[
            _issue_payload(1, "state:needs-planning"),
            _issue_payload(2, "state:clarification-needed"),
        ]
    )
    _aiorun(_make_scheduler(gh_runner=gh_runner).tick())

    rows = state.list_issues("teach-me-eng-bot")
    assert {r.number for r in rows} == {1, 2}


def test_tick_filters_untrusted_authors(base_setup: Path) -> None:
    gh_runner = FakeGhRunner(
        list_issues_payload=[_issue_payload(1, author="randomdude")]
    )
    res = _aiorun(_make_scheduler(gh_runner=gh_runner).tick())
    # the issue is upserted but never enters the candidate pool
    assert res.candidates == 0


def test_tick_skips_non_actionable_states(base_setup: Path) -> None:
    """state:clarification-needed is a human gate — never dispatched."""
    gh_runner = FakeGhRunner(
        list_issues_payload=[_issue_payload(1, "state:clarification-needed")]
    )
    res = _aiorun(_make_scheduler(gh_runner=gh_runner).tick())
    assert res.candidates == 0


# ---------- D3 sort ----------


def test_tick_picks_in_review_over_needs_planning(base_setup: Path) -> None:
    gh_runner = FakeGhRunner(
        list_issues_payload=[
            _issue_payload(1, "state:needs-planning", created_at="2026-05-01T10:00:00Z"),
            _issue_payload(2, "state:in-review", created_at="2026-05-02T10:00:00Z"),
        ]
    )
    res = _aiorun(_make_scheduler(gh_runner=gh_runner).tick())
    assert res.winner is not None
    assert res.winner.issue == 2
    assert res.winner.stage == "review-pr"


def test_tick_picks_high_priority_within_tier(base_setup: Path) -> None:
    gh_runner = FakeGhRunner(
        list_issues_payload=[
            _issue_payload(1, "state:needs-planning", priority="priority:low"),
            _issue_payload(2, "state:needs-planning", priority="priority:high"),
        ]
    )
    res = _aiorun(_make_scheduler(gh_runner=gh_runner).tick())
    assert res.winner is not None
    assert res.winner.issue == 2


def test_tick_round_robin_uses_last_served_at(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    a = _write_project(tmp_path / "a", name="a", repo="me/a")
    b = _write_project(tmp_path / "b", name="b", repo="me/b")
    register(a)
    register(b)
    state.upsert_project(name="a", path=str(a), repo="me/a")
    state.upsert_project(name="b", path=str(b), repo="me/b")
    # a was served more recently → b should go first
    state.set_project_last_served("a", "2026-05-04T10:00:00+00:00")
    state.set_project_last_served("b", "2026-05-03T10:00:00+00:00")

    gh_runner = FakeGhRunner(
        list_issues_payload=[_issue_payload(1, created_at="2026-05-01T10:00:00Z")]
    )
    res = _aiorun(_make_scheduler(gh_runner=gh_runner).tick())
    assert res.winner is not None
    assert res.winner.project == "b"


def test_tick_created_at_breaks_final_tie(base_setup: Path) -> None:
    gh_runner = FakeGhRunner(
        list_issues_payload=[
            _issue_payload(2, "state:needs-planning", created_at="2026-05-02T10:00:00Z"),
            _issue_payload(1, "state:needs-planning", created_at="2026-05-01T10:00:00Z"),
        ]
    )
    res = _aiorun(_make_scheduler(gh_runner=gh_runner).tick())
    assert res.winner is not None
    assert res.winner.issue == 1  # earlier created_at wins


# ---------- dispatch + last_served ----------


def test_tick_dispatches_winner_and_updates_last_served(base_setup: Path) -> None:
    gh_runner = FakeGhRunner(list_issues_payload=[_issue_payload(1)])
    res = _aiorun(_make_scheduler(gh_runner=gh_runner).tick())

    assert res.winner is not None
    proj = state.get_project("teach-me-eng-bot")
    assert proj is not None
    assert proj.last_served_at is not None


def test_tick_quota_exceeded_returns_reason(base_setup: Path) -> None:
    state.upsert_project(name="teach-me-eng-bot", path=str(base_setup), repo="me/x")
    # good.yaml's dispatch_cap == 30 over a 5h rolling window
    from datetime import datetime, timedelta, timezone
    inside = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(
        timespec="seconds"
    )
    for _ in range(30):
        state.record_dispatch(
            project="teach-me-eng-bot", issue=1, stage="plan-exec",
            started_at=inside,
        )

    gh_runner = FakeGhRunner(list_issues_payload=[_issue_payload(1)])
    res = _aiorun(_make_scheduler(gh_runner=gh_runner).tick())

    assert res.winner is None
    assert res.skipped_reason is not None
    assert "quota" in res.skipped_reason


# ---------- cycle-cap / failure auto-block ----------


def test_tick_cycle_cap_auto_blocks(base_setup: Path) -> None:
    state.upsert_project(name="teach-me-eng-bot", path=str(base_setup), repo="me/x")
    state.upsert_issue(project="teach-me-eng-bot", number=1, state_label="state:needs-planning")
    state.set_cycle_count("teach-me-eng-bot", 1, 5)  # good.yaml cycle_limit == 5

    gh_runner = FakeGhRunner(list_issues_payload=[_issue_payload(1)])
    res = _aiorun(_make_scheduler(gh_runner=gh_runner).tick())

    assert res.candidates == 0
    blocked = state.get_issue("teach-me-eng-bot", 1)
    assert blocked is not None and blocked.state_label == "state:blocked"
    notifications = state.list_unacked_notifications()
    assert any(n.kind == "blocked" for n in notifications)


def test_tick_consecutive_failures_block(base_setup: Path) -> None:
    state.upsert_project(name="teach-me-eng-bot", path=str(base_setup), repo="me/x")
    # 3 consecutive failures, no successes → retry_count == 3 → block
    for _ in range(3):
        did = state.record_dispatch(project="teach-me-eng-bot", issue=1, stage="plan-exec")
        state.complete_dispatch(did, exit_code=1)

    gh_runner = FakeGhRunner(list_issues_payload=[_issue_payload(1)])
    res = _aiorun(_make_scheduler(gh_runner=gh_runner).tick())

    assert res.candidates == 0
    blocked = state.get_issue("teach-me-eng-bot", 1)
    assert blocked is not None and blocked.state_label == "state:blocked"
    notifications = state.list_unacked_notifications()
    assert any(n.kind == "dispatch-failed" for n in notifications)


# ---------- retry-backoff ----------


def test_tick_skips_within_backoff(base_setup: Path) -> None:
    state.upsert_project(name="teach-me-eng-bot", path=str(base_setup), repo="me/x")
    failed_at = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
    did = state.record_dispatch(
        project="teach-me-eng-bot", issue=1, stage="plan-exec",
        started_at=failed_at.isoformat(timespec="seconds"),
    )
    state.complete_dispatch(
        did, ended_at=failed_at.isoformat(timespec="seconds"), exit_code=1
    )

    # 30 seconds after failure < 60s backoff[0]
    now = failed_at + timedelta(seconds=30)
    gh_runner = FakeGhRunner(list_issues_payload=[_issue_payload(1)])
    res = _aiorun(_make_scheduler(gh_runner=gh_runner, now=now).tick())

    assert res.candidates == 0
    assert res.winner is None


def test_tick_dispatches_after_backoff_elapsed(base_setup: Path) -> None:
    state.upsert_project(name="teach-me-eng-bot", path=str(base_setup), repo="me/x")
    failed_at = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
    did = state.record_dispatch(
        project="teach-me-eng-bot", issue=1, stage="plan-exec",
        started_at=failed_at.isoformat(timespec="seconds"),
    )
    state.complete_dispatch(
        did, ended_at=failed_at.isoformat(timespec="seconds"), exit_code=1
    )

    # 90 seconds after failure > 60s backoff[0]
    now = failed_at + timedelta(seconds=90)
    gh_runner = FakeGhRunner(list_issues_payload=[_issue_payload(1)])
    res = _aiorun(_make_scheduler(gh_runner=gh_runner, now=now).tick())

    assert res.winner is not None
    assert res.winner.issue == 1


# ---------- closed-issue completion notification ----------


def test_tick_fires_completion_notification_when_tracked_issue_closes(
    base_setup: Path,
) -> None:
    """An issue we previously saw open (state:in-review) disappears from the
    open list and `gh issue view` confirms CLOSED — fire `issue-completed`."""
    # First tick: see the issue open with state:in-review.
    gh_runner = FakeGhRunner(
        list_issues_payload=[_issue_payload(7, "state:in-review")]
    )
    _aiorun(_make_scheduler(gh_runner=gh_runner).tick())
    assert state.get_issue("teach-me-eng-bot", 7) is not None

    # Second tick: open list no longer contains issue 7; gh issue view says CLOSED.
    gh_runner = FakeGhRunner(
        list_issues_payload=[],
        issue_view_payload={
            "number": 7, "title": "Issue 7", "labels": [],
            "author": {"login": "valdisd96"}, "url": "https://example/i/7",
            "createdAt": "2026-05-01T10:00:00Z", "body": "",
            "state": "CLOSED", "comments": [],
        },
    )
    _aiorun(_make_scheduler(gh_runner=gh_runner).tick())

    notifs = state.list_unacked_notifications()
    completed = [n for n in notifs if n.kind == "issue-completed"]
    assert len(completed) == 1
    assert completed[0].project == "teach-me-eng-bot"
    assert completed[0].issue == 7

    row = state.get_issue("teach-me-eng-bot", 7)
    assert row is not None
    assert row.state_label == "state:done"


def test_tick_does_not_renotify_already_done_issues(base_setup: Path) -> None:
    """An issue already marked state:done in the DB never gets re-fetched
    nor re-notified, even if the open-list keeps omitting it."""
    state.upsert_project(
        name="teach-me-eng-bot", path=str(base_setup),
        repo="valdisd96/teach-me-eng-bot",
    )
    state.upsert_issue(
        project="teach-me-eng-bot", number=9,
        state_label="state:done", title="old", url="u",
        author="valdisd96", created_at="2026-04-01T10:00:00Z",
    )
    gh_runner = FakeGhRunner(list_issues_payload=[])
    _aiorun(_make_scheduler(gh_runner=gh_runner).tick())

    # No issue-completed notif fired and no `gh issue view` call was made.
    assert all(
        n.kind != "issue-completed"
        for n in state.list_unacked_notifications()
    )
    assert not any(c[1:3] == ["issue", "view"] for c in gh_runner.calls)


def test_close_as_not_planned_marks_cancelled_not_done(
    base_setup: Path,
) -> None:
    """`gh issue close --reason "not planned"` (or the web UI's "Close as
    not planned") sets stateReason=NOT_PLANNED — that should land as
    `state:cancelled` + `issue-cancelled` notif, not `state:done` +
    `issue-completed`."""
    state.upsert_project(
        name="teach-me-eng-bot", path=str(base_setup),
        repo="valdisd96/teach-me-eng-bot",
    )
    state.upsert_issue(
        project="teach-me-eng-bot", number=12,
        state_label="state:needs-planning", title="oops, accidental",
        url="u", author="valdisd96", created_at="2026-05-01T10:00:00Z",
    )
    gh_runner = FakeGhRunner(
        list_issues_payload=[],
        issue_view_payload={
            "number": 12, "title": "oops, accidental", "labels": [],
            "author": {"login": "valdisd96"}, "url": "u",
            "createdAt": "2026-05-01T10:00:00Z", "body": "",
            "state": "CLOSED", "stateReason": "NOT_PLANNED", "comments": [],
        },
    )
    _aiorun(_make_scheduler(gh_runner=gh_runner).tick())

    notifs = state.list_unacked_notifications()
    cancelled = [n for n in notifs if n.kind == "issue-cancelled"]
    completed = [n for n in notifs if n.kind == "issue-completed"]
    assert len(cancelled) == 1
    assert cancelled[0].issue == 12
    assert completed == []

    row = state.get_issue("teach-me-eng-bot", 12)
    assert row is not None
    assert row.state_label == "state:cancelled"


def test_close_with_cancelled_label_marks_cancelled(base_setup: Path) -> None:
    """If the issue carries `state:cancelled` at close (regardless of
    stateReason), that's treated as cancellation too — covers users who
    flip the label first then close-as-completed."""
    state.upsert_project(
        name="teach-me-eng-bot", path=str(base_setup),
        repo="valdisd96/teach-me-eng-bot",
    )
    state.upsert_issue(
        project="teach-me-eng-bot", number=13,
        state_label="state:in-review", title="t", url="u",
        author="valdisd96", created_at="2026-05-01T10:00:00Z",
    )
    gh_runner = FakeGhRunner(
        list_issues_payload=[],
        issue_view_payload={
            "number": 13, "title": "t",
            "labels": [{"name": "state:cancelled"}],
            "author": {"login": "valdisd96"}, "url": "u",
            "createdAt": "2026-05-01T10:00:00Z", "body": "",
            "state": "CLOSED", "stateReason": "COMPLETED", "comments": [],
        },
    )
    _aiorun(_make_scheduler(gh_runner=gh_runner).tick())

    notifs = state.list_unacked_notifications()
    assert any(n.kind == "issue-cancelled" for n in notifs)
    assert all(n.kind != "issue-completed" for n in notifs)
    row = state.get_issue("teach-me-eng-bot", 13)
    assert row is not None and row.state_label == "state:cancelled"


def test_tick_does_not_renotify_already_cancelled_issues(
    base_setup: Path,
) -> None:
    """A row already at `state:cancelled` is terminal — subsequent ticks
    must not re-fetch it from gh nor re-fire the notification."""
    state.upsert_project(
        name="teach-me-eng-bot", path=str(base_setup),
        repo="valdisd96/teach-me-eng-bot",
    )
    state.upsert_issue(
        project="teach-me-eng-bot", number=14,
        state_label="state:cancelled", title="t", url="u",
        author="valdisd96", created_at="2026-04-01T10:00:00Z",
    )
    gh_runner = FakeGhRunner(list_issues_payload=[])
    _aiorun(_make_scheduler(gh_runner=gh_runner).tick())

    assert all(
        n.kind != "issue-cancelled"
        for n in state.list_unacked_notifications()
    )
    assert not any(c[1:3] == ["issue", "view"] for c in gh_runner.calls)


def test_open_issue_with_cancelled_label_is_not_dispatched(
    base_setup: Path,
) -> None:
    """`state:cancelled` on an OPEN issue must keep the scheduler from
    dispatching it. Lets a user park a misfiled issue without closing it
    on GitHub — the row stays in DB with the cancelled label."""
    gh_runner = FakeGhRunner(
        list_issues_payload=[_issue_payload(15, "state:cancelled")]
    )
    res = _aiorun(_make_scheduler(gh_runner=gh_runner).tick())

    assert res.winner is None
    row = state.get_issue("teach-me-eng-bot", 15)
    assert row is not None and row.state_label == "state:cancelled"


def test_tick_skips_notification_when_issue_still_open(
    base_setup: Path,
) -> None:
    """If gh issue view reports OPEN (e.g. transient open-list pagination
    miss), don't fire the completion notification."""
    state.upsert_project(
        name="teach-me-eng-bot", path=str(base_setup),
        repo="valdisd96/teach-me-eng-bot",
    )
    state.upsert_issue(
        project="teach-me-eng-bot", number=11,
        state_label="state:in-review", title="t", url="u",
        author="valdisd96", created_at="2026-05-01T10:00:00Z",
    )
    gh_runner = FakeGhRunner(
        list_issues_payload=[],
        issue_view_payload={
            "number": 11, "title": "t", "labels": [],
            "author": {"login": "valdisd96"}, "url": "u",
            "createdAt": "2026-05-01T10:00:00Z", "body": "",
            "state": "OPEN", "comments": [],
        },
    )
    _aiorun(_make_scheduler(gh_runner=gh_runner).tick())
    assert all(
        n.kind != "issue-completed"
        for n in state.list_unacked_notifications()
    )


# ---------- generic transition notifications ----------


def test_generic_state_changed_notification_fires_with_rich_body(
    base_setup: Path,
) -> None:
    """Any prev_state→new_state transition outside the dedicated kinds
    fires a `state-changed` notification with a multi-line body containing
    project, issue#, transition arrow, cycle count, priority, and actor.

    Untrusted author keeps the issue out of the candidate pool, so we can
    seed dispatch history by hand without the real scheduler dispatch path
    polluting it.
    """
    # Tick 1 (no `now` override): seed the prev state.
    just_now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
    _aiorun(_make_scheduler(
        gh_runner=FakeGhRunner(list_issues_payload=[
            _issue_payload(3, "state:tests-pending", priority="priority:high",
                           type_="type:feat", author="randomdude"),
        ]),
        now=just_now - timedelta(minutes=1),
    ).tick())

    # A completed plan-exec dispatch right before the next tick.
    did = state.record_dispatch(
        project="teach-me-eng-bot", issue=3, stage="plan-exec",
        started_at=(just_now - timedelta(seconds=30)).isoformat(timespec="seconds"),
    )
    state.complete_dispatch(
        did,
        ended_at=just_now.isoformat(timespec="seconds"),
        exit_code=0,
    )

    # Tick 2: same issue now at state:in-review.
    _aiorun(_make_scheduler(
        gh_runner=FakeGhRunner(list_issues_payload=[
            _issue_payload(3, "state:in-review", priority="priority:high",
                           type_="type:feat", author="randomdude"),
        ]),
        now=just_now + timedelta(seconds=10),
    ).tick())

    notifs = [n for n in state.list_unacked_notifications() if n.kind == "state-changed"]
    assert len(notifs) == 1
    body = notifs[0].body or ""
    assert "teach-me-eng-bot#3" in body
    assert "state:tests-pending → state:in-review" in body
    assert "priority:high" in body
    assert "type:feat" in body
    assert "by plan-exec" in body
    assert "cycle 0/" in body  # cycle_count starts at 0; format is N/limit


def test_state_change_attributed_manual_when_no_recent_dispatch(
    base_setup: Path,
) -> None:
    """No recent successful dispatch → attribute the change to `manual`."""
    _aiorun(_make_scheduler(
        gh_runner=FakeGhRunner(list_issues_payload=[
            _issue_payload(4, "state:needs-planning", author="randomdude"),
        ]),
    ).tick())

    _aiorun(_make_scheduler(
        gh_runner=FakeGhRunner(list_issues_payload=[
            _issue_payload(4, "state:tests-pending", author="randomdude"),
        ]),
    ).tick())

    notifs = [n for n in state.list_unacked_notifications() if n.kind == "state-changed"]
    assert len(notifs) == 1
    assert "by manual" in (notifs[0].body or "")


def test_clarification_kind_still_used_with_rich_body(
    base_setup: Path,
) -> None:
    """Transitions to states with dedicated kinds (clarification,
    decompose-approval) keep their kind, but now ship the rich body too."""
    _aiorun(_make_scheduler(
        gh_runner=FakeGhRunner(list_issues_payload=[
            _issue_payload(5, "state:in-progress", author="randomdude"),
        ]),
    ).tick())
    _aiorun(_make_scheduler(
        gh_runner=FakeGhRunner(list_issues_payload=[
            _issue_payload(5, "state:clarification-needed", author="randomdude"),
        ]),
    ).tick())

    notifs = state.list_unacked_notifications()
    clar = [n for n in notifs if n.kind == "clarification"]
    assert len(clar) == 1
    body = clar[0].body or ""
    assert "state:in-progress → state:clarification-needed" in body
    # And no duplicate state-changed kind:
    assert not any(n.kind == "state-changed" for n in notifs)


def test_blocked_state_does_not_double_fire(base_setup: Path) -> None:
    """When `_block_issue` flips a label to state:blocked, the next tick
    sees prev_state→state:blocked but must NOT fire a generic
    state-changed on top of the dedicated `blocked` kind."""
    # Manually seed an issue at state:in-review with cycle count over the cap.
    state.upsert_project(
        name="teach-me-eng-bot", path=str(base_setup),
        repo="valdisd96/teach-me-eng-bot",
    )
    state.upsert_issue(
        project="teach-me-eng-bot", number=6,
        state_label="state:in-review", title="t", url="u",
        author="valdisd96", created_at="2026-05-01T10:00:00Z",
    )
    state.set_cycle_count("teach-me-eng-bot", 6, 999)

    # Tick: gh still reports it as state:in-review; the cycle-cap path
    # in _block_issue flips it.
    _aiorun(_make_scheduler(
        gh_runner=FakeGhRunner(list_issues_payload=[
            _issue_payload(6, "state:in-review", author="valdisd96"),
        ]),
    ).tick())

    notifs = state.list_unacked_notifications()
    assert any(n.kind == "blocked" for n in notifs)
    assert not any(n.kind == "state-changed" for n in notifs)


# ---------- epic coordinator (B3) ----------


@dataclass
class _CoordinatorRunner:
    """Per-issue `gh issue view` payloads + sequential `gh issue list`
    payloads. The plain FakeGhRunner returns one canned `issue_view`
    payload for every call, which collides with the coordinator path
    (which needs distinct payloads for the closed child and the parent).
    """

    issue_view_by_number: dict[int, dict[str, Any]] = field(default_factory=dict)
    list_issues_payloads: list[list[dict[str, Any]]] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)
    _list_idx: int = 0

    def __call__(self, args: list[str], **kwargs: Any) -> _GhRunResult:
        self.calls.append(list(args))
        if args[1:4] == ["issue", "list", "--repo"]:
            payloads = self.list_issues_payloads or [[]]
            idx = min(self._list_idx, len(payloads) - 1)
            self._list_idx += 1
            return _GhRunResult(stdout=json.dumps(payloads[idx]))
        if args[1:3] == ["issue", "view"]:
            try:
                num = int(args[3])
            except (IndexError, ValueError):
                num = 0
            payload = self.issue_view_by_number.get(num, {"comments": []})
            return _GhRunResult(stdout=json.dumps(payload))
        return _GhRunResult(stdout="https://example/c/1\n")


def _epic_parent_view(
    number: int, *, state: str = "OPEN", labels: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Epic {number}",
        "labels": [{"name": n} for n in (labels or ["type:epic", "state:tracking"])],
        "author": {"login": "valdisd96"},
        "url": f"https://example/i/{number}",
        "createdAt": "2026-04-29T10:00:00Z",
        "body": "Epic body",
        "state": state,
        "comments": [],
    }


def _closed_child_view(
    number: int, parent: int, *, body: str | None = None,
) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Child {number}",
        "labels": [],
        "author": {"login": "valdisd96"},
        "url": f"https://example/i/{number}",
        "createdAt": "2026-05-01T10:00:00Z",
        "body": body if body is not None else f"Part of epic.\nRefs #{parent}",
        "state": "CLOSED",
        "comments": [],
    }


def _seed_tracked_issue(
    project: str, number: int, state_label: str = "state:in-review"
) -> None:
    state.upsert_project(name=project, path="/p", repo="me/x")
    state.upsert_issue(
        project=project, number=number, state_label=state_label,
        title=f"Issue {number}", url=f"u{number}",
        author="valdisd96", created_at="2026-05-01T10:00:00Z",
    )


def test_parse_epic_parent_extracts_number() -> None:
    """The Refs-line parser is the contract between epic-decompose's
    file-children template and the scheduler's coordinator. Be loud about
    it being load-bearing."""
    from fabric.scheduler import _parse_epic_parent

    assert _parse_epic_parent("Refs #42") == 42
    assert _parse_epic_parent("Part of epic #5.\nRefs #5\n") == 5
    assert _parse_epic_parent("REFS #7") == 7  # case-insensitive
    assert _parse_epic_parent("see #99") is None  # bare # ref doesn't count
    assert _parse_epic_parent("") is None
    assert _parse_epic_parent(None) is None


def test_advance_oldest_draft_sibling_when_child_closes(
    base_setup: Path,
) -> None:
    """When a child of a `type:epic` parent closes and another sibling is
    in `state:draft`, flip the oldest draft to `state:needs-planning` and
    fire `epic-advanced`."""
    _seed_tracked_issue("teach-me-eng-bot", 2, "state:in-review")

    runner = _CoordinatorRunner(
        list_issues_payloads=[
            [],  # main poll: child #2 is no longer open
            [   # sibling search by Refs #5
                _issue_payload(3, "state:draft", created_at="2026-05-02T10:00:00Z"),
                _issue_payload(4, "state:draft", created_at="2026-05-03T10:00:00Z"),
            ],
        ],
        issue_view_by_number={
            2: _closed_child_view(2, parent=5),
            5: _epic_parent_view(5),
        },
    )
    _aiorun(_make_scheduler(gh_runner=runner).tick())

    # Oldest draft (#3) was flipped: --remove-label state:draft + --add-label state:needs-planning.
    edit_three = [c for c in runner.calls if c[1:3] == ["issue", "edit"] and "3" in c]
    assert any("--remove-label" in c and "state:draft" in c for c in edit_three)
    assert any(
        "--add-label" in c and "state:needs-planning" in c for c in edit_three
    )
    # And NOT on #4 (the newer draft).
    edit_four = [c for c in runner.calls if c[1:3] == ["issue", "edit"] and "4" in c]
    assert not edit_four

    # Notification fired on the parent.
    notifs = state.list_unacked_notifications()
    advanced = [n for n in notifs if n.kind == "epic-advanced"]
    assert len(advanced) == 1
    assert advanced[0].issue == 5
    assert "#3" in (advanced[0].body or "")


def test_parent_auto_closes_when_no_drafts_left(base_setup: Path) -> None:
    """Last child of an epic closes → no open siblings → fabric closes the
    parent and fires `epic-completed`."""
    _seed_tracked_issue("teach-me-eng-bot", 2, "state:in-review")

    runner = _CoordinatorRunner(
        list_issues_payloads=[
            [],  # main poll
            [],  # sibling search returns nothing → close parent
        ],
        issue_view_by_number={
            2: _closed_child_view(2, parent=5),
            5: _epic_parent_view(5),
        },
    )
    _aiorun(_make_scheduler(gh_runner=runner).tick())

    close_calls = [c for c in runner.calls if c[1:3] == ["issue", "close"]]
    assert len(close_calls) == 1
    assert "5" in close_calls[0]

    notifs = state.list_unacked_notifications()
    assert any(
        n.kind == "epic-completed" and n.issue == 5 for n in notifs
    )


def test_no_advance_when_another_sibling_is_active(base_setup: Path) -> None:
    """If another sibling is already non-draft (in-progress, in-review,
    needs-planning, etc.), don't advance — its closure will trigger the
    next advance later."""
    _seed_tracked_issue("teach-me-eng-bot", 2, "state:in-review")

    runner = _CoordinatorRunner(
        list_issues_payloads=[
            [],
            [
                _issue_payload(3, "state:in-progress"),  # active — leave alone
                _issue_payload(4, "state:draft"),
            ],
        ],
        issue_view_by_number={
            2: _closed_child_view(2, parent=5),
            5: _epic_parent_view(5),
        },
    )
    _aiorun(_make_scheduler(gh_runner=runner).tick())

    # No advance edit on #4, and parent stays open (no close call).
    edit_four = [
        c for c in runner.calls
        if c[1:3] == ["issue", "edit"] and "4" in c and "--add-label" in c
    ]
    assert not edit_four
    assert not any(c[1:3] == ["issue", "close"] for c in runner.calls)
    assert all(
        n.kind not in ("epic-advanced", "epic-completed")
        for n in state.list_unacked_notifications()
    )


def test_no_advance_when_body_lacks_refs(base_setup: Path) -> None:
    """A closed issue with no `Refs #N` in the body is just a regular
    closure — no coordinator action."""
    _seed_tracked_issue("teach-me-eng-bot", 2, "state:in-review")

    runner = _CoordinatorRunner(
        issue_view_by_number={
            2: _closed_child_view(2, parent=5, body="just a regular issue"),
        },
    )
    _aiorun(_make_scheduler(gh_runner=runner).tick())

    # No parent fetch (no `gh issue view <parent>` call).
    parent_views = [
        c for c in runner.calls
        if c[1:3] == ["issue", "view"] and len(c) > 3 and c[3] == "5"
    ]
    assert not parent_views
    assert all(
        n.kind not in ("epic-advanced", "epic-completed")
        for n in state.list_unacked_notifications()
    )


# ---------- agent-comment in clarification body ----------


def _issue_view_with_comments(
    number: int,
    state_label: str,
    comments: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Issue {number}",
        "url": f"https://example/i/{number}",
        "createdAt": "2026-05-01T10:00:00Z",
        "labels": [{"name": state_label}],
        "author": {"login": "valdisd96"},
        "body": "",
        "state": "OPEN",
        "comments": comments,
    }


def test_clarification_body_embeds_latest_agent_comment(
    base_setup: Path,
) -> None:
    """When an issue transitions to `state:clarification-needed`, the
    notification body should include the agent's most recent
    `<!-- agent-* -->` comment so the human can read the question in TG."""
    _aiorun(_make_scheduler(
        gh_runner=FakeGhRunner(list_issues_payload=[
            _issue_payload(8, "state:in-progress", author="randomdude"),
        ]),
    ).tick())

    agent_question = (
        "<!-- agent-decompose-q v1 -->\n"
        "**Decomposition question (round 1 of up to 8)**\n\n"
        "Should imports be one-shot or streaming?"
    )
    issue_view = _issue_view_with_comments(
        8, "state:clarification-needed",
        comments=[
            {"body": "looks good", "author": {"login": "valdisd96"}, "url": "u"},
            {"body": agent_question, "author": {"login": "fabric-bot"}, "url": "u"},
        ],
    )
    _aiorun(_make_scheduler(
        gh_runner=FakeGhRunner(
            list_issues_payload=[
                _issue_payload(8, "state:clarification-needed", author="randomdude"),
            ],
            issue_view_payload=issue_view,
        ),
    ).tick())

    clar = [n for n in state.list_unacked_notifications() if n.kind == "clarification"]
    assert len(clar) == 1
    body = clar[0].body or ""
    # Header still has the transition info.
    assert "state:in-progress → state:clarification-needed" in body
    # And the agent's question text is embedded.
    assert "agent-decompose-q v1" in body
    assert "Should imports be one-shot or streaming?" in body


def test_decompose_approval_body_embeds_latest_agent_comment(
    base_setup: Path,
) -> None:
    """Same for `state:awaiting-decompose-approval`: embed the proposal."""
    _aiorun(_make_scheduler(
        gh_runner=FakeGhRunner(list_issues_payload=[
            _issue_payload(9, "state:in-progress", type_="type:epic",
                           author="randomdude"),
        ]),
    ).tick())

    proposal = (
        "<!-- agent-decompose v1 -->\n"
        "## Proposed decomposition\n\n"
        "### Child 1 — add ingestion CLI"
    )
    issue_view = _issue_view_with_comments(
        9, "state:awaiting-decompose-approval",
        comments=[
            {"body": proposal, "author": {"login": "fabric-bot"}, "url": "u"},
        ],
    )
    _aiorun(_make_scheduler(
        gh_runner=FakeGhRunner(
            list_issues_payload=[
                _issue_payload(9, "state:awaiting-decompose-approval",
                               type_="type:epic", author="randomdude"),
            ],
            issue_view_payload=issue_view,
        ),
    ).tick())

    da = [n for n in state.list_unacked_notifications() if n.kind == "decompose-approval"]
    assert len(da) == 1
    body = da[0].body or ""
    assert "Proposed decomposition" in body
    assert "Child 1 — add ingestion CLI" in body


def test_clarification_body_falls_back_when_no_agent_comment(
    base_setup: Path,
) -> None:
    """If no agent-marker comment exists yet (e.g. the agent just flipped
    the label without commenting), the body still ships transition
    metadata — no crash, no spurious agent text."""
    _aiorun(_make_scheduler(
        gh_runner=FakeGhRunner(list_issues_payload=[
            _issue_payload(10, "state:in-progress", author="randomdude"),
        ]),
    ).tick())

    issue_view = _issue_view_with_comments(
        10, "state:clarification-needed",
        comments=[
            {"body": "human note", "author": {"login": "valdisd96"}, "url": "u"},
        ],
    )
    _aiorun(_make_scheduler(
        gh_runner=FakeGhRunner(
            list_issues_payload=[
                _issue_payload(10, "state:clarification-needed", author="randomdude"),
            ],
            issue_view_payload=issue_view,
        ),
    ).tick())

    clar = [n for n in state.list_unacked_notifications() if n.kind == "clarification"]
    assert len(clar) == 1
    body = clar[0].body or ""
    assert "state:in-progress → state:clarification-needed" in body
    assert "<!-- agent-" not in body


# ---------- cycle-counter on rework entry ----------


def test_transition_to_needs_rework_bumps_cycle_count(
    base_setup: Path,
) -> None:
    """`Dispatcher.bump_cycle` exists but had no call site — cycle_count
    stayed at 0 forever, the cap was dead, and runaway rework loops
    couldn't auto-block. The scheduler must now bump on every observed
    transition into `state:needs-rework`."""
    state.upsert_project(
        name="teach-me-eng-bot", path=str(base_setup),
        repo="valdisd96/teach-me-eng-bot",
    )
    state.upsert_issue(
        project="teach-me-eng-bot", number=1,
        state_label="state:in-review", title="t", url="u",
        author="valdisd96", created_at="2026-05-01T10:00:00Z",
    )

    gh_runner = FakeGhRunner(
        list_issues_payload=[_issue_payload(1, "state:needs-rework")],
    )
    _aiorun(_make_scheduler(gh_runner=gh_runner).tick())

    row = state.get_issue("teach-me-eng-bot", 1)
    assert row is not None
    assert row.cycle_count == 1


def test_repeated_observation_at_needs_rework_does_not_double_bump(
    base_setup: Path,
) -> None:
    """If two consecutive ticks both see the issue at `state:needs-rework`
    (no transition), cycle_count must NOT increment again. The bump fires
    only on the entry transition."""
    state.upsert_project(
        name="teach-me-eng-bot", path=str(base_setup),
        repo="valdisd96/teach-me-eng-bot",
    )
    state.upsert_issue(
        project="teach-me-eng-bot", number=1,
        state_label="state:in-review", title="t", url="u",
        author="valdisd96", created_at="2026-05-01T10:00:00Z",
    )
    # Tick 1: in-review → needs-rework. Bump fires.
    _aiorun(_make_scheduler(
        gh_runner=FakeGhRunner(
            list_issues_payload=[_issue_payload(1, "state:needs-rework")],
        ),
    ).tick())
    # Tick 2: needs-rework → needs-rework (no transition). No bump.
    _aiorun(_make_scheduler(
        gh_runner=FakeGhRunner(
            list_issues_payload=[_issue_payload(1, "state:needs-rework")],
        ),
    ).tick())

    row = state.get_issue("teach-me-eng-bot", 1)
    assert row is not None
    assert row.cycle_count == 1


def test_first_sight_at_needs_rework_does_not_bump(base_setup: Path) -> None:
    """If the issue is observed for the very first time at
    `state:needs-rework` (e.g. fabric just installed), don't bump —
    we don't know if this is cycle 1 or cycle 5. `bump_cycle` itself
    recovers the right counter from the GH HTML comment when we DO
    have a transition to bump on, so this caution is harmless."""
    gh_runner = FakeGhRunner(
        list_issues_payload=[_issue_payload(1, "state:needs-rework")],
    )
    _aiorun(_make_scheduler(gh_runner=gh_runner).tick())

    row = state.get_issue("teach-me-eng-bot", 1)
    assert row is not None
    assert row.cycle_count == 0


def test_no_advance_when_parent_lacks_type_epic(base_setup: Path) -> None:
    """A closed issue Refs'ing a non-epic parent (e.g. a regular bug
    cross-referencing another bug) is a coincidence — don't act."""
    _seed_tracked_issue("teach-me-eng-bot", 2, "state:in-review")

    runner = _CoordinatorRunner(
        list_issues_payloads=[[]],  # main poll only — no sibling search expected
        issue_view_by_number={
            2: _closed_child_view(2, parent=5),
            5: _epic_parent_view(5, labels=["type:bug"]),  # not type:epic
        },
    )
    _aiorun(_make_scheduler(gh_runner=runner).tick())

    # No sibling search was issued (we bailed out at the type:epic check).
    sibling_searches = [
        c for c in runner.calls
        if c[1:4] == ["issue", "list", "--repo"] and "--search" in c
    ]
    assert not sibling_searches
    assert not any(c[1:3] == ["issue", "close"] for c in runner.calls)
