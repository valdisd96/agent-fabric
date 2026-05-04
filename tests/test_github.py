from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from fabric.github import (
    GhError,
    add_labels,
    comment,
    find_pr_for_issue,
    get_issue,
    list_issues,
    merge_pr,
    remove_labels,
    review_pr,
    set_html_comment,
)


@dataclass
class FakeResult:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class FakeRunner:
    """Records every invocation, returns canned responses in order."""

    responses: list[FakeResult] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)
    inputs: list[str | None] = field(default_factory=list)

    def __call__(self, args: list[str], **kwargs: Any) -> FakeResult:
        self.calls.append(list(args))
        self.inputs.append(kwargs.get("input"))
        if not self.responses:
            return FakeResult()
        return self.responses.pop(0)

    def queue(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.responses.append(FakeResult(returncode=returncode, stdout=stdout, stderr=stderr))


# ---------- error path ----------


def test_run_gh_raises_on_nonzero() -> None:
    r = FakeRunner()
    r.queue(returncode=1, stderr="not authenticated")
    with pytest.raises(GhError, match="not authenticated"):
        list_issues("me/r", runner=r)


def test_run_gh_json_raises_on_invalid_json() -> None:
    r = FakeRunner()
    r.queue(stdout="not-json")
    with pytest.raises(GhError, match="invalid JSON"):
        list_issues("me/r", runner=r)


# ---------- list_issues ----------


def test_list_issues_invokes_correct_argv() -> None:
    r = FakeRunner()
    r.queue(stdout="[]")
    list_issues("owner/repo", runner=r)
    assert r.calls[0] == [
        "gh",
        "issue",
        "list",
        "--repo",
        "owner/repo",
        "--state",
        "open",
        "--limit",
        "100",
        "--json",
        "number,title,labels,author,url,createdAt",
    ]


def test_list_issues_passes_label_query() -> None:
    r = FakeRunner()
    r.queue(stdout="[]")
    list_issues("owner/repo", label_query='label:"state:needs-planning"', runner=r)
    assert "--search" in r.calls[0]
    idx = r.calls[0].index("--search")
    assert r.calls[0][idx + 1] == 'label:"state:needs-planning"'


def test_list_issues_parses_camelcase_to_snake() -> None:
    payload = [
        {
            "number": 7,
            "title": "Hi",
            "url": "https://example/7",
            "createdAt": "2026-05-01T10:00:00Z",
            "labels": [{"name": "state:needs-planning"}],
            "author": {"login": "valdisd96"},
        }
    ]
    r = FakeRunner()
    r.queue(stdout=json.dumps(payload))
    issues = list_issues("me/r", runner=r)
    assert len(issues) == 1
    assert issues[0].number == 7
    assert issues[0].created_at == "2026-05-01T10:00:00Z"  # camelCase alias
    assert issues[0].labels[0].name == "state:needs-planning"
    assert issues[0].author and issues[0].author.login == "valdisd96"


def test_list_issues_ignores_unmodeled_fields() -> None:
    payload = [{"number": 1, "title": "x", "url": "u", "createdAt": "t", "extraField": 42}]
    r = FakeRunner()
    r.queue(stdout=json.dumps(payload))
    issues = list_issues("me/r", runner=r)
    assert issues[0].number == 1


def test_list_issues_rejects_non_list_response() -> None:
    r = FakeRunner()
    r.queue(stdout='{"oops": "object not list"}')
    with pytest.raises(GhError, match="expected list"):
        list_issues("me/r", runner=r)


# ---------- get_issue ----------


def test_get_issue_parses_full_detail() -> None:
    payload = {
        "number": 1,
        "title": "T",
        "body": "B",
        "url": "u",
        "createdAt": "t",
        "state": "OPEN",
        "labels": [],
        "author": {"login": "a"},
        "comments": [
            {"body": "hi", "author": {"login": "b"}, "createdAt": "t2", "url": "u#1"}
        ],
    }
    r = FakeRunner()
    r.queue(stdout=json.dumps(payload))
    issue = get_issue("me/r", 1, runner=r)
    assert issue.number == 1
    assert issue.body == "B"
    assert issue.state == "OPEN"
    assert len(issue.comments) == 1
    assert issue.comments[0].author and issue.comments[0].author.login == "b"


# ---------- add_labels / remove_labels ----------


def test_add_labels_emits_one_arg_per_label() -> None:
    r = FakeRunner()
    r.queue()
    add_labels("me/r", 1, ["state:in-progress", "type:feature"], runner=r)
    args = r.calls[0]
    assert args.count("--add-label") == 2
    flat = " ".join(args)
    assert "state:in-progress" in flat
    assert "type:feature" in flat


def test_add_labels_noop_on_empty_sequence() -> None:
    r = FakeRunner()
    add_labels("me/r", 1, [], runner=r)
    assert r.calls == []


def test_remove_labels_emits_one_arg_per_label() -> None:
    r = FakeRunner()
    r.queue()
    remove_labels("me/r", 1, ["state:needs-planning"], runner=r)
    assert r.calls[0].count("--remove-label") == 1


# ---------- comment ----------


def test_comment_returns_url_and_passes_body() -> None:
    r = FakeRunner()
    r.queue(stdout="https://github.com/me/r/issues/1#issuecomment-99\n")
    ref = comment("me/r", 1, "hello", runner=r)
    assert ref.url == "https://github.com/me/r/issues/1#issuecomment-99"
    args = r.calls[0]
    body_idx = args.index("--body")
    assert args[body_idx + 1] == "hello"


# ---------- find_pr_for_issue ----------


def test_find_pr_for_issue_returns_first_match() -> None:
    payload = [
        {
            "number": 5,
            "title": "Closes #1",
            "state": "OPEN",
            "headRefName": "feat/x",
            "url": "https://example/pr/5",
        }
    ]
    r = FakeRunner()
    r.queue(stdout=json.dumps(payload))
    pr = find_pr_for_issue("me/r", 1, runner=r)
    assert pr is not None
    assert pr.number == 5
    assert pr.head_ref_name == "feat/x"
    assert "in:body" in r.calls[0][r.calls[0].index("--search") + 1]


def test_find_pr_for_issue_returns_none_when_empty() -> None:
    r = FakeRunner()
    r.queue(stdout="[]")
    assert find_pr_for_issue("me/r", 1, runner=r) is None


# ---------- review_pr ----------


@pytest.mark.parametrize("action", ["approve", "request-changes", "comment"])
def test_review_pr_each_action_emits_correct_flag(action: str) -> None:
    r = FakeRunner()
    r.queue()
    review_pr("me/r", 1, action=action, body="lgtm", runner=r)
    assert f"--{action}" in r.calls[0]
    assert "lgtm" in r.calls[0]


def test_review_pr_invalid_action_raises() -> None:
    r = FakeRunner()
    with pytest.raises(GhError, match="unsupported review action"):
        review_pr("me/r", 1, action="nuke", runner=r)


def test_review_pr_omits_body_when_none() -> None:
    r = FakeRunner()
    r.queue()
    review_pr("me/r", 1, action="approve", body=None, runner=r)
    assert "--body" not in r.calls[0]


# ---------- merge_pr ----------


def test_merge_pr_default_squash_with_delete() -> None:
    r = FakeRunner()
    r.queue()
    merge_pr("me/r", 1, runner=r)
    args = r.calls[0]
    assert "--squash" in args
    assert "--delete-branch" in args


def test_merge_pr_can_skip_branch_delete() -> None:
    r = FakeRunner()
    r.queue()
    merge_pr("me/r", 1, delete_branch=False, runner=r)
    assert "--delete-branch" not in r.calls[0]


def test_merge_pr_unsupported_method_raises() -> None:
    r = FakeRunner()
    with pytest.raises(GhError, match="unsupported merge method"):
        merge_pr("me/r", 1, method="cherry-pick", runner=r)


# ---------- set_html_comment (idempotent marker) ----------


def test_set_html_comment_creates_when_no_marker() -> None:
    r = FakeRunner()
    r.queue(stdout=json.dumps({"comments": []}))  # issue view
    r.queue(stdout="https://example/c1\n")  # comment create
    ref = set_html_comment("me/r", 1, marker="cycle", value="3", runner=r)
    assert ref.url == "https://example/c1"
    # second call → comment, with sentinel + value
    body_arg = r.calls[1][r.calls[1].index("--body") + 1]
    assert body_arg.startswith("<!-- agent-fabric:cycle -->")
    assert body_arg.endswith("\n3")


def test_set_html_comment_edits_when_marker_exists() -> None:
    existing = {
        "comments": [
            {
                "body": "<!-- agent-fabric:cycle -->\nold",
                "url": "https://github.com/me/r/issues/1#issuecomment-42",
            }
        ]
    }
    r = FakeRunner()
    r.queue(stdout=json.dumps(existing))  # issue view
    r.queue(stdout="")  # api PATCH
    ref = set_html_comment("me/r", 1, marker="cycle", value="4", runner=r)
    assert ref.url == "https://github.com/me/r/issues/1#issuecomment-42"
    patch_args = r.calls[1]
    assert patch_args[:4] == ["gh", "api", "-X", "PATCH"]
    assert "/repos/me/r/issues/comments/42" in patch_args
    # body sent as JSON via stdin
    body_payload = json.loads(r.inputs[1] or "")
    assert body_payload["body"].startswith("<!-- agent-fabric:cycle -->")
    assert body_payload["body"].endswith("\n4")


def test_set_html_comment_ignores_other_markers() -> None:
    existing = {
        "comments": [
            {
                "body": "<!-- agent-fabric:other -->\nfoo",
                "url": "https://github.com/me/r/issues/1#issuecomment-99",
            }
        ]
    }
    r = FakeRunner()
    r.queue(stdout=json.dumps(existing))
    r.queue(stdout="https://example/new\n")
    set_html_comment("me/r", 1, marker="cycle", value="1", runner=r)
    # second call should be a fresh comment, not a PATCH
    assert "issue" in r.calls[1] and "comment" in r.calls[1]
