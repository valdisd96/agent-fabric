"""Thin sync wrapper around the `gh` CLI.

We invoke `gh` with explicit `--repo owner/name` so cwd doesn't matter,
and rely on `gh auth login` having been done out of band — credential
management isn't this module's job.

The subprocess runner is injectable via the `runner` keyword argument
on every public function so tests can pass a fake without monkeypatching
(see `dev-flow` SKILL: "Inject collaborators as keyword args"). Async
callers (1C dispatcher, 1D scheduler, 1E server) wrap each call in
`asyncio.to_thread` rather than colour the API with `await`.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class GhError(Exception):
    """`gh` returned non-zero, or its stdout couldn't be parsed."""


SubprocessRunner = Callable[..., "subprocess.CompletedProcess[str]"]


def _default_runner(args: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False, **kwargs)


def _run_gh(
    args: Sequence[str],
    *,
    stdin: str | None = None,
    runner: SubprocessRunner = _default_runner,
) -> str:
    kwargs: dict[str, Any] = {}
    if stdin is not None:
        kwargs["input"] = stdin
    result = runner(["gh", *args], **kwargs)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise GhError(f"gh {' '.join(args)} -> exit {result.returncode}: {stderr}")
    return result.stdout


def _run_gh_json(
    args: Sequence[str],
    *,
    runner: SubprocessRunner = _default_runner,
) -> Any:
    out = _run_gh(args, runner=runner)
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise GhError(f"gh {' '.join(args)} -> invalid JSON: {e}") from e


# ---- output models ----


class _GhModel(BaseModel):
    """Pydantic base: snake_case attrs, camelCase aliases (gh's JSON style)."""

    model_config = ConfigDict(
        frozen=True,
        extra="ignore",  # gh adds fields we don't model — let them pass
        alias_generator=to_camel,
        populate_by_name=True,
    )


class Author(_GhModel):
    login: str = ""


class Label(_GhModel):
    name: str


class IssueComment(_GhModel):
    body: str = ""
    author: Author | None = None
    created_at: str = ""
    url: str = ""


class IssueSummary(_GhModel):
    number: int
    title: str = ""
    labels: list[Label] = []
    author: Author | None = None
    url: str = ""
    created_at: str = ""


class IssueDetail(IssueSummary):
    body: str = ""
    state: str = ""
    comments: list[IssueComment] = []


class PRSummary(_GhModel):
    number: int
    title: str = ""
    state: str = ""
    head_ref_name: str = ""
    url: str = ""


class CommentRef(_GhModel):
    url: str = ""


# ---- public API ----


def list_issues(
    repo: str,
    *,
    label_query: str | None = None,
    state: str = "open",
    limit: int = 100,
    runner: SubprocessRunner = _default_runner,
) -> list[IssueSummary]:
    """List issues in `repo`. `label_query` is a GH search expression."""
    args = [
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        state,
        "--limit",
        str(limit),
        "--json",
        "number,title,labels,author,url,createdAt",
    ]
    if label_query:
        args += ["--search", label_query]
    raw = _run_gh_json(args, runner=runner)
    if not isinstance(raw, list):
        raise GhError(f"expected list from `gh issue list`, got {type(raw).__name__}")
    return [IssueSummary.model_validate(item) for item in raw]


def get_issue(
    repo: str,
    number: int,
    *,
    runner: SubprocessRunner = _default_runner,
) -> IssueDetail:
    """Issue body + comments + labels + author + state."""
    args = [
        "issue",
        "view",
        str(number),
        "--repo",
        repo,
        "--json",
        "number,title,body,labels,author,comments,url,createdAt,state",
    ]
    raw = _run_gh_json(args, runner=runner)
    return IssueDetail.model_validate(raw)


def add_labels(
    repo: str,
    number: int,
    labels: Sequence[str],
    *,
    runner: SubprocessRunner = _default_runner,
) -> None:
    if not labels:
        return
    args = ["issue", "edit", str(number), "--repo", repo]
    for lab in labels:
        args += ["--add-label", lab]
    _run_gh(args, runner=runner)


def remove_labels(
    repo: str,
    number: int,
    labels: Sequence[str],
    *,
    runner: SubprocessRunner = _default_runner,
) -> None:
    if not labels:
        return
    args = ["issue", "edit", str(number), "--repo", repo]
    for lab in labels:
        args += ["--remove-label", lab]
    _run_gh(args, runner=runner)


def comment(
    repo: str,
    number: int,
    body: str,
    *,
    runner: SubprocessRunner = _default_runner,
) -> CommentRef:
    """Post a top-level comment on `repo#number`. Returns the comment URL."""
    out = _run_gh(
        ["issue", "comment", str(number), "--repo", repo, "--body", body],
        runner=runner,
    )
    return CommentRef(url=out.strip())


_ISSUE_PR_LINK_RE = re.compile(r"#issuecomment-(\d+)$")


def find_pr_for_issue(
    repo: str,
    issue_number: int,
    *,
    runner: SubprocessRunner = _default_runner,
) -> PRSummary | None:
    """Best-effort lookup of a PR that references this issue.

    Searches PR bodies for "#N". GitHub's search returns recent matches first;
    we take the first hit. None if no PR references the issue.
    """
    args = [
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "all",
        "--limit",
        "5",
        "--search",
        f'in:body "#{issue_number}"',
        "--json",
        "number,title,state,headRefName,url",
    ]
    raw = _run_gh_json(args, runner=runner)
    if not isinstance(raw, list) or not raw:
        return None
    return PRSummary.model_validate(raw[0])


_REVIEW_ACTIONS = {"approve", "request-changes", "comment"}


def review_pr(
    repo: str,
    pr_number: int,
    *,
    action: str,
    body: str | None = None,
    runner: SubprocessRunner = _default_runner,
) -> None:
    if action not in _REVIEW_ACTIONS:
        raise GhError(f"unsupported review action: {action!r}")
    args = ["pr", "review", str(pr_number), "--repo", repo, f"--{action}"]
    if body is not None:
        args += ["--body", body]
    _run_gh(args, runner=runner)


_MERGE_METHODS = {"squash", "merge", "rebase"}


def merge_pr(
    repo: str,
    pr_number: int,
    *,
    method: str = "squash",
    delete_branch: bool = True,
    runner: SubprocessRunner = _default_runner,
) -> None:
    if method not in _MERGE_METHODS:
        raise GhError(f"unsupported merge method: {method!r}")
    args = ["pr", "merge", str(pr_number), "--repo", repo, f"--{method}"]
    if delete_branch:
        args.append("--delete-branch")
    _run_gh(args, runner=runner)


_HTML_COMMENT_NS = "agent-fabric"
_COMMENT_ID_RE = re.compile(r"#issuecomment-(\d+)\b")


def _parse_comment_id(url: str) -> int | None:
    m = _COMMENT_ID_RE.search(url or "")
    return int(m.group(1)) if m else None


def set_html_comment(
    repo: str,
    issue_number: int,
    *,
    marker: str,
    value: str,
    runner: SubprocessRunner = _default_runner,
) -> CommentRef:
    """Idempotently set a fabric-managed comment carrying a small payload.

    The comment body is `<!-- agent-fabric:{marker} -->\\n{value}`. Subsequent
    calls with the same `marker` edit the existing comment instead of
    creating a duplicate. Used by the cycle counter (1C) and any future
    fabric-managed metadata mirrored to GH.
    """
    sentinel = f"<!-- {_HTML_COMMENT_NS}:{marker} -->"
    body = f"{sentinel}\n{value}"

    detail_raw = _run_gh_json(
        [
            "issue",
            "view",
            str(issue_number),
            "--repo",
            repo,
            "--json",
            "comments",
        ],
        runner=runner,
    )
    for c in detail_raw.get("comments", []) or []:
        c_body = c.get("body", "")
        if not c_body.startswith(sentinel):
            continue
        comment_id = _parse_comment_id(c.get("url", ""))
        if comment_id is None:
            continue
        _run_gh(
            [
                "api",
                "-X",
                "PATCH",
                f"/repos/{repo}/issues/comments/{comment_id}",
                "--input",
                "-",
            ],
            stdin=json.dumps({"body": body}),
            runner=runner,
        )
        return CommentRef(url=c.get("url", ""))

    return comment(repo, issue_number, body, runner=runner)


__all__ = [
    "Author",
    "CommentRef",
    "GhError",
    "IssueComment",
    "IssueDetail",
    "IssueSummary",
    "Label",
    "PRSummary",
    "SubprocessRunner",
    "add_labels",
    "comment",
    "find_pr_for_issue",
    "get_issue",
    "list_issues",
    "merge_pr",
    "remove_labels",
    "review_pr",
    "set_html_comment",
]
