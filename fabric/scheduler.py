"""Cross-project scheduler tick — D3 selection + retry/pause/cycle gating.

One `Scheduler.tick()` call:
  1. Honours three-layer pause (settings flag, per-project flag, touch-file).
  2. Polls each registered project via `gh.list_issues` and upserts into DB.
  3. Filters candidates: trusted-author-only, actionable state labels,
     under cycle cap, past retry-backoff window.
  4. Sorts by D3 — state tier > priority > round-robin > createdAt.
  5. Hands the winner to the dispatcher; updates `last_served_at` for fairness.

The 60s tick loop lives in 1E's FastAPI lifespan; this module only ships
the synchronous decision layer + the CLI hook.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fabric import github as gh
from fabric import state
from fabric.config import FabricConfig, load_project_config
from fabric.dispatcher import Dispatcher, DispatchError, QuotaExceeded
from fabric.registry import ProjectEntry, fabric_home, load_registry


# Drain in-flight first → bring rework/tests back → start fresh planning.
_STATE_TIER: dict[str, int] = {
    "state:in-review": 0,
    "state:needs-rework": 1,
    "state:tests-pending": 2,
    "state:needs-planning": 3,
}

_STAGE_BY_STATE: dict[str, str] = {
    "state:needs-planning": "plan-exec",
    "state:needs-rework": "plan-exec",
    "state:tests-pending": "test-writer",
    "state:in-review": "review-pr",
}

_PRIORITY_RANK: dict[str, int] = {
    "priority:high": 0,
    "priority:medium": 1,
    "priority:low": 2,
}
_DEFAULT_PRIORITY_RANK = _PRIORITY_RANK["priority:medium"]

# State→kind map for transitions that already have a dedicated notification
# kind with custom buttons in the TG bot. Anything not in this map (and not
# `state:blocked`, which is fired by `_block_issue`) gets the catch-all
# `state-changed` kind. Notifications fire only on observed CHANGES, not on
# first-sight, so a fabric restart doesn't spam the human chat.
_NOTIFY_STATE_KIND: dict[str, str] = {
    "state:clarification-needed": "clarification",
    "state:awaiting-decompose-approval": "decompose-approval",
}
_GENERIC_TRANSITION_KIND = "state-changed"
# The auto-block path (`_block_issue`) fires its own enriched `blocked` /
# `dispatch-failed` notification, so suppress the generic transition fire.
_SUPPRESS_GENERIC_FOR_STATE: set[str] = {"state:blocked"}

# Window for attributing a state change to the most-recent completed dispatch.
# Anything beyond this is treated as an external/manual edit.
_ATTRIBUTION_WINDOW_SECONDS = 300

_DONE_STATE_LABEL = "state:done"
_COMPLETED_NOTIF_KIND = "issue-completed"


@dataclass(frozen=True)
class TickWinner:
    project: str
    issue: int
    stage: str
    exit_code: int
    dispatch_id: int


@dataclass(frozen=True)
class TickResult:
    skipped_reason: str | None = None
    winner: TickWinner | None = None
    polled_projects: int = 0
    candidates: int = 0


@dataclass
class _Candidate:
    entry: ProjectEntry
    config: FabricConfig
    issue_number: int
    state_label: str
    priority_rank: int
    last_served_at: str
    created_at: str


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: str) -> datetime:
    """Tolerant ISO-8601 parser (handles the Z suffix gh emits)."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _label_with_prefix(labels: Iterable[gh.Label], prefix: str) -> str | None:
    for lbl in labels:
        if lbl.name.startswith(prefix):
            return lbl.name
    return None


def _format_transition_body(
    *,
    project: str,
    issue: int,
    title: str | None,
    prev_state: str,
    new_state: str,
    cycle_count: int,
    cycle_limit: int,
    priority_label: str | None,
    type_label: str | None,
    actor: str,
    url: str | None,
) -> str:
    """One pre-rendered text block stored in `notifications.body`. The TG
    formatter prefers this over the legacy minimal rendering. Keeping the
    rendering at fire-time means we capture old→new state, which gets lost
    once the issue row is upserted with the new label.
    """
    head = f"{project}#{issue}"
    if title:
        head = f"{head} — {title}"
    meta_bits = [f"cycle {cycle_count}/{cycle_limit}"]
    if priority_label:
        meta_bits.append(priority_label)
    if type_label:
        meta_bits.append(type_label)
    lines = [
        head,
        f"{prev_state} → {new_state}",
        " · ".join(meta_bits),
        f"by {actor}",
    ]
    if url:
        lines.append(url)
    return "\n".join(lines)


def _attribute_change(
    latest: state.DispatchRow | None, now: datetime
) -> str:
    """Best-guess attribution: which agent stage caused this state change?
    If a dispatch ended cleanly within the attribution window, blame it.
    Otherwise treat the change as external (someone edited a label by hand).
    """
    if latest is None or not latest.ended_at:
        return "manual"
    try:
        ended = _parse_iso(latest.ended_at)
    except ValueError:
        return "manual"
    if (now - ended).total_seconds() > _ATTRIBUTION_WINDOW_SECONDS:
        return "manual"
    if latest.exit_code != 0:
        return "manual"
    return latest.stage


class Scheduler:
    """One-tick dispatcher for the cross-project queue."""

    def __init__(
        self,
        *,
        dispatcher: Dispatcher,
        gh_runner: gh.SubprocessRunner | None = None,
        fabric_home_path: Path | None = None,
        now: Callable[[], datetime] = _now_utc,
    ) -> None:
        self._dispatcher = dispatcher
        self._gh_runner: gh.SubprocessRunner = gh_runner or gh._default_runner
        self._home = fabric_home_path or fabric_home()
        self._now = now

    # ---- public ----

    async def tick(self) -> TickResult:
        reason = await self._global_paused_reason()
        if reason:
            return TickResult(skipped_reason=reason)

        registry_entries = load_registry()
        candidates: list[_Candidate] = []
        polled = 0

        for entry in registry_entries:
            try:
                config = await asyncio.to_thread(load_project_config, entry.path)
            except Exception:
                continue
            await self._ensure_state_project(entry, config)

            proj_row = await asyncio.to_thread(state.get_project, entry.name)
            if proj_row is not None and proj_row.paused:
                continue

            polled += 1
            await self._poll_project(entry, config, proj_row, candidates)

        if not candidates:
            return TickResult(polled_projects=polled, candidates=0)

        candidates.sort(key=self._sort_key)
        winner = candidates[0]
        stage = _STAGE_BY_STATE[winner.state_label]

        try:
            result = await self._dispatcher.dispatch(
                winner.entry.name,
                winner.issue_number,
                stage,
                triggered_by="scheduler",
            )
        except QuotaExceeded as e:
            return TickResult(
                skipped_reason=f"quota: {e}",
                polled_projects=polled,
                candidates=len(candidates),
            )
        except DispatchError as e:
            return TickResult(
                skipped_reason=f"dispatch error: {e}",
                polled_projects=polled,
                candidates=len(candidates),
            )

        await asyncio.to_thread(
            state.set_project_last_served,
            winner.entry.name,
            self._now().isoformat(timespec="seconds"),
        )

        return TickResult(
            polled_projects=polled,
            candidates=len(candidates),
            winner=TickWinner(
                project=winner.entry.name,
                issue=winner.issue_number,
                stage=stage,
                exit_code=result.exit_code,
                dispatch_id=result.dispatch_id,
            ),
        )

    # ---- pause ----

    async def _global_paused_reason(self) -> str | None:
        if (self._home / "PAUSED").exists():
            return f"PAUSED touch-file at {self._home / 'PAUSED'}"
        flag = await asyncio.to_thread(state.get_setting, "paused")
        if flag == "1":
            why = await asyncio.to_thread(state.get_setting, "paused_reason")
            return f"paused: {why}" if why else "paused"
        return None

    # ---- polling ----

    async def _ensure_state_project(self, entry: ProjectEntry, config: FabricConfig) -> None:
        await asyncio.to_thread(
            state.upsert_project,
            name=entry.name,
            path=entry.path,
            repo=entry.repo,
            fabric_version=config.fabric_version,
        )

    async def _poll_project(
        self,
        entry: ProjectEntry,
        config: FabricConfig,
        proj_row: state.ProjectRow | None,
        candidates: list[_Candidate],
    ) -> None:
        try:
            issues = await asyncio.to_thread(
                gh.list_issues,
                entry.repo,
                state="open",
                limit=200,
                runner=self._gh_runner,
            )
        except gh.GhError:
            return

        last_served = (proj_row.last_served_at if proj_row else None) or ""
        observed_open: set[int] = {issue.number for issue in issues}

        for issue in issues:
            state_label = _label_with_prefix(issue.labels, "state:")
            if state_label is None:
                continue
            priority_label = _label_with_prefix(issue.labels, "priority:")
            type_label = _label_with_prefix(issue.labels, "type:")
            area_label = _label_with_prefix(issue.labels, "area:")
            author = issue.author.login if issue.author else None

            prev_row = await asyncio.to_thread(state.get_issue, entry.name, issue.number)
            prev_state = prev_row.state_label if prev_row else None

            await asyncio.to_thread(
                state.upsert_issue,
                project=entry.name,
                number=issue.number,
                state_label=state_label,
                type_label=type_label,
                priority_label=priority_label,
                area_label=area_label,
                title=issue.title,
                url=issue.url,
                author=author,
                created_at=issue.created_at,
            )

            # Fire a notification on observed state transitions. We only fire
            # when prev_state was already populated (not on first-sight) so a
            # fabric restart doesn't spam the human chat.
            if (
                prev_state is not None
                and prev_state != state_label
                and state_label not in _SUPPRESS_GENERIC_FOR_STATE
            ):
                kind = _NOTIFY_STATE_KIND.get(state_label, _GENERIC_TRANSITION_KIND)
                cycle_count = prev_row.cycle_count if prev_row else 0
                latest = await asyncio.to_thread(
                    state.latest_dispatch, entry.name, issue.number
                )
                actor = _attribute_change(latest, self._now())
                body = _format_transition_body(
                    project=entry.name,
                    issue=issue.number,
                    title=issue.title,
                    prev_state=prev_state,
                    new_state=state_label,
                    cycle_count=cycle_count,
                    cycle_limit=config.pipeline.cycle_limit,
                    priority_label=priority_label,
                    type_label=type_label,
                    actor=actor,
                    url=issue.url,
                )
                await asyncio.to_thread(
                    state.add_notification,
                    kind=kind,
                    project=entry.name,
                    issue=issue.number,
                    body=body,
                )

            if author not in config.project.trusted_authors:
                continue
            if state_label not in _STAGE_BY_STATE:
                continue

            issue_row = await asyncio.to_thread(state.get_issue, entry.name, issue.number)
            if (
                issue_row is not None
                and issue_row.cycle_count >= config.pipeline.cycle_limit
            ):
                await self._block_issue(
                    entry,
                    issue.number,
                    state_label,
                    reason=(
                        f"cycle cap reached "
                        f"({issue_row.cycle_count}/{config.pipeline.cycle_limit})"
                    ),
                    kind="blocked",
                    cycle_limit=config.pipeline.cycle_limit,
                )
                continue

            failures = await asyncio.to_thread(
                state.consecutive_failures, entry.name, issue.number
            )
            if failures >= config.pipeline.retry_count:
                await self._block_issue(
                    entry,
                    issue.number,
                    state_label,
                    reason=f"dispatch failed {failures} times",
                    kind="dispatch-failed",
                    cycle_limit=config.pipeline.cycle_limit,
                )
                continue
            if failures > 0:
                latest = await asyncio.to_thread(
                    state.latest_dispatch, entry.name, issue.number
                )
                if latest and latest.ended_at and self._in_backoff(
                    failures, latest.ended_at, config.pipeline.retry_backoff_seconds
                ):
                    continue

            candidates.append(
                _Candidate(
                    entry=entry,
                    config=config,
                    issue_number=issue.number,
                    state_label=state_label,
                    priority_rank=_PRIORITY_RANK.get(
                        priority_label or "", _DEFAULT_PRIORITY_RANK
                    ),
                    last_served_at=last_served,
                    created_at=issue.created_at or "",
                )
            )

        await self._detect_completions(entry, observed_open)

    async def _detect_completions(
        self, entry: ProjectEntry, observed_open: set[int]
    ) -> None:
        """Find tracked issues that vanished from the open list and confirm
        closure via `gh.get_issue`. Mark them `state:done` and fire one
        `issue-completed` notification each. Closed issues lose their
        `state:*` label, so this is the only place that detects pipeline
        completion (PR-merge auto-close or manual close-as-completed).
        """
        tracked = await asyncio.to_thread(state.list_issues, entry.name)
        for row in tracked:
            if row.number in observed_open:
                continue
            if row.state_label in (None, _DONE_STATE_LABEL):
                continue
            try:
                detail = await asyncio.to_thread(
                    gh.get_issue, entry.repo, row.number, runner=self._gh_runner
                )
            except gh.GhError:
                continue
            if detail.state.upper() != "CLOSED":
                continue
            await asyncio.to_thread(
                state.upsert_issue,
                project=entry.name,
                number=row.number,
                state_label=_DONE_STATE_LABEL,
                type_label=row.type_label,
                priority_label=row.priority_label,
                area_label=row.area_label,
                title=row.title,
                url=row.url,
                author=row.author,
                created_at=row.created_at,
            )
            await asyncio.to_thread(
                state.add_notification,
                kind=_COMPLETED_NOTIF_KIND,
                project=entry.name,
                issue=row.number,
            )

    def _in_backoff(
        self, failures: int, ended_at: str, backoffs: list[int]
    ) -> bool:
        if not backoffs:
            return False
        idx = min(failures - 1, len(backoffs) - 1)
        delay = backoffs[idx]
        try:
            ended = _parse_iso(ended_at)
        except ValueError:
            return False
        elapsed = (self._now() - ended).total_seconds()
        return elapsed < delay

    # ---- D3 sort ----

    def _sort_key(self, c: _Candidate) -> tuple[int, int, str, str, int]:
        return (
            _STATE_TIER.get(c.state_label, 99),
            c.priority_rank,
            c.last_served_at,  # ascending → less-recently-served goes first
            c.created_at,      # ascending → older issue goes first
            c.issue_number,
        )

    # ---- blocking ----

    async def _block_issue(
        self,
        entry: ProjectEntry,
        issue_number: int,
        current_state: str,
        *,
        reason: str,
        kind: str,
        cycle_limit: int,
    ) -> None:
        recent = await asyncio.to_thread(
            state.dispatches_for_issue, entry.name, issue_number, limit=5
        )
        gh_comment_lines = [f"⚠️ Auto-blocked: {reason}", "", "Recent attempts:"]
        for d in recent:
            tail = f" log={d.log_path}" if d.log_path else ""
            gh_comment_lines.append(
                f"- {d.started_at} stage={d.stage} exit={d.exit_code}{tail}"
            )
        gh_comment_body = "\n".join(gh_comment_lines)

        try:
            await asyncio.to_thread(
                gh.comment, entry.repo, issue_number, gh_comment_body,
                runner=self._gh_runner,
            )
            await asyncio.to_thread(
                gh.remove_labels,
                entry.repo,
                issue_number,
                [current_state],
                runner=self._gh_runner,
            )
            await asyncio.to_thread(
                gh.add_labels,
                entry.repo,
                issue_number,
                ["state:blocked"],
                runner=self._gh_runner,
            )
        except gh.GhError:
            pass  # best-effort — next tick can retry

        prev_row = await asyncio.to_thread(state.get_issue, entry.name, issue_number)
        await asyncio.to_thread(
            state.upsert_issue,
            project=entry.name,
            number=issue_number,
            state_label="state:blocked",
        )
        notif_body: str | None = None
        if prev_row is not None:
            notif_body = _format_transition_body(
                project=entry.name,
                issue=issue_number,
                title=prev_row.title,
                prev_state=current_state,
                new_state="state:blocked",
                cycle_count=prev_row.cycle_count,
                cycle_limit=cycle_limit,
                priority_label=prev_row.priority_label,
                type_label=prev_row.type_label,
                actor=f"system: {reason}",
                url=prev_row.url,
            )
        await asyncio.to_thread(
            state.add_notification,
            kind=kind, project=entry.name, issue=issue_number, body=notif_body,
        )


__all__ = [
    "Scheduler",
    "TickResult",
    "TickWinner",
]
