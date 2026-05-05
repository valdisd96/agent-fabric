from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from fabric import github as gh
from fabric.labels import (
    LabelSpec,
    PRIORITY_LABELS,
    STATE_LABELS,
    TYPE_LABELS,
    all_labels,
    apply_label_diff,
    area_labels,
    diff_labels,
)


# ---------- helpers ----------


@dataclass
class _GhRunResult:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class FakeRunner:
    responses: list[_GhRunResult] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, args: list[str], **kwargs: Any) -> _GhRunResult:
        self.calls.append(list(args))
        if not self.responses:
            return _GhRunResult()
        return self.responses.pop(0)

    def queue(self, stdout: str = "", returncode: int = 0) -> None:
        self.responses.append(_GhRunResult(returncode=returncode, stdout=stdout))


# ---------- shape ----------


def test_state_labels_cover_all_pipeline_states() -> None:
    """Every state the scheduler dispatches on must be in the canonical
    set. The `_UNQUALIFIED_STATE` pseudo-label is exempt — it lives only
    in fabric's DB and is never written to GitHub, so it doesn't need a
    canonical spec."""
    from fabric.scheduler import _STAGE_BY_STATE, _UNQUALIFIED_STATE

    spec_names = {s.name for s in STATE_LABELS}
    for state_label in _STAGE_BY_STATE:
        if state_label == _UNQUALIFIED_STATE:
            continue
        assert state_label in spec_names, f"missing canonical: {state_label}"


def test_draft_and_epic_states_are_canonical() -> None:
    """The triage states added with the qualify-issue stage must exist in
    the canonical set so `setup-labels` provisions them on every project."""
    spec_names = {s.name for s in STATE_LABELS}
    assert "state:draft" in spec_names
    assert "state:epic" in spec_names


def test_priority_labels_cover_all_dispatcher_priorities() -> None:
    from fabric.scheduler import _PRIORITY_RANK

    spec_names = {s.name for s in PRIORITY_LABELS}
    for priority in _PRIORITY_RANK:
        assert priority in spec_names


def test_all_labels_includes_areas() -> None:
    target = all_labels(["bot", "vocab"])
    names = {s.name for s in target}
    assert "area:bot" in names
    assert "area:vocab" in names
    assert "state:needs-planning" in names
    assert "priority:high" in names


def test_area_labels_emits_prefix() -> None:
    specs = area_labels(["a", "b"])
    assert [s.name for s in specs] == ["area:a", "area:b"]


# ---------- diff ----------


def _detail(name: str, color: str = "", description: str = "") -> gh.LabelDetail:
    return gh.LabelDetail(name=name, color=color, description=description)


def test_diff_creates_when_missing() -> None:
    target = [LabelSpec("priority:high", "b60205", "")]
    diff = diff_labels(target, [])
    assert [s.name for s in diff.to_create] == ["priority:high"]
    assert diff.to_update == []


def test_diff_updates_when_color_differs() -> None:
    target = [LabelSpec("priority:high", "b60205", "")]
    existing = [_detail("priority:high", color="ffffff")]
    diff = diff_labels(target, existing)
    assert diff.to_create == []
    assert [s.name for s in diff.to_update] == ["priority:high"]


def test_diff_updates_when_description_differs() -> None:
    target = [LabelSpec("priority:high", "b60205", "drains first")]
    existing = [_detail("priority:high", color="b60205", description="old")]
    diff = diff_labels(target, existing)
    assert [s.name for s in diff.to_update] == ["priority:high"]


def test_diff_unchanged_when_match() -> None:
    target = [LabelSpec("priority:high", "b60205", "drains first")]
    existing = [_detail("priority:high", color="b60205", description="drains first")]
    diff = diff_labels(target, existing)
    assert diff.to_create == []
    assert diff.to_update == []
    assert diff.unchanged == ["priority:high"]


def test_diff_color_compare_is_case_insensitive() -> None:
    """Match colors regardless of hex casing."""
    target = [LabelSpec("priority:high", "B60205", "")]
    existing = [_detail("priority:high", color="b60205")]
    diff = diff_labels(target, existing)
    assert diff.unchanged == ["priority:high"]


# ---------- apply ----------


def test_apply_label_diff_creates_and_edits() -> None:
    target = [
        LabelSpec("state:new", "0e8a16", "fresh"),
        LabelSpec("state:old", "fbca04", "updated desc"),
    ]
    existing = [_detail("state:old", color="fbca04", description="stale desc")]

    diff = diff_labels(target, existing)
    assert [s.name for s in diff.to_create] == ["state:new"]
    assert [s.name for s in diff.to_update] == ["state:old"]

    runner = FakeRunner()
    apply_label_diff("me/r", diff, runner=runner)

    create_calls = [c for c in runner.calls if c[1:3] == ["label", "create"]]
    edit_calls = [c for c in runner.calls if c[1:3] == ["label", "edit"]]
    assert len(create_calls) == 1
    assert len(edit_calls) == 1
    assert "state:new" in create_calls[0]
    assert "state:old" in edit_calls[0]


def test_apply_label_diff_no_calls_when_clean() -> None:
    runner = FakeRunner()
    apply_label_diff(
        "me/r",
        diff_labels(
            [LabelSpec("state:done", "0e8a16", "")],
            [_detail("state:done", color="0e8a16")],
        ),
        runner=runner,
    )
    assert runner.calls == []


# ---------- gh client surface ----------


def test_list_labels_invokes_correct_argv() -> None:
    runner = FakeRunner()
    runner.queue(stdout="[]")
    gh.list_labels("me/r", runner=runner)
    assert runner.calls[0][:5] == ["gh", "label", "list", "--repo", "me/r"]


def test_create_label_emits_color_and_description() -> None:
    runner = FakeRunner()
    runner.queue()
    gh.create_label("me/r", "x", color="abcdef", description="hi", runner=runner)
    args = runner.calls[0]
    assert "--color" in args and "abcdef" in args
    assert "--description" in args and "hi" in args


def test_edit_label_skips_when_nothing_changed() -> None:
    runner = FakeRunner()
    gh.edit_label("me/r", "x", runner=runner)  # color=None, description=None
    assert runner.calls == []


def test_list_labels_parses_camelcase() -> None:
    payload = [{"name": "n", "color": "abcdef", "description": "d"}]
    runner = FakeRunner()
    runner.queue(stdout=json.dumps(payload))
    rows = gh.list_labels("me/r", runner=runner)
    assert len(rows) == 1
    assert rows[0].name == "n"
    assert rows[0].color == "abcdef"
