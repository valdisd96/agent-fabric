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
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fabric import github as gh
from fabric import state
from fabric.config import FabricConfig, load_project_config
from fabric.dispatcher import Dispatcher, DispatchError, QuotaExceeded
from fabric.registry import ProjectEntry, fabric_home, load_registry


log = logging.getLogger("fabric.scheduler")


# Pseudo-state used internally when an open issue has no `state:*` label on
# GitHub — routes the issue into the qualify-issue triage stage. Never
# written back to GitHub; only lives in the scheduler/DB.
_UNQUALIFIED_STATE = "state:unqualified"

# Drain in-flight first → bring rework/tests back → start fresh planning.
# Triage (unqualified, epic) sits below planning so it doesn't preempt
# in-flight work. `state:in-progress` shares the in-review tier because a
# resume is in-flight work the human just unblocked, not a fresh planning
# round.
_STATE_TIER: dict[str, int] = {
    "state:in-review": 0,
    "state:in-progress": 0,
    "state:needs-rework": 1,
    "state:tests-pending": 2,
    "state:needs-planning": 3,
    "state:needs-decompose": 4,
    _UNQUALIFIED_STATE: 4,
}

_STAGE_BY_STATE: dict[str, str] = {
    "state:needs-planning": "plan-exec",
    "state:needs-rework": "plan-exec",
    "state:tests-pending": "test-writer",
    "state:in-review": "review-pr",
    "state:needs-decompose": "epic-decompose",
    _UNQUALIFIED_STATE: "qualify-issue",
}

# Resume routing for `state:in-progress`. The label is set by plan-exec /
# epic-decompose at the start of work and is also the resume target the
# Telegram bot writes after a non-epic clarification reply (see
# telegram_bot._resume_state_for_reply). An issue parked here is either
# (a) a clarification just unblocked, or (b) a prior dispatch crashed
# mid-stream — both cases want the original stage to be re-invoked. Type
# disambiguates: epics resume into epic-decompose, everything else into
# plan-exec.
_IN_PROGRESS_STATE = "state:in-progress"
_IN_PROGRESS_TYPE_STAGE: dict[str, str] = {
    "type:epic": "epic-decompose",
}
_IN_PROGRESS_DEFAULT_STAGE = "plan-exec"


def _resolve_stage(state_label: str, type_label: str | None) -> str | None:
    """Map (state, type) to the agent stage the scheduler should dispatch.
    Returns None when the issue is in a non-actionable state (human gate,
    terminal, or unrecognized label).
    """
    if state_label == _IN_PROGRESS_STATE:
        if type_label is not None:
            mapped = _IN_PROGRESS_TYPE_STAGE.get(type_label)
            if mapped is not None:
                return mapped
        return _IN_PROGRESS_DEFAULT_STAGE
    return _STAGE_BY_STATE.get(state_label)


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

# Cancellation. A closed issue is treated as cancelled (rather than
# completed) when either (a) GitHub's stateReason is `NOT_PLANNED` — the
# `gh issue close --reason "not planned"` and the web UI's "Close as not
# planned" both set this — or (b) the issue carries `state:cancelled` at
# close time. The label may also be applied to an OPEN issue: the
# scheduler simply skips it (no `_STAGE_BY_STATE` mapping), which lets
# users park a misfiled issue without closing it on GitHub.
_CANCELLED_STATE_LABEL = "state:cancelled"
_CANCELLED_NOTIF_KIND = "issue-cancelled"
_NOT_PLANNED_REASON = "NOT_PLANNED"
# Terminal states the completion-detector should never re-process.
_TERMINAL_STATE_LABELS: frozenset[str] = frozenset(
    {_DONE_STATE_LABEL, _CANCELLED_STATE_LABEL}
)

# Epic coordinator (B3 — hold-and-release).
_EPIC_TYPE_LABEL = "type:epic"
_DRAFT_STATE_LABEL = "state:draft"
_NEEDS_PLANNING_LABEL = "state:needs-planning"
_EPIC_ADVANCED_KIND = "epic-advanced"
_EPIC_COMPLETED_KIND = "epic-completed"

# `Refs #<N>` in a child issue's body marks the epic parent. Children are
# filed by the `epic-decompose` skill with this exact line.
_REFS_PARENT_RE = re.compile(r"\bRefs\s+#(\d+)\b", re.IGNORECASE)


def _parse_epic_parent(body: str | None) -> int | None:
    if not body:
        return None
    m = _REFS_PARENT_RE.search(body)
    return int(m.group(1)) if m else None


# Q&A states the agent uses to park an issue. On transitions INTO these
# states we fetch the latest agent-marker comment and embed it in the
# notification body so the human can see the question/proposal in TG
# without opening GitHub.
_AGENT_QUESTION_STATES: set[str] = {
    "state:clarification-needed",
    "state:awaiting-decompose-approval",
}

# Marker prefix every fabric agent prepends to its question / proposal
# comments (e.g. `<!-- agent-decompose-q v1 -->`, `<!-- agent-qualify v1 -->`,
# `<!-- agent-decompose v1 -->`). Used to distinguish agent-authored
# comments from human follow-ups.
_AGENT_COMMENT_MARKER_PREFIX = "<!-- agent-"


def _latest_agent_comment_body(comments: list[gh.IssueComment]) -> str | None:
    """Most recent comment whose body starts with `<!-- agent-... -->`.
    `gh issue view --json comments` returns comments in chronological
    order; the last agent-marker comment is the freshest question or
    proposal. Returns None if no agent-authored comment exists yet.
    """
    agent_authored = [
        c for c in comments
        if c.body.lstrip().startswith(_AGENT_COMMENT_MARKER_PREFIX)
    ]
    return agent_authored[-1].body if agent_authored else None


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
    type_label: str | None
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
        stage = _resolve_stage(winner.state_label, winner.type_label)
        # `_poll_project` only enqueues candidates whose (state, type) maps
        # to a real stage, so this is unreachable; mypy / future refactors
        # need the assertion to keep the type narrow.
        assert stage is not None

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
            # No state:* label → triage the issue via the qualify-issue
            # stage. We tag it with an internal pseudo-state so the existing
            # candidate/dispatch path handles it; the agent will set the
            # real state label as part of qualification.
            if state_label is None:
                state_label = _UNQUALIFIED_STATE
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
                # On Q&A transitions, embed the agent's most recent
                # question / proposal so it lands directly in TG. The
                # extra gh round-trip only fires when the human actually
                # needs to read the question, not on every transition.
                if state_label in _AGENT_QUESTION_STATES:
                    try:
                        detail = await asyncio.to_thread(
                            gh.get_issue, entry.repo, issue.number,
                            runner=self._gh_runner,
                        )
                    except gh.GhError:
                        detail = None
                    if detail is not None:
                        agent_body = _latest_agent_comment_body(detail.comments)
                        if agent_body:
                            body = f"{body}\n\n{agent_body}"
                await asyncio.to_thread(
                    state.add_notification,
                    kind=kind,
                    project=entry.name,
                    issue=issue.number,
                    body=body,
                )

            # Cycle-counter bump on entry into state:needs-rework. Without
            # this the cap is dead — `Dispatcher.bump_cycle` exists but no
            # call site invokes it, so cycle_count stays at 0 forever and
            # runaway rework loops never auto-block. We bump only on
            # observed transitions (not first-sight) so a fabric restart
            # mid-rework doesn't double-count. `bump_cycle` itself does
            # `max(GH-comment, DB) + 1` so a fabric reinstall recovers
            # the right counter from the GH HTML comment.
            if (
                prev_state is not None
                and prev_state != "state:needs-rework"
                and state_label == "state:needs-rework"
            ):
                try:
                    await self._dispatcher.bump_cycle(entry.name, issue.number)
                except Exception:
                    log.exception(
                        "bump_cycle failed for %s#%d",
                        entry.name, issue.number,
                    )

            if author not in config.project.trusted_authors:
                continue
            if _resolve_stage(state_label, type_label) is None:
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
                    type_label=type_label,
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
        closure via `gh.get_issue`. Mark them terminal (`state:done` or
        `state:cancelled`) and fire one `issue-completed` /
        `issue-cancelled` notification each. Closed issues lose their
        `state:*` label, so this is the only place that detects pipeline
        completion (PR-merge auto-close, manual close-as-completed, or
        manual close-as-not-planned).
        """
        tracked = await asyncio.to_thread(state.list_issues, entry.name)
        for row in tracked:
            if row.number in observed_open:
                continue
            if row.state_label in (None, *_TERMINAL_STATE_LABELS):
                continue
            try:
                detail = await asyncio.to_thread(
                    gh.get_issue, entry.repo, row.number, runner=self._gh_runner
                )
            except gh.GhError:
                continue
            if detail.state.upper() != "CLOSED":
                continue
            cancelled = (
                detail.state_reason.upper() == _NOT_PLANNED_REASON
                or any(
                    lbl.name == _CANCELLED_STATE_LABEL for lbl in detail.labels
                )
            )
            terminal_label = (
                _CANCELLED_STATE_LABEL if cancelled else _DONE_STATE_LABEL
            )
            notif_kind = (
                _CANCELLED_NOTIF_KIND if cancelled else _COMPLETED_NOTIF_KIND
            )
            await asyncio.to_thread(
                state.upsert_issue,
                project=entry.name,
                number=row.number,
                state_label=terminal_label,
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
                kind=notif_kind,
                project=entry.name,
                issue=row.number,
            )
            await self._advance_or_close_parent(entry, detail)

    async def _advance_or_close_parent(
        self,
        entry: ProjectEntry,
        closed_detail: gh.IssueDetail,
    ) -> None:
        """B3 coordinator. When a `type:epic` parent's child closes, advance
        the oldest `state:draft` sibling to `state:needs-planning`. If no
        drafts remain and no other siblings are open, close the parent.

        Best-effort — gh hiccups silently no-op so a transient API blip
        doesn't poison the tick. Next closure will re-trigger.
        """
        parent_num = _parse_epic_parent(closed_detail.body)
        if parent_num is None:
            return
        try:
            parent = await asyncio.to_thread(
                gh.get_issue, entry.repo, parent_num, runner=self._gh_runner,
            )
        except gh.GhError:
            return
        if not any(lbl.name == _EPIC_TYPE_LABEL for lbl in parent.labels):
            return
        if parent.state.upper() == "CLOSED":
            return

        try:
            siblings = await asyncio.to_thread(
                gh.list_issues,
                entry.repo,
                label_query=f'in:body "Refs #{parent_num}"',
                state="open",
                runner=self._gh_runner,
            )
        except gh.GhError:
            return

        # Defensive: gh search may briefly include the just-closed issue.
        siblings = [s for s in siblings if s.number != closed_detail.number]

        drafts = [
            s for s in siblings
            if any(lbl.name == _DRAFT_STATE_LABEL for lbl in s.labels)
        ]
        non_drafts = [
            s for s in siblings
            if not any(lbl.name == _DRAFT_STATE_LABEL for lbl in s.labels)
        ]

        # Another sibling is already in flight; let it finish before
        # advancing — its closure will trigger the next advance.
        if non_drafts:
            return

        if drafts:
            oldest = min(drafts, key=lambda s: s.created_at or "")
            try:
                await asyncio.to_thread(
                    gh.remove_labels, entry.repo, oldest.number,
                    [_DRAFT_STATE_LABEL], runner=self._gh_runner,
                )
                await asyncio.to_thread(
                    gh.add_labels, entry.repo, oldest.number,
                    [_NEEDS_PLANNING_LABEL], runner=self._gh_runner,
                )
            except gh.GhError:
                return
            await asyncio.to_thread(
                state.add_notification,
                kind=_EPIC_ADVANCED_KIND,
                project=entry.name,
                issue=parent_num,
                body=(
                    f"{entry.name}#{parent_num} — epic advanced\n"
                    f"#{closed_detail.number} closed → "
                    f"#{oldest.number} now {_NEEDS_PLANNING_LABEL}"
                ),
            )
            return

        # No active siblings, no drafts left — all children done.
        try:
            await asyncio.to_thread(
                gh.close_issue,
                entry.repo,
                parent_num,
                comment_body=(
                    "Auto-closed by agent-fabric: all epic children have closed."
                ),
                runner=self._gh_runner,
            )
        except gh.GhError:
            return
        await asyncio.to_thread(
            state.add_notification,
            kind=_EPIC_COMPLETED_KIND,
            project=entry.name,
            issue=parent_num,
            body=f"{entry.name}#{parent_num} — epic completed (all children closed)",
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
