from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from fabric import state
from fabric.dispatcher import Dispatcher
from fabric.registry import register
from fabric.scheduler import Scheduler
from fabric.server import create_app
from fabric.telegram_bot import (
    CallbackPayload,
    TelegramBot,
    decode_callback,
    encode_callback,
    handle_callback,
    handle_reply_to_notification,
    notification_buttons,
    render_issue,
    render_notification_text,
    render_projects,
    render_queue,
    render_status,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---------- helpers ----------


def _aiorun(coro: Any) -> Any:
    return asyncio.run(coro)


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


@pytest.fixture
def app_setup(isolated_state_db: Path, tmp_path: Path) -> dict[str, Any]:
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
def rest(app_setup: dict[str, Any]) -> httpx.AsyncClient:
    """An in-process httpx client routed through the FastAPI app via ASGI."""
    transport = httpx.ASGITransport(app=app_setup["app"])
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------- callback_data ----------


def test_encode_decode_roundtrip() -> None:
    encoded = encode_callback("retry", "teach-me-eng-bot", 42)
    decoded = decode_callback(encoded)
    assert decoded == CallbackPayload(action="retry", project="teach-me-eng-bot", number=42)


def test_encode_rejects_colon_in_action() -> None:
    with pytest.raises(ValueError):
        encode_callback("retry:malicious", "p", 1)


def test_decode_returns_none_for_garbage() -> None:
    assert decode_callback("not-our-format") is None
    assert decode_callback("af:retry:p") is None  # missing number
    assert decode_callback("af:retry:p:notanint") is None


def test_decode_rejects_wrong_namespace() -> None:
    assert decode_callback("other:retry:p:1") is None


# ---------- formatters ----------


def test_render_queue_empty(rest: httpx.AsyncClient) -> None:
    text = _aiorun(render_queue(rest))
    assert text == "Queue: empty."


def test_render_queue_lists_issues(
    isolated_state_db: Path, rest: httpx.AsyncClient
) -> None:
    state.upsert_project(name="teach-me-eng-bot", path="/p", repo="me/x")
    state.upsert_issue(
        project="teach-me-eng-bot", number=1, state_label="state:needs-planning",
        priority_label="priority:high", title="Hi",
    )

    text = _aiorun(render_queue(rest))
    assert "teach-me-eng-bot#1" in text
    assert "state:needs-planning" in text
    assert "priority:high" in text


def test_render_status_running(rest: httpx.AsyncClient) -> None:
    text = _aiorun(render_status(rest))
    assert "running" in text


def test_render_status_paused_includes_reason(
    isolated_state_db: Path, rest: httpx.AsyncClient
) -> None:
    state.set_setting("paused", "1")
    state.set_setting("paused_reason", "lunch")
    text = _aiorun(render_status(rest))
    assert "paused" in text
    assert "lunch" in text


def test_render_projects_empty(rest: httpx.AsyncClient) -> None:
    text = _aiorun(render_projects(rest))
    assert "no registered" in text


def test_render_issue_404(rest: httpx.AsyncClient) -> None:
    text = _aiorun(render_issue(rest, "teach-me-eng-bot", 999))
    assert "not found" in text


# ---------- notification rendering ----------


def test_render_notification_for_each_kind() -> None:
    n_blocked = state.NotificationRow(
        id=1, kind="blocked", project="p", issue=2,
        created_at="t", delivered_to=None, acknowledged_at=None,
    )
    text = render_notification_text(n_blocked)
    assert "Auto-blocked" in text
    assert "p#2" in text


def test_render_issue_completed_notification() -> None:
    n = state.NotificationRow(
        id=1, kind="issue-completed", project="p", issue=7,
        created_at="t", delivered_to=None, acknowledged_at=None,
    )
    text = render_notification_text(n)
    assert "Issue completed" in text
    assert "p#7" in text


def test_render_epic_advanced_notification() -> None:
    """Fired by the B3 coordinator when the next held child gets advanced
    to `state:needs-planning` after its predecessor closes."""
    body = "p#5 — epic advanced\n#2 closed → #3 now state:needs-planning"
    n = state.NotificationRow(
        id=1, kind="epic-advanced", project="p", issue=5,
        created_at="t", delivered_to=None, acknowledged_at=None, body=body,
    )
    text = render_notification_text(n)
    assert "Epic advanced" in text
    assert "#3 now state:needs-planning" in text


def test_render_epic_completed_notification() -> None:
    """Fired by the B3 coordinator when the parent auto-closes after the
    last child closes."""
    n = state.NotificationRow(
        id=1, kind="epic-completed", project="p", issue=5,
        created_at="t", delivered_to=None, acknowledged_at=None,
        body="p#5 — epic completed (all children closed)",
    )
    text = render_notification_text(n)
    assert "Epic completed" in text
    assert "all children closed" in text


def test_render_state_changed_uses_body_when_present() -> None:
    body = (
        "teach-me-eng-bot#7 — Rewrite README\n"
        "state:in-review → state:tests-pending\n"
        "cycle 1/8 · priority:low · type:docs\n"
        "by review-pr\n"
        "https://example/i/7"
    )
    n = state.NotificationRow(
        id=1, kind="state-changed", project="teach-me-eng-bot", issue=7,
        created_at="t", delivered_to=None, acknowledged_at=None, body=body,
    )
    text = render_notification_text(n)
    assert "🔄 State changed" in text
    assert "state:in-review → state:tests-pending" in text
    assert "by review-pr" in text
    assert "cycle 1/8" in text


def test_render_falls_back_when_body_absent() -> None:
    n = state.NotificationRow(
        id=1, kind="clarification", project="p", issue=2,
        created_at="t", delivered_to=None, acknowledged_at=None,
    )
    text = render_notification_text(n)
    assert "Clarification needed" in text
    assert "p#2" in text


def test_notification_buttons_dispatch_failed_has_retry_and_block() -> None:
    n = state.NotificationRow(
        id=1, kind="dispatch-failed", project="p", issue=2,
        created_at="t", delivered_to=None, acknowledged_at=None,
    )
    rows = notification_buttons(n)
    flat = [b for row in rows for b in row]
    actions = {decode_callback(b["callback_data"]).action for b in flat if b.get("callback_data")}  # type: ignore[union-attr]
    assert "retry" in actions
    assert "block" in actions


# ---------- callback dispatch ----------


def test_handle_callback_retry_invokes_dispatch(
    isolated_state_db: Path, app_setup: dict[str, Any], rest: httpx.AsyncClient
) -> None:
    payload = CallbackPayload(action="retry", project="teach-me-eng-bot", number=1)
    result = _aiorun(handle_callback(rest, payload))
    assert result == "queued"
    # The fake spawner ran claude -p
    assert app_setup["spawner"].calls


def test_handle_callback_block_calls_label_endpoint(
    app_setup: dict[str, Any], rest: httpx.AsyncClient
) -> None:
    payload = CallbackPayload(action="block", project="teach-me-eng-bot", number=1)
    result = _aiorun(handle_callback(rest, payload))
    assert result == "blocked"
    assert any("--add-label" in c for c in app_setup["gh_runner"].calls)


def test_handle_callback_unknown_action_returns_explanation(
    rest: httpx.AsyncClient,
) -> None:
    result = _aiorun(handle_callback(rest, CallbackPayload(action="nuke", project="p", number=1)))
    assert "unknown action" in result


# ---------- reply-to-message ----------


def test_handle_reply_to_notification_posts_comment(
    isolated_state_db: Path, app_setup: dict[str, Any], rest: httpx.AsyncClient
) -> None:
    state.upsert_project(name="teach-me-eng-bot", path="/p", repo="me/x")
    nid = state.add_notification(kind="clarification", project="teach-me-eng-bot", issue=1)
    state.set_notification_tg_message(nid, chat_id=42, message_id=99)
    notif = state.find_notification_by_tg_message(42, 99)
    assert notif is not None

    ok = _aiorun(handle_reply_to_notification(rest, notif, "the answer is 42"))
    assert ok is True
    assert any(c[1:3] == ["issue", "comment"] for c in app_setup["gh_runner"].calls)


def test_handle_reply_flips_clarification_to_in_progress_for_non_epic(
    isolated_state_db: Path, app_setup: dict[str, Any], rest: httpx.AsyncClient
) -> None:
    """Replying to a `clarification` notif on a non-epic issue flips the
    state back to `state:in-progress` so the next tick re-dispatches
    plan-exec / clarify-issue."""
    state.upsert_project(name="teach-me-eng-bot", path="/p", repo="me/x")
    state.upsert_issue(
        project="teach-me-eng-bot", number=1,
        state_label="state:clarification-needed",
        type_label="type:bug",
    )
    nid = state.add_notification(
        kind="clarification", project="teach-me-eng-bot", issue=1,
    )
    notif = state.NotificationRow(
        id=nid, kind="clarification", project="teach-me-eng-bot", issue=1,
        created_at="t", delivered_to=None, acknowledged_at=None,
    )
    _aiorun(handle_reply_to_notification(rest, notif, "answer"))

    edit_calls = [
        c for c in app_setup["gh_runner"].calls if c[1:3] == ["issue", "edit"]
    ]
    assert any("--add-label" in c and "state:in-progress" in c for c in edit_calls)
    assert any(
        "--remove-label" in c and "state:clarification-needed" in c
        for c in edit_calls
    )


def test_handle_reply_flips_clarification_to_needs_decompose_for_epic(
    isolated_state_db: Path, app_setup: dict[str, Any], rest: httpx.AsyncClient
) -> None:
    """On a `type:epic` issue, the resume state is `state:needs-decompose`
    so the next tick re-dispatches `epic-decompose`, not plan-exec."""
    state.upsert_project(name="teach-me-eng-bot", path="/p", repo="me/x")
    state.upsert_issue(
        project="teach-me-eng-bot", number=2,
        state_label="state:clarification-needed",
        type_label="type:epic",
    )
    notif = state.NotificationRow(
        id=1, kind="clarification", project="teach-me-eng-bot", issue=2,
        created_at="t", delivered_to=None, acknowledged_at=None,
    )
    _aiorun(handle_reply_to_notification(rest, notif, "the answer"))

    edit_calls = [
        c for c in app_setup["gh_runner"].calls if c[1:3] == ["issue", "edit"]
    ]
    assert any("--add-label" in c and "state:needs-decompose" in c for c in edit_calls)


def test_handle_reply_flips_decompose_approval_to_needs_decompose(
    isolated_state_db: Path, app_setup: dict[str, Any], rest: httpx.AsyncClient
) -> None:
    """`/decompose-ok` (or revision feedback) lands as a comment + flip
    back to `state:needs-decompose` so the next tick files children."""
    state.upsert_project(name="teach-me-eng-bot", path="/p", repo="me/x")
    state.upsert_issue(
        project="teach-me-eng-bot", number=3,
        state_label="state:awaiting-decompose-approval",
        type_label="type:epic",
    )
    notif = state.NotificationRow(
        id=1, kind="decompose-approval", project="teach-me-eng-bot", issue=3,
        created_at="t", delivered_to=None, acknowledged_at=None,
    )
    _aiorun(handle_reply_to_notification(rest, notif, "/decompose-ok"))

    edit_calls = [
        c for c in app_setup["gh_runner"].calls if c[1:3] == ["issue", "edit"]
    ]
    assert any("--add-label" in c and "state:needs-decompose" in c for c in edit_calls)


def test_handle_reply_no_flip_for_informational_kinds(
    isolated_state_db: Path, app_setup: dict[str, Any], rest: httpx.AsyncClient
) -> None:
    """Replies to non-Q&A notifications (blocked, issue-completed, etc.)
    post the comment but never flip labels — those flows have no resume
    contract to honor."""
    state.upsert_project(name="teach-me-eng-bot", path="/p", repo="me/x")
    state.upsert_issue(
        project="teach-me-eng-bot", number=4,
        state_label="state:blocked",
    )
    notif = state.NotificationRow(
        id=1, kind="blocked", project="teach-me-eng-bot", issue=4,
        created_at="t", delivered_to=None, acknowledged_at=None,
    )
    _aiorun(handle_reply_to_notification(rest, notif, "thoughts"))

    edit_calls = [
        c for c in app_setup["gh_runner"].calls if c[1:3] == ["issue", "edit"]
    ]
    assert not edit_calls


def test_render_truncates_oversize_notification() -> None:
    """A `decompose-approval` proposal can run thousands of chars; TG
    caps at 4096. Verify we trim to the budget and append a marker."""
    huge_body = "x" * 5000
    n = state.NotificationRow(
        id=1, kind="decompose-approval", project="p", issue=1,
        created_at="t", delivered_to=None, acknowledged_at=None,
        body=huge_body,
    )
    text = render_notification_text(n)
    assert len(text) <= 4000
    assert "truncated" in text
    assert "Decompose approval" in text  # header still present


# ---------- TelegramBot handler methods (with mocked context.bot) ----------


def _mock_context() -> Any:
    bot = AsyncMock()
    return SimpleNamespace(bot=bot, args=[])


def _make_bot(rest: httpx.AsyncClient) -> TelegramBot:
    return TelegramBot(
        token="test-token",
        chat_id=42,
        rest_base_url="http://unused",
        rest_client=rest,
    )


def test_cmd_queue_sends_to_chat(
    isolated_state_db: Path, rest: httpx.AsyncClient
) -> None:
    bot = _make_bot(rest)
    ctx = _mock_context()
    update = MagicMock()

    _aiorun(bot._cmd_queue(update, ctx))

    ctx.bot.send_message.assert_called_once()
    kwargs = ctx.bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == 42
    assert "Queue:" in kwargs["text"]


def test_cmd_pause_with_args(rest: httpx.AsyncClient) -> None:
    bot = _make_bot(rest)
    ctx = _mock_context()
    ctx.args = ["lunch", "break"]

    _aiorun(bot._cmd_pause(MagicMock(), ctx))

    ctx.bot.send_message.assert_called_once()
    text = ctx.bot.send_message.call_args.kwargs["text"]
    assert "paused" in text
    assert "lunch break" in text


def test_cmd_issue_validates_arg_count(rest: httpx.AsyncClient) -> None:
    bot = _make_bot(rest)
    ctx = _mock_context()
    ctx.args = ["only-one"]

    _aiorun(bot._cmd_issue(MagicMock(), ctx))
    text = ctx.bot.send_message.call_args.kwargs["text"]
    assert "usage:" in text


# ---------- scheduler-side notification production ----------


def test_state_transition_to_clarification_fires_notification(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    project = _write_project(tmp_path / "proj")
    register(project)
    state.upsert_project(name="teach-me-eng-bot", path=str(project), repo="me/x")

    # Seed prev state
    state.upsert_issue(
        project="teach-me-eng-bot", number=1, state_label="state:needs-planning",
    )

    gh_runner = FakeGhRunner(
        list_issues_payload=[
            {
                "number": 1,
                "title": "x",
                "url": "u",
                "createdAt": "t",
                "labels": [{"name": "state:clarification-needed"}],
                "author": {"login": "valdisd96"},
            }
        ]
    )
    dispatcher = Dispatcher(spawner=FakeSpawner(), gh_runner=gh_runner)
    scheduler = Scheduler(dispatcher=dispatcher, gh_runner=gh_runner)

    _aiorun(scheduler.tick())

    notifications = state.list_unacked_notifications()
    assert any(n.kind == "clarification" and n.issue == 1 for n in notifications)


def test_first_sight_does_not_fire_notification(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    """First time we see an issue (no prev row) should NOT fire a notif."""
    project = _write_project(tmp_path / "proj")
    register(project)
    state.upsert_project(name="teach-me-eng-bot", path=str(project), repo="me/x")

    gh_runner = FakeGhRunner(
        list_issues_payload=[
            {
                "number": 1,
                "title": "x",
                "url": "u",
                "createdAt": "t",
                "labels": [{"name": "state:clarification-needed"}],
                "author": {"login": "valdisd96"},
            }
        ]
    )
    dispatcher = Dispatcher(spawner=FakeSpawner(), gh_runner=gh_runner)
    scheduler = Scheduler(dispatcher=dispatcher, gh_runner=gh_runner)

    _aiorun(scheduler.tick())

    notifications = state.list_unacked_notifications()
    assert all(n.kind != "clarification" for n in notifications)


# ---------- TG-message reverse mapping ----------


def test_set_and_find_notification_tg_message(isolated_state_db: Path) -> None:
    state.upsert_project(name="t", path="/p", repo="me/t")
    nid = state.add_notification(kind="blocked", project="t", issue=1)
    state.set_notification_tg_message(nid, chat_id=100, message_id=200)
    found = state.find_notification_by_tg_message(100, 200)
    assert found is not None
    assert found.id == nid


def test_find_notification_returns_none_for_unknown(isolated_state_db: Path) -> None:
    assert state.find_notification_by_tg_message(999, 999) is None


# ---------- bot lifecycle (with fake Application) ----------


def test_bot_start_and_stop_with_fake_application(
    isolated_state_db: Path, rest: httpx.AsyncClient
) -> None:
    """Verify start() / stop() work end-to-end with a mocked Application."""

    fake_app = MagicMock()
    fake_app.initialize = AsyncMock()
    fake_app.start = AsyncMock()
    fake_app.stop = AsyncMock()
    fake_app.shutdown = AsyncMock()
    fake_app.updater = MagicMock()
    fake_app.updater.start_polling = AsyncMock()
    fake_app.updater.stop = AsyncMock()
    fake_app.add_handler = MagicMock()

    def factory(token: str) -> Any:
        return fake_app

    bot = TelegramBot(
        token="t",
        chat_id=42,
        rest_base_url="http://unused",
        rest_client=rest,
        application_factory=factory,
        notification_poll_interval_s=0.05,
    )

    async def _run() -> None:
        await bot.start()
        await asyncio.sleep(0.1)  # let one notification poll run
        await bot.stop()

    _aiorun(_run())

    fake_app.initialize.assert_called()
    fake_app.start.assert_called()
    fake_app.shutdown.assert_called()
