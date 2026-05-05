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


def test_tick_state_epic_dispatches_decompose(base_setup: Path) -> None:
    gh_runner = FakeGhRunner(list_issues_payload=[_issue_payload(1, "state:epic")])
    res = _aiorun(_make_scheduler(gh_runner=gh_runner).tick())
    assert res.winner is not None
    assert res.winner.stage == "epic-decompose"


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
    for _ in range(30):  # good.yaml's daily_dispatch_cap == 30
        state.inc_quota("teach-me-eng-bot")

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
