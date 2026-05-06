"""Pydantic v2 request/response shapes for the FastAPI surface.

Kept separate from `fabric/github.py` and `fabric/state.py` so the REST
contract can evolve without dragging the gh JSON shape or the SQLite row
shape along with it. The Telegram bot (1F) and the future web dashboard
(Phase 2) both consume this module.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- response shapes ----


class ProjectOut(_ApiModel):
    name: str
    path: str
    repo: str
    fabric_version: str | None = None
    last_served_at: str | None = None
    paused: bool = False


class IssueSummaryOut(_ApiModel):
    project: str
    number: int
    state_label: str | None = None
    type_label: str | None = None
    priority_label: str | None = None
    area_label: str | None = None
    title: str | None = None
    url: str | None = None
    cycle_count: int = 0
    author: str | None = None
    created_at: str | None = None


class DispatchOut(_ApiModel):
    id: int
    project: str
    issue: int
    stage: str
    started_at: str
    ended_at: str | None = None
    exit_code: int | None = None
    log_path: str | None = None
    triggered_by: str


class NotificationOut(_ApiModel):
    id: int
    kind: str
    project: str
    issue: int
    created_at: str
    delivered_to: str | None = None
    acknowledged_at: str | None = None


class StatusOut(_ApiModel):
    paused: bool
    paused_reason: str | None = None
    projects: int
    actionable_issues: int


class LogOut(_ApiModel):
    log_path: str
    content: str


class CommentOut(_ApiModel):
    url: str


class OkOut(_ApiModel):
    ok: bool = True


class DispatchSubmittedOut(_ApiModel):
    dispatch_id: int
    exit_code: int
    log_path: str
    duration_s: float


class DeploymentOut(_ApiModel):
    id: int
    project: str
    sha: str
    short_sha: str | None = None
    status: str
    deployed_at: str
    branch: str | None = None
    actor: str | None = None
    workflow_run_url: str | None = None
    service_unit: str | None = None
    journal_tail: str | None = None
    diagnose_dispatch_id: int | None = None


# ---- request shapes ----


class CommentRequest(_ApiModel):
    body: str


class LabelRequest(_ApiModel):
    add: list[str] = []
    remove: list[str] = []


class DispatchRequest(_ApiModel):
    stage: str
    model: str | None = None


class ReviewRequest(_ApiModel):
    action: str
    body: str | None = None


class MergeRequest(_ApiModel):
    method: str = "squash"
    delete_branch: bool = True


class PauseRequest(_ApiModel):
    reason: str | None = None


class DeploymentRecordRequest(_ApiModel):
    """Body posted by the deploy workflow on a successful deploy.

    Mirrors the JSON written to `/var/lib/<project>/deploy.json`.
    """
    sha: str
    short_sha: str | None = None
    deployed_at: str
    branch: str | None = None
    actor: str | None = None
    workflow_run_url: str | None = None


class DeployFailureRequest(_ApiModel):
    """Body posted by the deploy workflow when a deploy fails.

    The fabric records the failure and (forward-looking) auto-dispatches
    the `deploy-diagnose` skill against this bundle.
    """
    sha: str
    workflow_run_url: str | None = None
    service_unit: str | None = None
    journal_tail: str | None = None
    status: str = "failed"


__all__ = [
    "CommentOut",
    "CommentRequest",
    "DeployFailureRequest",
    "DeploymentOut",
    "DeploymentRecordRequest",
    "DispatchOut",
    "DispatchRequest",
    "DispatchSubmittedOut",
    "IssueSummaryOut",
    "LabelRequest",
    "LogOut",
    "MergeRequest",
    "NotificationOut",
    "OkOut",
    "PauseRequest",
    "ProjectOut",
    "ReviewRequest",
    "StatusOut",
]
