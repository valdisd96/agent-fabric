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

# States whose entry triggers a notification for the human (1F's TG bot).
# Notification is fired only on observed CHANGES, not on first-sight, so a
# fabric restart re-polling in-progress state doesn't spam.
_NOTIFY_STATE_KIND: dict[str, str] = {
    "state:clarification-needed": "clarification",
    "state:awaiting-decompose-approval": "decompose-approval",
}


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
                and state_label in _NOTIFY_STATE_KIND
            ):
                await asyncio.to_thread(
                    state.add_notification,
                    kind=_NOTIFY_STATE_KIND[state_label],
                    project=entry.name,
                    issue=issue.number,
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
    ) -> None:
        recent = await asyncio.to_thread(
            state.dispatches_for_issue, entry.name, issue_number, limit=5
        )
        body_lines = [f"⚠️ Auto-blocked: {reason}", "", "Recent attempts:"]
        for d in recent:
            tail = f" log={d.log_path}" if d.log_path else ""
            body_lines.append(
                f"- {d.started_at} stage={d.stage} exit={d.exit_code}{tail}"
            )
        body = "\n".join(body_lines)

        try:
            await asyncio.to_thread(
                gh.comment, entry.repo, issue_number, body, runner=self._gh_runner
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

        await asyncio.to_thread(
            state.upsert_issue,
            project=entry.name,
            number=issue_number,
            state_label="state:blocked",
        )
        await asyncio.to_thread(
            state.add_notification, kind=kind, project=entry.name, issue=issue_number
        )


__all__ = [
    "Scheduler",
    "TickResult",
    "TickWinner",
]
