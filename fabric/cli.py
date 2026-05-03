"""`fabric` CLI entry point.

Phase 0A wires all eight subcommands listed in DESIGN.md "CLI modes" so the
surface is discoverable; only `register` does real work this milestone.
The rest stub out with exit code 2 so accidental invocations fail loudly.
"""

from __future__ import annotations

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
    _stub("tick")


@app.command()
def dispatch(
    project: str = typer.Argument(...),
    issue: int = typer.Argument(...),
    stage: str = typer.Argument(...),
) -> None:
    """Force-dispatch an agent stage on an issue."""
    _stub("dispatch")


@app.command()
def status() -> None:
    """Text dump of current queue state."""
    _stub("status")


@app.command()
def pause(reason: str = typer.Option("", "--reason", help="Pause reason recorded in state.")) -> None:
    """Set the global pause flag."""
    _stub("pause")


@app.command()
def resume() -> None:
    """Clear the global pause flag."""
    _stub("resume")


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
