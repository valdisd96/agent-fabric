"""Long-running `claude -p` subprocess runner.

Single-flight via an `asyncio.Semaphore(1)` — exactly one agent stage
runs in the fabric process at any time, by design (DESIGN.md Decision 3).
Streams stdout to a per-dispatch log file under `$FABRIC_HOME/logs/...`
and to an in-memory pubsub for live consumers (the FastAPI WS in 1E,
the Telegram bot in 1F).

The exact `claude -p` argv is intentionally minimal in this sub-PR;
SMOKE.md (1G) will document the full prompt + system-prompt shape once
the end-to-end smoke against teach-me-eng-bot has run.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fabric import github as gh
from fabric import state
from fabric.config import FabricConfig, load_project_config
from fabric.registry import ProjectEntry, fabric_home, find as find_project


log = logging.getLogger("fabric.dispatcher")


class DispatchError(Exception):
    """Wrong project, missing config, or other dispatch-time failure."""


class QuotaExceeded(DispatchError):
    """Per-project daily dispatch cap reached."""


@dataclass(frozen=True)
class DispatchResult:
    dispatch_id: int
    exit_code: int
    log_path: Path
    duration_s: float


SubprocessSpawner = Callable[..., Awaitable[asyncio.subprocess.Process]]

_DEFAULT_MODEL = "claude-opus-4-7"
_DOWNGRADE_MODEL = "claude-sonnet-4-6"
_LOW_PRIORITY_LABEL = "priority:low"
_CYCLE_MARKER = "cycle"
_CYCLE_SENTINEL = f"<!-- agent-fabric:{_CYCLE_MARKER} -->"

# Asyncio's StreamReader defaults to a 64 KB buffer per readline. The agent
# runs with `--output-format stream-json --verbose`, which emits one JSON
# event per tool call; a single big tool result (long file read, verbose
# bash output) easily exceeds 64 KB. When that happens the reader raises
# LimitOverrunError mid-dispatch and the subprocess gets orphaned. 16 MB
# fits any realistic event without forcing a defensive overflow path on
# the hot path.
_STREAM_READER_LIMIT = 16 * 1024 * 1024
_OVERFLOW_EXIT_CODE = -1
_TERMINATE_GRACE_S = 5.0

#: Sentinel for deploy-diagnose dispatches — not bound to a GitHub issue.
_NO_ISSUE = 0
#: Skill name (must match `fabric/skill_templates/deploy-diagnose/`).
_DIAGNOSE_STAGE = "deploy-diagnose"


async def _default_spawner(args: list[str], **kwargs: Any) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(*args, **kwargs)


class Dispatcher:
    """Owns the single-flight semaphore + the dispatch pubsub.

    The constructor takes injectable collaborators for tests:
    `spawner` for the `claude -p` subprocess, `gh_runner` for any gh
    CLI calls (currently only the cycle-counter mirror).
    """

    def __init__(
        self,
        *,
        fabric_home_path: Path | None = None,
        spawner: SubprocessSpawner = _default_spawner,
        gh_runner: gh.SubprocessRunner | None = None,
    ) -> None:
        self._home = fabric_home_path or fabric_home()
        self._spawner = spawner
        self._gh_runner: gh.SubprocessRunner = gh_runner or gh._default_runner
        self._sem = asyncio.Semaphore(1)
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []

    # ---- public API ----

    async def dispatch(
        self,
        project: str,
        issue: int,
        stage: str,
        *,
        model: str | None = None,
        triggered_by: str = "manual",
    ) -> DispatchResult:
        entry, config = self._load_project(project)
        await self._ensure_state_project(entry, config)

        used = await asyncio.to_thread(
            state.dispatches_in_window,
            project,
            config.pipeline.dispatch_window_hours,
        )
        if used >= config.pipeline.dispatch_cap:
            raise QuotaExceeded(
                f"{project} hit cap "
                f"({used}/{config.pipeline.dispatch_cap} "
                f"in last {config.pipeline.dispatch_window_hours}h)"
            )

        resolved_model = await self._resolve_model(project, issue, model, config)

        async with self._sem:
            return await self._run(
                entry=entry,
                issue=issue,
                stage=stage,
                model=resolved_model,
                triggered_by=triggered_by,
            )

    async def dispatch_deploy_diagnose(
        self, project: str, deployment_id: int, *, model: str | None = None,
    ) -> DispatchResult:
        """Dispatch the deploy-diagnose skill against a failed deployment.

        Unlike `dispatch()`, this is not tied to a GitHub issue — the
        diagnose's job is to *file* one. The dispatch row is recorded with
        `issue=0` and a non-NULL `deployment_id`; the deployment row is
        updated to point back at the dispatch via
        `state.set_deployment_diagnose_dispatch` once the run starts.

        Triggered automatically by `POST /api/projects/<n>/deploy-failures`
        and manually by `fabric diagnose <project> <deployment-id>`.
        """
        entry, config = self._load_project(project)
        await self._ensure_state_project(entry, config)

        deployment = await asyncio.to_thread(state.get_deployment, deployment_id)
        if deployment is None:
            raise DispatchError(f"deployment {deployment_id} not found")
        if deployment.project != project:
            raise DispatchError(
                f"deployment {deployment_id} belongs to project "
                f"{deployment.project!r}, not {project!r}"
            )

        used = await asyncio.to_thread(
            state.dispatches_in_window,
            project,
            config.pipeline.dispatch_window_hours,
        )
        if used >= config.pipeline.dispatch_cap:
            raise QuotaExceeded(
                f"{project} hit cap "
                f"({used}/{config.pipeline.dispatch_cap} "
                f"in last {config.pipeline.dispatch_window_hours}h)"
            )

        # Force opus — diagnose is reasoning-heavy; never downgrade.
        resolved_model = model or _DEFAULT_MODEL
        prev_good = await asyncio.to_thread(
            state.previous_successful_deployment, project, before_id=deployment_id
        )
        bundle = self._build_diagnose_bundle(deployment, prev_good)

        async with self._sem:
            return await self._run_diagnose(
                entry=entry,
                deployment=deployment,
                bundle=bundle,
                model=resolved_model,
            )

    async def bump_cycle(self, project: str, issue: int) -> int:
        """`max(GH-comment, DB)` + 1, written back to both stores.

        On a fresh `state.db` we still recover the right counter from the
        GH HTML comment so a wiped+reinstalled fabric doesn't reset the
        cycle cap to zero.
        """
        entry = find_project(project)
        if entry is None:
            raise DispatchError(f"project {project!r} not registered")
        await self._ensure_state_project_simple(entry)

        gh_n = await asyncio.to_thread(self._read_cycle_from_gh, entry.repo, issue)
        issue_row = await asyncio.to_thread(state.get_issue, project, issue)
        db_n = issue_row.cycle_count if issue_row is not None else 0
        new = max(gh_n, db_n) + 1

        if issue_row is None:
            await asyncio.to_thread(state.upsert_issue, project=project, number=issue)
        await asyncio.to_thread(state.set_cycle_count, project, issue, new)
        await asyncio.to_thread(
            gh.set_html_comment,
            entry.repo,
            issue,
            marker=_CYCLE_MARKER,
            value=str(new),
            runner=self._gh_runner,
        )
        return new

    def subscribe(self, *, maxsize: int = 1024) -> asyncio.Queue[dict[str, Any]]:
        """Register a pubsub queue for dispatch_started/stdout/ended events."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Stop receiving events on `queue`. Idempotent."""
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass

    # ---- internals ----

    def _load_project(self, project: str) -> tuple[ProjectEntry, FabricConfig]:
        entry = find_project(project)
        if entry is None:
            raise DispatchError(f"project {project!r} not registered")
        try:
            config = load_project_config(entry.path)
        except Exception as e:
            raise DispatchError(
                f"project {project!r}: failed to load config from {entry.path}: {e}"
            ) from e
        return entry, config

    async def _ensure_state_project(self, entry: ProjectEntry, config: FabricConfig) -> None:
        await asyncio.to_thread(
            state.upsert_project,
            name=entry.name,
            path=entry.path,
            repo=entry.repo,
            fabric_version=config.fabric_version,
        )

    async def _ensure_state_project_simple(self, entry: ProjectEntry) -> None:
        await asyncio.to_thread(
            state.upsert_project,
            name=entry.name,
            path=entry.path,
            repo=entry.repo,
        )

    async def _resolve_model(
        self, project: str, issue: int, override: str | None, config: FabricConfig
    ) -> str:
        if override:
            return override
        if not config.pipeline.downgrade_low_priority:
            return _DEFAULT_MODEL
        issue_row = await asyncio.to_thread(state.get_issue, project, issue)
        if issue_row is not None and issue_row.priority_label == _LOW_PRIORITY_LABEL:
            return _DOWNGRADE_MODEL
        return _DEFAULT_MODEL

    async def _run(
        self,
        *,
        entry: ProjectEntry,
        issue: int,
        stage: str,
        model: str,
        triggered_by: str,
    ) -> DispatchResult:
        log_path = self._log_path(entry.name, issue, stage)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        started = datetime.now(timezone.utc)
        dispatch_id = await asyncio.to_thread(
            state.record_dispatch,
            project=entry.name,
            issue=issue,
            stage=stage,
            started_at=started.isoformat(timespec="seconds"),
            triggered_by=triggered_by,
        )

        log.info(
            "dispatch start id=%d project=%s issue=%d stage=%s model=%s triggered_by=%s",
            dispatch_id,
            entry.name,
            issue,
            stage,
            model,
            triggered_by,
        )

        await self._publish(
            {
                "kind": "dispatch_started",
                "dispatch_id": dispatch_id,
                "project": entry.name,
                "issue": issue,
                "stage": stage,
                "model": model,
                "log_path": str(log_path),
            }
        )

        argv = self._build_argv(model=model, project_path=entry.path, stage=stage, issue=issue)

        proc = await self._spawner(
            argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=_STREAM_READER_LIMIT,
        )

        stream_overflow = False
        with log_path.open("w", encoding="utf-8") as logf:
            assert proc.stdout is not None
            try:
                async for line_bytes in proc.stdout:
                    line = line_bytes.decode(errors="replace")
                    logf.write(line)
                    logf.flush()
                    await self._publish(
                        {
                            "kind": "dispatch_stdout",
                            "dispatch_id": dispatch_id,
                            "project": entry.name,
                            "issue": issue,
                            "stage": stage,
                            "line": line.rstrip("\n"),
                        }
                    )
            except asyncio.LimitOverrunError as e:
                # Single stdout line still bigger than 16 MB. Defensive —
                # don't leave the subprocess orphaned the way the prior
                # 64 KB default did. Terminate, fall through, and let the
                # standard end-of-dispatch path close the DB row with a
                # synthetic failure code so single-flight stays honest.
                log.error(
                    "dispatch %d: stdout line exceeded reader limit "
                    "(%s); terminating subprocess",
                    dispatch_id,
                    e,
                )
                stream_overflow = True
                proc.terminate()
                logf.write(
                    f"\n[fabric] stdout line exceeded reader limit; "
                    f"subprocess terminated by dispatcher\n"
                )
                logf.flush()

        if stream_overflow:
            try:
                await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_GRACE_S)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            exit_code = _OVERFLOW_EXIT_CODE
        else:
            exit_code = await proc.wait()
        ended = datetime.now(timezone.utc)

        await asyncio.to_thread(
            state.complete_dispatch,
            dispatch_id,
            ended_at=ended.isoformat(timespec="seconds"),
            exit_code=exit_code,
            log_path=str(log_path),
        )
        duration_s = (ended - started).total_seconds()
        # Bias toward log volume on failure: success at INFO, failure at
        # WARNING with the log path so journalctl alone tells the story.
        if exit_code == 0:
            log.info(
                "dispatch end id=%d project=%s issue=%d stage=%s exit=0 duration=%.1fs",
                dispatch_id,
                entry.name,
                issue,
                stage,
                duration_s,
            )
        else:
            log.warning(
                "dispatch FAILED id=%d project=%s issue=%d stage=%s exit=%d duration=%.1fs log=%s",
                dispatch_id,
                entry.name,
                issue,
                stage,
                exit_code,
                duration_s,
                log_path,
            )

        await self._publish(
            {
                "kind": "dispatch_ended",
                "dispatch_id": dispatch_id,
                "project": entry.name,
                "issue": issue,
                "stage": stage,
                "exit_code": exit_code,
                "log_path": str(log_path),
            }
        )

        return DispatchResult(
            dispatch_id=dispatch_id,
            exit_code=exit_code,
            log_path=log_path,
            duration_s=duration_s,
        )

    def _build_diagnose_bundle(
        self,
        deployment: state.DeploymentRow,
        prev_good: state.DeploymentRow | None,
    ) -> dict[str, Any]:
        """Compose the JSON bundle the diagnose agent reads from its prompt."""
        return {
            "deployment_id": deployment.id,
            "project": deployment.project,
            "failed_sha": deployment.sha,
            "failed_short_sha": deployment.short_sha,
            "previous_good_sha": prev_good.sha if prev_good else None,
            "previous_good_short_sha": prev_good.short_sha if prev_good else None,
            "deployed_at": deployment.deployed_at,
            "service_unit": deployment.service_unit,
            "workflow_run_url": deployment.workflow_run_url,
            "journal_tail": deployment.journal_tail,
        }

    async def _run_diagnose(
        self,
        *,
        entry: ProjectEntry,
        deployment: state.DeploymentRow,
        bundle: dict[str, Any],
        model: str,
    ) -> DispatchResult:
        log_path = self._diagnose_log_path(entry.name, deployment.id)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        triggered_by = f"auto:deploy-failure-{deployment.id}"
        started = datetime.now(timezone.utc)
        dispatch_id = await asyncio.to_thread(
            state.record_dispatch,
            project=entry.name,
            issue=_NO_ISSUE,
            stage=_DIAGNOSE_STAGE,
            started_at=started.isoformat(timespec="seconds"),
            triggered_by=triggered_by,
            deployment_id=deployment.id,
        )
        # Link the deployment row back to this dispatch eagerly so a crash
        # mid-run still leaves a discoverable trail in the database.
        await asyncio.to_thread(
            state.set_deployment_diagnose_dispatch, deployment.id, dispatch_id
        )

        log.info(
            "diagnose start id=%d project=%s deployment=%d model=%s",
            dispatch_id, entry.name, deployment.id, model,
        )
        await self._publish(
            {
                "kind": "dispatch_started",
                "dispatch_id": dispatch_id,
                "project": entry.name,
                "issue": _NO_ISSUE,
                "stage": _DIAGNOSE_STAGE,
                "deployment_id": deployment.id,
                "model": model,
                "log_path": str(log_path),
            }
        )

        argv = self._build_diagnose_argv(
            model=model, project_path=entry.path, bundle=bundle,
        )
        proc = await self._spawner(
            argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=_STREAM_READER_LIMIT,
        )

        stream_overflow = False
        with log_path.open("w", encoding="utf-8") as logf:
            assert proc.stdout is not None
            try:
                async for line_bytes in proc.stdout:
                    line = line_bytes.decode(errors="replace")
                    logf.write(line)
                    logf.flush()
                    await self._publish(
                        {
                            "kind": "dispatch_stdout",
                            "dispatch_id": dispatch_id,
                            "project": entry.name,
                            "issue": _NO_ISSUE,
                            "stage": _DIAGNOSE_STAGE,
                            "deployment_id": deployment.id,
                            "line": line.rstrip("\n"),
                        }
                    )
            except asyncio.LimitOverrunError as e:
                log.error(
                    "diagnose %d: stdout line exceeded reader limit (%s); terminating",
                    dispatch_id, e,
                )
                stream_overflow = True
                proc.terminate()
                logf.write(
                    "\n[fabric] stdout line exceeded reader limit; "
                    "subprocess terminated by dispatcher\n"
                )
                logf.flush()

        if stream_overflow:
            try:
                await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_GRACE_S)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            exit_code = _OVERFLOW_EXIT_CODE
        else:
            exit_code = await proc.wait()
        ended = datetime.now(timezone.utc)

        await asyncio.to_thread(
            state.complete_dispatch,
            dispatch_id,
            ended_at=ended.isoformat(timespec="seconds"),
            exit_code=exit_code,
            log_path=str(log_path),
        )
        duration_s = (ended - started).total_seconds()
        if exit_code == 0:
            log.info(
                "diagnose end id=%d project=%s deployment=%d exit=0 duration=%.1fs",
                dispatch_id, entry.name, deployment.id, duration_s,
            )
        else:
            log.warning(
                "diagnose FAILED id=%d project=%s deployment=%d exit=%d "
                "duration=%.1fs log=%s",
                dispatch_id, entry.name, deployment.id, exit_code, duration_s,
                log_path,
            )

        await self._publish(
            {
                "kind": "dispatch_ended",
                "dispatch_id": dispatch_id,
                "project": entry.name,
                "issue": _NO_ISSUE,
                "stage": _DIAGNOSE_STAGE,
                "deployment_id": deployment.id,
                "exit_code": exit_code,
                "log_path": str(log_path),
            }
        )

        return DispatchResult(
            dispatch_id=dispatch_id,
            exit_code=exit_code,
            log_path=log_path,
            duration_s=duration_s,
        )

    def _build_diagnose_argv(
        self, *, model: str, project_path: str, bundle: dict[str, Any],
    ) -> list[str]:
        # The skill is auto-discovered from <project_path>/.claude/skills/
        # deploy-diagnose/SKILL.md. The prompt only carries the bundle —
        # the skill prose tells the agent what to do with it.
        bundle_json = json.dumps(bundle, indent=2, ensure_ascii=False)
        prompt = (
            "Run the deploy-diagnose skill against this failed deployment. "
            "The bundle below is the only structured input you have; the "
            "rest comes from reading the repo and gh.\n\n"
            "```json\n"
            f"{bundle_json}\n"
            "```\n"
        )
        return [
            "claude",
            "-p",
            "--model",
            model,
            "--permission-mode",
            "bypassPermissions",
            "--output-format",
            "stream-json",
            "--verbose",
            "--add-dir",
            project_path,
            "--",
            prompt,
        ]

    def _diagnose_log_path(self, project: str, deployment_id: int) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        return (
            self._home / "logs" / project / "_diagnose"
            / f"deploy-{deployment_id}-{ts}.log"
        )

    def _build_argv(self, *, model: str, project_path: str, stage: str, issue: int) -> list[str]:
        # `--add-dir <directories...>` is variadic, so the prompt must sit
        # behind a `--` separator — otherwise `claude` swallows it as
        # another directory and aborts with "Input must be provided …".
        # `bypassPermissions` is required for unattended dispatch: without
        # it, every tool call blocks on a TTY-less permission prompt and
        # the run produces no output.
        return [
            "claude",
            "-p",
            "--model",
            model,
            "--permission-mode",
            "bypassPermissions",
            # `stream-json --verbose` makes claude emit one JSON event per
            # tool call / assistant message / tool result, so the log on
            # disk is a full play-by-play (replay-as-interactive-session,
            # like the bash scripts using `tee` did). Without --verbose,
            # stream-json suppresses tool events.
            "--output-format",
            "stream-json",
            "--verbose",
            "--add-dir",
            project_path,
            "--",
            f"Run the {stage} skill on issue #{issue}.",
        ]

    def _log_path(self, project: str, issue: int, stage: str) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        return self._home / "logs" / project / str(issue) / f"{stage}-{ts}.log"

    def _read_cycle_from_gh(self, repo: str, issue: int) -> int:
        try:
            detail = gh.get_issue(repo, issue, runner=self._gh_runner)
        except gh.GhError:
            return 0
        for c in detail.comments:
            if not c.body.startswith(_CYCLE_SENTINEL):
                continue
            tail = c.body[len(_CYCLE_SENTINEL):].strip()
            try:
                return int(tail.splitlines()[0]) if tail else 0
            except ValueError:
                continue
        return 0

    async def _publish(self, event: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # slow subscriber: drop the oldest item to make room
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(event)


__all__ = [
    "Dispatcher",
    "DispatchError",
    "DispatchResult",
    "QuotaExceeded",
    "SubprocessSpawner",
]
