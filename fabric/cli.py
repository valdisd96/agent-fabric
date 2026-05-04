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
    import os

    from fabric.state import init_db, latest_dispatch

    init_db()
    d = latest_dispatch(project, issue)
    if d is None or d.log_path is None:
        typer.echo(f"logs: no dispatch found for {project}#{issue}", err=True)
        raise typer.Exit(1)
    log_path = Path(d.log_path)
    if not log_path.exists():
        typer.echo(f"logs: log file missing: {d.log_path}", err=True)
        raise typer.Exit(1)
    if follow:
        os.execvp("tail", ["tail", "-f", str(log_path)])
    typer.echo(log_path.read_text(), nl=False)


@app.command(name="setup-labels")
def setup_labels(
    project: str = typer.Argument(..., help="Project name (must be registered)"),
    check: bool = typer.Option(
        False, "--check", help="Print planned changes without writing"
    ),
) -> None:
    """Idempotently provision the canonical label set in a project's repo."""
    from fabric import github as gh
    from fabric.config import ConfigError, load_project_config
    from fabric.labels import all_labels, apply_label_diff, diff_labels
    from fabric.registry import find as find_project

    entry = find_project(project)
    if entry is None:
        typer.echo(f"setup-labels: project {project!r} not registered", err=True)
        raise typer.Exit(1)
    try:
        config = load_project_config(entry.path)
    except ConfigError as e:
        typer.echo(f"setup-labels: {e}", err=True)
        raise typer.Exit(1) from e

    target = all_labels(config.labels.area_labels)
    try:
        existing = gh.list_labels(entry.repo)
    except gh.GhError as e:
        typer.echo(f"setup-labels: {e}", err=True)
        raise typer.Exit(2) from e

    diff = diff_labels(target, existing)

    for spec in diff.to_create:
        typer.echo(f"  create: {spec.name} ({spec.color})")
    for spec in diff.to_update:
        typer.echo(f"  update: {spec.name} ({spec.color})")
    typer.echo(f"  ({len(diff.unchanged)} unchanged)")

    if check:
        return

    try:
        apply_label_diff(entry.repo, diff)
    except gh.GhError as e:
        typer.echo(f"setup-labels: {e}", err=True)
        raise typer.Exit(2) from e

    typer.echo(
        f"setup-labels: created {len(diff.to_create)}, "
        f"updated {len(diff.to_update)}, unchanged {len(diff.unchanged)}"
    )


@app.command()
def server(
    host: str = typer.Option("127.0.0.1", "--host", envvar="FABRIC_HOST"),
    port: int = typer.Option(7878, "--port", envvar="FABRIC_PORT"),
) -> None:
    """Run the long-running fabric service (REST + WS + scheduler tick)."""
    import os

    import uvicorn

    from fabric.dispatcher import Dispatcher
    from fabric.scheduler import Scheduler
    from fabric.server import create_app
    from fabric.state import init_db

    init_db()
    dispatcher = Dispatcher()
    scheduler = Scheduler(dispatcher=dispatcher)

    tg_factory = _make_telegram_factory(host=host, port=port)
    web_app = create_app(
        scheduler=scheduler, dispatcher=dispatcher, telegram_factory=tg_factory
    )
    uvicorn.run(web_app, host=host, port=port, log_level="info")


def _make_telegram_factory(*, host: str, port: int):  # type: ignore[no-untyped-def]
    """Return a no-arg factory that constructs+starts a TelegramBot, or None.

    Disabled (with a warning at server startup) if either env var is missing.
    """
    import os

    token = os.environ.get("FABRIC_TELEGRAM_TOKEN")
    chat_id = os.environ.get("FABRIC_TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        typer.echo(
            "telegram: disabled — set FABRIC_TELEGRAM_TOKEN and FABRIC_TELEGRAM_CHAT_ID",
            err=True,
        )
        return None
    try:
        chat_id_int = int(chat_id)
    except ValueError:
        typer.echo(
            f"telegram: invalid FABRIC_TELEGRAM_CHAT_ID={chat_id!r}", err=True
        )
        return None

    rest_url = f"http://{host}:{port}"

    async def factory():  # type: ignore[no-untyped-def]
        from fabric.telegram_bot import TelegramBot

        bot = TelegramBot(
            token=token, chat_id=chat_id_int, rest_base_url=rest_url
        )
        await bot.start()
        return bot

    return factory


if __name__ == "__main__":
    app()
