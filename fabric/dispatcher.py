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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fabric import github as gh
from fabric import state
from fabric.config import FabricConfig, load_project_config
from fabric.registry import ProjectEntry, fabric_home, find as find_project


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

        used = await asyncio.to_thread(state.quota_today, project)
        if used >= config.pipeline.daily_dispatch_cap:
            raise QuotaExceeded(
                f"{project} hit daily cap ({used}/{config.pipeline.daily_dispatch_cap})"
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
        )

        with log_path.open("w", encoding="utf-8") as logf:
            assert proc.stdout is not None
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

        exit_code = await proc.wait()
        ended = datetime.now(timezone.utc)

        await asyncio.to_thread(
            state.complete_dispatch,
            dispatch_id,
            ended_at=ended.isoformat(timespec="seconds"),
            exit_code=exit_code,
            log_path=str(log_path),
        )
        await asyncio.to_thread(state.inc_quota, entry.name)

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
            duration_s=(ended - started).total_seconds(),
        )

    def _build_argv(self, *, model: str, project_path: str, stage: str, issue: int) -> list[str]:
        return [
            "claude",
            "-p",
            "--model",
            model,
            "--add-dir",
            project_path,
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
