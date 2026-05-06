"""FastAPI surface — REST + WS + the 60s scheduler tick loop.

Constructed via `create_app(scheduler=..., dispatcher=...)` so tests
can wire fakes without monkey-patching. The scheduler tick lives in
the FastAPI lifespan: kicks off once at startup, then loops on
`asyncio.wait_for(stop_event.wait(), timeout=tick_interval_s)` so a
shutdown wakes the sleeper instead of waiting out the full interval.

No auth in this phase — single-user, local. Phase 2 adds basic auth
behind Tailscale.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.websockets import WebSocketDisconnect

from fabric import api_models as am
from fabric import github as gh
from fabric import state
from fabric.dispatcher import Dispatcher, DispatchError, QuotaExceeded
from fabric.registry import find as find_project
from fabric.scheduler import Scheduler


log = logging.getLogger("fabric.server")


TelegramBotFactory = Any  # callable returning an awaitable that yields a bot with stop()


def create_app(
    *,
    scheduler: Scheduler,
    dispatcher: Dispatcher,
    gh_runner: gh.SubprocessRunner | None = None,
    tick_interval_s: float = 60.0,
    telegram_factory: TelegramBotFactory | None = None,
) -> FastAPI:
    runner: gh.SubprocessRunner = gh_runner or gh._default_runner

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop_event = asyncio.Event()
        task = asyncio.create_task(_tick_loop(scheduler, stop_event, tick_interval_s))
        tg_bot = None
        if telegram_factory is not None:
            try:
                tg_bot = await telegram_factory()
            except Exception:
                log.exception("telegram factory failed; bot disabled")
        try:
            yield
        finally:
            stop_event.set()
            await task
            if tg_bot is not None:
                try:
                    await tg_bot.stop()
                except Exception:
                    log.exception("telegram bot shutdown failed")

    app = FastAPI(title="agent-fabric", lifespan=lifespan)
    app.state.scheduler = scheduler
    app.state.dispatcher = dispatcher
    _wire_routes(app, dispatcher=dispatcher, gh_runner=runner)
    return app


async def _tick_loop(
    scheduler: Scheduler, stop_event: asyncio.Event, interval_s: float
) -> None:
    while not stop_event.is_set():
        try:
            await scheduler.tick()
        except Exception:
            log.exception("scheduler tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
            return
        except asyncio.TimeoutError:
            continue


def _issue_to_out(r: state.IssueRow) -> am.IssueSummaryOut:
    return am.IssueSummaryOut(
        project=r.project,
        number=r.number,
        state_label=r.state_label,
        type_label=r.type_label,
        priority_label=r.priority_label,
        area_label=r.area_label,
        title=r.title,
        url=r.url,
        cycle_count=r.cycle_count,
        author=r.author,
        created_at=r.created_at,
    )


def _project_to_out(r: state.ProjectRow) -> am.ProjectOut:
    return am.ProjectOut(
        name=r.name,
        path=r.path,
        repo=r.repo,
        fabric_version=r.fabric_version,
        last_served_at=r.last_served_at,
        paused=r.paused,
    )


def _dispatch_to_out(r: state.DispatchRow) -> am.DispatchOut:
    return am.DispatchOut(
        id=r.id,
        project=r.project,
        issue=r.issue,
        stage=r.stage,
        started_at=r.started_at,
        ended_at=r.ended_at,
        exit_code=r.exit_code,
        log_path=r.log_path,
        triggered_by=r.triggered_by,
    )


def _deployment_to_out(r: state.DeploymentRow) -> am.DeploymentOut:
    return am.DeploymentOut(
        id=r.id,
        project=r.project,
        sha=r.sha,
        short_sha=r.short_sha,
        status=r.status,
        deployed_at=r.deployed_at,
        branch=r.branch,
        actor=r.actor,
        workflow_run_url=r.workflow_run_url,
        service_unit=r.service_unit,
        journal_tail=r.journal_tail,
        diagnose_dispatch_id=r.diagnose_dispatch_id,
    )


def _wire_routes(
    app: FastAPI,
    *,
    dispatcher: Dispatcher,
    gh_runner: gh.SubprocessRunner,
) -> None:

    # ---- liveness ----

    @app.get("/healthz")
    async def healthz() -> dict[str, bool | str]:
        """Liveness probe — never touches state.db / gh / claude.
        Reverse proxies and uptime monitors hit this hot, so keep it
        synchronous and dependency-free."""
        return {"ok": True}

    # ---- read paths ----

    @app.get("/api/projects", response_model=list[am.ProjectOut])
    async def list_projects() -> list[am.ProjectOut]:
        rows = await asyncio.to_thread(state.list_projects)
        return [_project_to_out(r) for r in rows]

    @app.get(
        "/api/projects/{project}/issues", response_model=list[am.IssueSummaryOut]
    )
    async def project_issues(project: str) -> list[am.IssueSummaryOut]:
        rows = await asyncio.to_thread(state.list_issues, project)
        return [_issue_to_out(r) for r in rows]

    @app.get("/api/issues", response_model=list[am.IssueSummaryOut])
    async def all_issues() -> list[am.IssueSummaryOut]:
        rows = await asyncio.to_thread(state.list_issues)
        return [_issue_to_out(r) for r in rows]

    @app.get(
        "/api/issues/{project}/{number}", response_model=am.IssueSummaryOut
    )
    async def get_issue(project: str, number: int) -> am.IssueSummaryOut:
        row = await asyncio.to_thread(state.get_issue, project, number)
        if row is None:
            raise HTTPException(404, f"issue {project}#{number} not found")
        return _issue_to_out(row)

    @app.get(
        "/api/projects/{project}/deployments",
        response_model=list[am.DeploymentOut],
    )
    async def list_deployments(
        project: str, limit: int = 50, status: str | None = None
    ) -> list[am.DeploymentOut]:
        _require_project(project)
        rows = await asyncio.to_thread(
            state.list_deployments, project, limit=limit, status=status
        )
        return [_deployment_to_out(r) for r in rows]

    @app.get(
        "/api/projects/{project}/deployments/latest",
        response_model=am.DeploymentOut,
    )
    async def latest_deployment(
        project: str, status: str | None = "success"
    ) -> am.DeploymentOut:
        """Return the latest deployment for `project`. Defaults to status='success'
        — i.e. "what is currently running" — but accepts `?status=failed` or
        `?status=` (blank) to query the most recent attempt of any status."""
        _require_project(project)
        normalized = status if status else None
        row = await asyncio.to_thread(
            state.latest_deployment, project, status=normalized
        )
        if row is None:
            qualifier = f" with status={normalized}" if normalized else ""
            raise HTTPException(
                404, f"no deployments recorded for {project}{qualifier}"
            )
        return _deployment_to_out(row)

    @app.post(
        "/api/projects/{project}/deployments",
        response_model=am.DeploymentOut,
    )
    async def post_deployment(
        project: str, req: am.DeploymentRecordRequest
    ) -> am.DeploymentOut:
        """Record a successful deploy. Posted by the deploy workflow after
        the smoke check passes (see examples/github-actions/deploy.yml)."""
        _require_project(project)
        deployment = await asyncio.to_thread(
            state.record_deployment,
            project=project,
            sha=req.sha,
            short_sha=req.short_sha,
            status="success",
            deployed_at=req.deployed_at,
            branch=req.branch,
            actor=req.actor,
            workflow_run_url=req.workflow_run_url,
        )
        return _deployment_to_out(deployment)

    @app.post(
        "/api/projects/{project}/deploy-failures",
        response_model=am.DeploymentOut,
    )
    async def post_deploy_failure(
        project: str, req: am.DeployFailureRequest
    ) -> am.DeploymentOut:
        """Record a failed deploy. The deploy-diagnose skill auto-dispatch
        against this row is a follow-up — for now we just persist the bundle
        so the future diagnose pass has the data it needs."""
        _require_project(project)
        deployment = await asyncio.to_thread(
            state.record_deployment,
            project=project,
            sha=req.sha,
            status="failed",
            workflow_run_url=req.workflow_run_url,
            service_unit=req.service_unit,
            journal_tail=req.journal_tail,
        )
        log.warning(
            "deploy failed: project=%s sha=%s deployment_id=%s "
            "— diagnose dispatch wiring pending",
            project,
            req.sha,
            deployment.id,
        )
        return _deployment_to_out(deployment)

    @app.get("/api/dispatches", response_model=list[am.DispatchOut])
    async def list_dispatches(
        project: str | None = None, limit: int = 50
    ) -> list[am.DispatchOut]:
        rows = await asyncio.to_thread(
            state.recent_dispatches, project=project, limit=limit
        )
        return [_dispatch_to_out(r) for r in rows]

    @app.get("/api/dispatches/{dispatch_id}/log", response_model=am.LogOut)
    async def get_log(dispatch_id: int, tail: int | None = None) -> am.LogOut:
        # Linear scan over recent dispatches — fine at our scale (10s/day).
        rows = await asyncio.to_thread(state.recent_dispatches, limit=10000)
        row = next((r for r in rows if r.id == dispatch_id), None)
        if row is None or row.log_path is None:
            raise HTTPException(404, f"dispatch {dispatch_id} has no log")
        path = Path(row.log_path)
        if not path.exists():
            raise HTTPException(404, f"log file missing: {row.log_path}")
        text = await asyncio.to_thread(path.read_text)
        if tail is not None and tail > 0:
            text = "\n".join(text.splitlines()[-tail:])
        return am.LogOut(log_path=row.log_path, content=text)

    @app.get("/api/status", response_model=am.StatusOut)
    async def status() -> am.StatusOut:
        paused = (await asyncio.to_thread(state.get_setting, "paused")) == "1"
        reason = await asyncio.to_thread(state.get_setting, "paused_reason")
        projects = await asyncio.to_thread(state.list_projects)
        issues = await asyncio.to_thread(state.list_issues)
        actionable = sum(
            1
            for i in issues
            if i.state_label and i.state_label.startswith("state:")
        )
        return am.StatusOut(
            paused=paused,
            paused_reason=reason,
            projects=len(projects),
            actionable_issues=actionable,
        )

    # ---- write paths ----

    @app.post(
        "/api/issues/{project}/{number}/comment", response_model=am.CommentOut
    )
    async def post_comment(
        project: str, number: int, req: am.CommentRequest
    ) -> am.CommentOut:
        repo = _resolve_repo(project)
        try:
            ref = await asyncio.to_thread(
                gh.comment, repo, number, req.body, runner=gh_runner
            )
        except gh.GhError as e:
            raise HTTPException(502, str(e)) from e
        return am.CommentOut(url=ref.url)

    @app.post("/api/issues/{project}/{number}/label", response_model=am.OkOut)
    async def post_labels(
        project: str, number: int, req: am.LabelRequest
    ) -> am.OkOut:
        repo = _resolve_repo(project)
        try:
            if req.add:
                await asyncio.to_thread(
                    gh.add_labels, repo, number, req.add, runner=gh_runner
                )
            if req.remove:
                await asyncio.to_thread(
                    gh.remove_labels, repo, number, req.remove, runner=gh_runner
                )
        except gh.GhError as e:
            raise HTTPException(502, str(e)) from e
        return am.OkOut()

    @app.post(
        "/api/issues/{project}/{number}/dispatch",
        response_model=am.DispatchSubmittedOut,
    )
    async def post_dispatch(
        project: str, number: int, req: am.DispatchRequest
    ) -> am.DispatchSubmittedOut:
        try:
            result = await dispatcher.dispatch(
                project,
                number,
                req.stage,
                model=req.model,
                triggered_by="manual:api",
            )
        except QuotaExceeded as e:
            raise HTTPException(429, str(e)) from e
        except DispatchError as e:
            raise HTTPException(404, str(e)) from e
        return am.DispatchSubmittedOut(
            dispatch_id=result.dispatch_id,
            exit_code=result.exit_code,
            log_path=str(result.log_path),
            duration_s=result.duration_s,
        )

    @app.post(
        "/api/prs/{project}/{pr_number}/review", response_model=am.OkOut
    )
    async def post_review(
        project: str, pr_number: int, req: am.ReviewRequest
    ) -> am.OkOut:
        repo = _resolve_repo(project)
        try:
            await asyncio.to_thread(
                gh.review_pr,
                repo,
                pr_number,
                action=req.action,
                body=req.body,
                runner=gh_runner,
            )
        except gh.GhError as e:
            raise HTTPException(400, str(e)) from e
        return am.OkOut()

    @app.post(
        "/api/prs/{project}/{pr_number}/merge", response_model=am.OkOut
    )
    async def post_merge(
        project: str, pr_number: int, req: am.MergeRequest
    ) -> am.OkOut:
        repo = _resolve_repo(project)
        try:
            await asyncio.to_thread(
                gh.merge_pr,
                repo,
                pr_number,
                method=req.method,
                delete_branch=req.delete_branch,
                runner=gh_runner,
            )
        except gh.GhError as e:
            raise HTTPException(400, str(e)) from e
        return am.OkOut()

    @app.post("/api/pause", response_model=am.OkOut)
    async def pause(req: am.PauseRequest) -> am.OkOut:
        await asyncio.to_thread(state.set_setting, "paused", "1")
        if req.reason:
            await asyncio.to_thread(state.set_setting, "paused_reason", req.reason)
        else:
            await asyncio.to_thread(state.delete_setting, "paused_reason")
        return am.OkOut()

    @app.post("/api/resume", response_model=am.OkOut)
    async def resume() -> am.OkOut:
        await asyncio.to_thread(state.set_setting, "paused", "0")
        await asyncio.to_thread(state.delete_setting, "paused_reason")
        return am.OkOut()

    # ---- WebSocket ----

    @app.websocket("/ws/live")
    async def ws_live(websocket: WebSocket) -> None:
        await websocket.accept()
        queue = dispatcher.subscribe()
        try:
            while True:
                event = await queue.get()
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        finally:
            dispatcher.unsubscribe(queue)


def _resolve_repo(project: str) -> str:
    entry = find_project(project)
    if entry is None:
        raise HTTPException(404, f"project {project!r} not registered")
    return entry.repo


def _require_project(project: str) -> None:
    """404 if the project name isn't registered. Used on routes that don't
    need the repo string but still want to fail loudly on typos."""
    if find_project(project) is None:
        raise HTTPException(404, f"project {project!r} not registered")


__all__ = ["create_app"]
