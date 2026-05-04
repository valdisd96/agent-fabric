"""`fabric` CLI entry point.

Phase 0A wired all eight subcommands so the surface is discoverable;
each lights up as its phase ships. As of Phase 1C, `dispatch` is real;
`tick`, `status`, `pause`, `resume`, `logs` still stub out.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from fabric.registry import RegistryError, register as registry_register
from fabric.sync import SyncError, sync as run_sync

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Multi-project agent fabric CLI.",
)

_NOT_IMPLEMENTED_EXIT = 2


def _stub(name: str) -> None:
    typer.echo(f"fabric {name}: not implemented in phase 0", err=True)
    raise typer.Exit(_NOT_IMPLEMENTED_EXIT)


@app.command()
def register(
    repo_path: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Path to a project containing a valid .fabric/config.yaml",
    ),
) -> None:
    """Add a project to ~/.fabric/projects.yaml."""
    try:
        result = registry_register(repo_path)
    except RegistryError as e:
        typer.echo(f"register: {e}", err=True)
        raise typer.Exit(1) from e
    verb = "updated" if result.replaced else "registered"
    typer.echo(f"{verb} {result.entry.name} -> {result.entry.path} ({result.entry.repo})")


@app.command()
def sync(
    project: str = typer.Argument(..., help="Project name or path"),
    check: bool = typer.Option(
        False,
        "--check",
        help="Exit 1 on drift; do not write. Use in CI.",
    ),
) -> None:
    """Re-render skill templates into the project's .claude/skills/.

    Exit codes: 0 clean (or wrote files), 1 drift (--check only),
    2 error (config invalid, project not found, template missing).
    """
    try:
        result = run_sync(project, check=check)
    except SyncError as e:
        typer.echo(f"sync: {e}", err=True)
        raise typer.Exit(2) from e

    if check:
        if not result.drift:
            typer.echo(f"sync: {project} is clean")
            return
        for d in result.drift:
            typer.echo(d.unified_diff(result.project_path))
        files = ", ".join(d.name for d in result.drift)
        typer.echo(f"sync: drift detected in {len(result.drift)} skill(s): {files}", err=True)
        raise typer.Exit(1)

    if result.written:
        for name in result.written:
            typer.echo(f"wrote {name}")
        typer.echo(f"sync: {len(result.written)} skill(s) updated")
    else:
        typer.echo(f"sync: {project} already up to date")


@app.command()
def tick() -> None:
    """One-shot poll-and-dispatch (debug)."""
    from fabric.dispatcher import Dispatcher
    from fabric.scheduler import Scheduler
    from fabric.state import init_db

    init_db()
    scheduler = Scheduler(dispatcher=Dispatcher())
    result = asyncio.run(scheduler.tick())

    if result.skipped_reason:
        typer.echo(f"tick: skipped — {result.skipped_reason}")
        return
    if result.winner is None:
        typer.echo(f"tick: no actionable issues (polled {result.polled_projects} project(s))")
        return
    w = result.winner
    typer.echo(
        f"tick: dispatched {w.project}#{w.issue} ({w.stage}) "
        f"id={w.dispatch_id} exit={w.exit_code}"
    )


@app.command()
def dispatch(
    project: str = typer.Argument(...),
    issue: int = typer.Argument(...),
    stage: str = typer.Argument(...),
    model: str = typer.Option(
        "",
        "--model",
        help="Override model. Empty = default + downgrade rules.",
    ),
) -> None:
    """Force-dispatch an agent stage on an issue."""
    from fabric.dispatcher import Dispatcher, DispatchError
    from fabric.state import init_db

    init_db()
    dispatcher = Dispatcher()
    try:
        result = asyncio.run(
            dispatcher.dispatch(
                project,
                issue,
                stage,
                model=model or None,
                triggered_by="manual:cli",
            )
        )
    except DispatchError as e:
        typer.echo(f"dispatch: {e}", err=True)
        raise typer.Exit(1) from e

    typer.echo(
        f"dispatch: id={result.dispatch_id} exit={result.exit_code} "
        f"duration={result.duration_s:.1f}s log={result.log_path}"
    )


@app.command()
def status() -> None:
    """Text dump of current queue state."""
    from fabric.state import (
        get_setting,
        init_db,
        list_issues,
        list_projects,
        recent_dispatches,
    )

    init_db()
    paused = get_setting("paused") == "1"
    reason = get_setting("paused_reason") or ""
    pause_line = "paused: yes" if paused else "paused: no"
    if paused and reason:
        pause_line += f" — {reason}"
    typer.echo(pause_line)

    projects = list_projects()
    if not projects:
        typer.echo("(no registered projects)")
    else:
        typer.echo("")
        typer.echo("Projects:")
        for p in projects:
            issues = list_issues(p.name)
            actionable = [i for i in issues if i.state_label and i.state_label.startswith("state:")]
            tag = " [paused]" if p.paused else ""
            typer.echo(f"  {p.name}{tag}: {len(actionable)} issue(s) tracked")

    typer.echo("")
    typer.echo("Recent dispatches:")
    rows = recent_dispatches(limit=5)
    if not rows:
        typer.echo("  (none)")
    for d in rows:
        ended = d.ended_at or "running"
        typer.echo(
            f"  {d.started_at}  {d.project}#{d.issue}  {d.stage}  "
            f"exit={d.exit_code}  ended={ended}"
        )


@app.command()
def pause(reason: str = typer.Option("", "--reason", help="Pause reason recorded in state.")) -> None:
    """Set the global pause flag."""
    from fabric.state import delete_setting, init_db, set_setting

    init_db()
    set_setting("paused", "1")
    if reason:
        set_setting("paused_reason", reason)
    else:
        delete_setting("paused_reason")
    typer.echo(f"fabric: paused" + (f" ({reason})" if reason else ""))


@app.command()
def resume() -> None:
    """Clear the global pause flag."""
    from fabric.state import delete_setting, init_db, set_setting

    init_db()
    set_setting("paused", "0")
    delete_setting("paused_reason")
    typer.echo("fabric: resumed")


@app.command()
def logs(
    project: str = typer.Argument(...),
    issue: int = typer.Argument(...),
    follow: bool = typer.Option(False, "--follow", "-f"),
) -> None:
    """Tail the latest agent log for an issue."""
    _stub("logs")


if __name__ == "__main__":
    app()
