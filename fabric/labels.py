"""Canonical label vocabulary the pipeline expects.

`state:*` labels drive the dispatcher: scheduler.py's `_STAGE_BY_STATE`
keys are this module's state-label names. `priority:*` feeds the D3
sort. `type:*` is informational. Per-project `area:*` labels come from
`<project>/.fabric/config.yaml::labels.area_labels` — the fabric ships
the prefix only, the values are project-specific.

Used by `fabric setup-labels <project>` (Phase 1G) to converge a repo's
GitHub labels to this vocabulary idempotently.
"""

from __future__ import annotations

from dataclasses import dataclass

from fabric import github as gh


@dataclass(frozen=True)
class LabelSpec:
    name: str
    color: str  # 6-char hex, no leading '#'
    description: str = ""


# Pipeline state — every transition the scheduler/dispatcher cares about.
STATE_LABELS: list[LabelSpec] = [
    LabelSpec(
        "state:draft",
        "cccccc",
        "Author-marked WIP — agents ignore until label is removed",
    ),
    LabelSpec(
        "state:needs-decompose",
        "8b5cf6",
        "Epic awaiting decomposition into child issues",
    ),
    LabelSpec("state:needs-planning", "0e8a16", "Ready for plan-exec"),
    LabelSpec("state:in-progress", "fbca04", "Agent currently working"),
    LabelSpec("state:tests-pending", "1d76db", "Plan landed; tests next"),
    LabelSpec("state:in-review", "5319e7", "PR ready; review-pr will run"),
    LabelSpec("state:needs-rework", "d93f0b", "Review found issues; back to plan-exec"),
    LabelSpec(
        "state:clarification-needed",
        "ff9800",
        "Waiting on human input — reply to the TG notification",
    ),
    LabelSpec(
        "state:awaiting-decompose-approval",
        "9c27b0",
        "Decomposition awaits human approval",
    ),
    LabelSpec(
        "state:tracking",
        "b3e5fc",
        "Epic with children filed; auto-closes when all children close",
    ),
    LabelSpec(
        "state:blocked",
        "b60205",
        "Cycle cap or repeated failure; human intervention required",
    ),
    LabelSpec("state:done", "0e8a16", "Closed; pipeline complete"),
]

# Priority — D3 sort tier inside a state group.
PRIORITY_LABELS: list[LabelSpec] = [
    LabelSpec("priority:high", "b60205", "Drains the queue first"),
    LabelSpec("priority:medium", "fbca04", "Default priority"),
    LabelSpec("priority:low", "0e8a16", "Eligible for sonnet downgrade"),
]

# Type — informational; not pipeline-load-bearing. `type:epic` is the
# permanent marker on a parent issue; the transient state lives in the
# `state:*` family (`state:needs-decompose`, `state:tracking`, etc.).
TYPE_LABELS: list[LabelSpec] = [
    LabelSpec("type:feature", "1d76db", ""),
    LabelSpec("type:bug", "d93f0b", ""),
    LabelSpec("type:refactor", "5319e7", ""),
    LabelSpec("type:test", "0e8a16", ""),
    LabelSpec("type:docs", "fef2c0", ""),
    LabelSpec("type:epic", "8b5cf6", "Multi-PR initiative; child issues track the work"),
]


def area_labels(area_names: list[str]) -> list[LabelSpec]:
    """Build `area:<name>` specs from a project's `labels.area_labels` list."""
    return [
        LabelSpec(f"area:{name}", "c5def5", "")
        for name in area_names
    ]


def all_labels(area_names: list[str]) -> list[LabelSpec]:
    """The full label set for a project."""
    return [*STATE_LABELS, *PRIORITY_LABELS, *TYPE_LABELS, *area_labels(area_names)]


@dataclass(frozen=True)
class LabelDiff:
    to_create: list[LabelSpec]
    to_update: list[LabelSpec]
    unchanged: list[str]


def diff_labels(
    target: list[LabelSpec], existing: list[gh.LabelDetail]
) -> LabelDiff:
    """Compute the create/update/unchanged buckets given current + target."""
    by_name = {lbl.name: lbl for lbl in existing}
    to_create: list[LabelSpec] = []
    to_update: list[LabelSpec] = []
    unchanged: list[str] = []

    for spec in target:
        cur = by_name.get(spec.name)
        if cur is None:
            to_create.append(spec)
        elif cur.color.lower() != spec.color.lower() or cur.description != spec.description:
            to_update.append(spec)
        else:
            unchanged.append(spec.name)
    return LabelDiff(to_create=to_create, to_update=to_update, unchanged=unchanged)


def apply_label_diff(
    repo: str,
    diff: LabelDiff,
    *,
    runner: gh.SubprocessRunner | None = None,
) -> None:
    """Apply create/update calls via `gh label create|edit`."""
    runner = runner or gh._default_runner
    for spec in diff.to_create:
        gh.create_label(
            repo,
            spec.name,
            color=spec.color,
            description=spec.description,
            runner=runner,
        )
    for spec in diff.to_update:
        gh.edit_label(
            repo,
            spec.name,
            color=spec.color,
            description=spec.description,
            runner=runner,
        )


__all__ = [
    "LabelDiff",
    "LabelSpec",
    "PRIORITY_LABELS",
    "STATE_LABELS",
    "TYPE_LABELS",
    "all_labels",
    "apply_label_diff",
    "area_labels",
    "diff_labels",
]
