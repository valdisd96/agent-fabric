from __future__ import annotations

from pathlib import Path

import pytest

from fabric.state import (
    StateError,
    acknowledge_notification,
    add_notification,
    complete_dispatch,
    connect,
    consecutive_failures,
    delete_project,
    delete_setting,
    dispatches_for_issue,
    get_deployment,
    get_issue,
    get_project,
    get_setting,
    inc_quota,
    init_db,
    latest_deployment,
    latest_dispatch,
    list_deployments,
    list_issues,
    list_projects,
    list_unacked_notifications,
    previous_successful_deployment,
    quota_today,
    recent_dispatches,
    record_deployment,
    record_dispatch,
    set_cycle_count,
    set_deployment_diagnose_dispatch,
    set_project_last_served,
    set_project_paused,
    set_setting,
    state_db_path,
    upsert_issue,
    upsert_project,
)


# ---------- init / migrations ----------


def test_state_db_path_under_fabric_home(isolated_fabric_home: Path) -> None:
    assert state_db_path() == isolated_fabric_home / "state.db"


def test_init_db_creates_tables(isolated_fabric_home: Path) -> None:
    p = init_db()
    assert p.exists()
    expected = {
        "schema_version",
        "projects",
        "issues",
        "dispatches",
        "notifications",
        "quota_log",
        "settings",
    }
    with connect(p) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    found = {r["name"] for r in rows}
    assert expected.issubset(found)


def test_init_db_records_schema_version(isolated_state_db: Path) -> None:
    from fabric.state import _MIGRATIONS

    with connect(isolated_state_db) as conn:
        rows = conn.execute("SELECT version FROM schema_version").fetchall()
    assert {r["version"] for r in rows} == {v for v, _ in _MIGRATIONS}


def test_init_db_is_idempotent(isolated_fabric_home: Path) -> None:
    p1 = init_db()
    with connect(p1) as conn:
        n1 = conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()["n"]
    init_db()
    with connect(p1) as conn:
        n2 = conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()["n"]
    assert n1 == n2 and n1 > 0


def test_pragmas_are_set(isolated_state_db: Path) -> None:
    with connect(isolated_state_db) as conn:
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert journal.lower() == "wal"
    assert fk == 1


# ---------- settings ----------


def test_set_get_setting_roundtrip(isolated_state_db: Path) -> None:
    set_setting("paused", "1")
    assert get_setting("paused") == "1"


def test_set_setting_overwrites(isolated_state_db: Path) -> None:
    set_setting("paused", "1")
    set_setting("paused", "0")
    assert get_setting("paused") == "0"


def test_get_setting_returns_default_when_missing(isolated_state_db: Path) -> None:
    assert get_setting("missing") is None
    assert get_setting("missing", default="0") == "0"


def test_delete_setting(isolated_state_db: Path) -> None:
    set_setting("paused", "1")
    delete_setting("paused")
    assert get_setting("paused") is None


# ---------- projects ----------


def test_upsert_project_inserts_and_lists(isolated_state_db: Path) -> None:
    upsert_project(name="t1", path="/p/t1", repo="me/t1", fabric_version="0.0.1")
    upsert_project(name="t2", path="/p/t2", repo="me/t2")
    rows = list_projects()
    assert [r.name for r in rows] == ["t1", "t2"]
    assert rows[0].fabric_version == "0.0.1"
    assert rows[1].fabric_version is None


def test_upsert_project_updates_existing(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/old", repo="me/t")
    upsert_project(name="t", path="/new", repo="me/t", fabric_version="0.1.0")
    rows = list_projects()
    assert len(rows) == 1
    assert rows[0].path == "/new"
    assert rows[0].fabric_version == "0.1.0"


def test_get_project_returns_paused_and_last_served(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    p = get_project("t")
    assert p is not None
    assert p.paused is False
    assert p.last_served_at is None


def test_get_project_returns_none_for_unknown(isolated_state_db: Path) -> None:
    assert get_project("nope") is None


def test_set_project_paused_round_trip(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    set_project_paused("t", True)
    assert get_project("t").paused is True  # type: ignore[union-attr]
    set_project_paused("t", False)
    assert get_project("t").paused is False  # type: ignore[union-attr]


def test_set_project_paused_raises_when_missing(isolated_state_db: Path) -> None:
    with pytest.raises(StateError, match="not found"):
        set_project_paused("nope", True)


def test_set_project_last_served_persists(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    set_project_last_served("t", "2026-05-04T10:00:00+00:00")
    p = get_project("t")
    assert p is not None and p.last_served_at == "2026-05-04T10:00:00+00:00"


# ---------- issues ----------


def test_upsert_issue_inserts(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    upsert_issue(
        project="t",
        number=1,
        state_label="state:needs-planning",
        priority_label="priority:high",
        title="Hi",
        url="https://example.com/1",
    )
    row = get_issue("t", 1)
    assert row is not None
    assert row.state_label == "state:needs-planning"
    assert row.priority_label == "priority:high"
    assert row.cycle_count == 0
    assert row.last_seen_at  # timestamp populated


def test_upsert_issue_updates_label_and_last_seen(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    upsert_issue(project="t", number=1, state_label="state:needs-planning")
    first_seen = get_issue("t", 1).last_seen_at  # type: ignore[union-attr]

    upsert_issue(project="t", number=1, state_label="state:in-progress")
    second = get_issue("t", 1)
    assert second is not None
    assert second.state_label == "state:in-progress"
    assert second.last_seen_at >= first_seen


def test_get_issue_returns_none_for_unknown(isolated_state_db: Path) -> None:
    assert get_issue("t", 999) is None


def test_list_issues_filters_by_project(isolated_state_db: Path) -> None:
    upsert_project(name="a", path="/a", repo="me/a")
    upsert_project(name="b", path="/b", repo="me/b")
    upsert_issue(project="a", number=1)
    upsert_issue(project="a", number=2)
    upsert_issue(project="b", number=1)

    assert {r.number for r in list_issues("a")} == {1, 2}
    assert {(r.project, r.number) for r in list_issues()} == {
        ("a", 1),
        ("a", 2),
        ("b", 1),
    }


def test_set_cycle_count_updates(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    upsert_issue(project="t", number=1)
    set_cycle_count("t", 1, 3)
    row = get_issue("t", 1)
    assert row is not None and row.cycle_count == 3


def test_set_cycle_count_raises_when_missing(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    with pytest.raises(StateError, match="not found"):
        set_cycle_count("t", 999, 1)


def test_set_cycle_count_rejects_negative(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    upsert_issue(project="t", number=1)
    with pytest.raises(StateError, match="non-negative"):
        set_cycle_count("t", 1, -1)


# ---------- dispatches ----------


def test_record_dispatch_returns_id(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    did = record_dispatch(project="t", issue=1, stage="plan-exec", triggered_by="manual:cli")
    assert isinstance(did, int) and did > 0


def test_complete_dispatch_sets_fields(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    did = record_dispatch(project="t", issue=1, stage="plan-exec")
    complete_dispatch(did, exit_code=0, log_path="/tmp/log.txt")
    row = latest_dispatch("t", 1)
    assert row is not None
    assert row.exit_code == 0
    assert row.log_path == "/tmp/log.txt"
    assert row.ended_at is not None


def test_complete_dispatch_raises_when_missing(isolated_state_db: Path) -> None:
    with pytest.raises(StateError, match="not found"):
        complete_dispatch(9999, exit_code=0)


def test_latest_dispatch_returns_most_recent(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    record_dispatch(project="t", issue=1, stage="plan-exec", started_at="2026-05-01T10:00:00+00:00")
    did2 = record_dispatch(
        project="t", issue=1, stage="test-writer", started_at="2026-05-02T10:00:00+00:00"
    )
    row = latest_dispatch("t", 1)
    assert row is not None
    assert row.id == did2
    assert row.stage == "test-writer"


def test_dispatches_for_issue_orders_desc(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    record_dispatch(project="t", issue=1, stage="plan-exec", started_at="2026-05-01T10:00:00+00:00")
    record_dispatch(project="t", issue=1, stage="plan-exec", started_at="2026-05-02T10:00:00+00:00")
    record_dispatch(project="t", issue=2, stage="plan-exec", started_at="2026-05-03T10:00:00+00:00")

    rows = dispatches_for_issue("t", 1)
    assert len(rows) == 2
    assert rows[0].started_at > rows[1].started_at


def test_consecutive_failures_zero_when_no_dispatches(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    assert consecutive_failures("t", 1) == 0


def test_consecutive_failures_counts_until_success(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    # success → fail → fail (newest first)
    a = record_dispatch(project="t", issue=1, stage="plan-exec", started_at="2026-05-01T10:00:00+00:00")
    complete_dispatch(a, exit_code=0)
    b = record_dispatch(project="t", issue=1, stage="plan-exec", started_at="2026-05-02T10:00:00+00:00")
    complete_dispatch(b, exit_code=1)
    c = record_dispatch(project="t", issue=1, stage="plan-exec", started_at="2026-05-03T10:00:00+00:00")
    complete_dispatch(c, exit_code=1)

    assert consecutive_failures("t", 1) == 2


def test_consecutive_failures_zero_when_last_succeeded(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    a = record_dispatch(project="t", issue=1, stage="plan-exec", started_at="2026-05-01T10:00:00+00:00")
    complete_dispatch(a, exit_code=1)
    b = record_dispatch(project="t", issue=1, stage="plan-exec", started_at="2026-05-02T10:00:00+00:00")
    complete_dispatch(b, exit_code=0)

    assert consecutive_failures("t", 1) == 0


def test_consecutive_failures_skips_running(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    # in-progress dispatch (no exit_code yet) shouldn't count
    record_dispatch(project="t", issue=1, stage="plan-exec", started_at="2026-05-03T10:00:00+00:00")
    assert consecutive_failures("t", 1) == 0


def test_upsert_issue_records_author_and_created_at(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    upsert_issue(
        project="t", number=1, author="alice", created_at="2026-05-01T10:00:00+00:00",
    )
    row = get_issue("t", 1)
    assert row is not None
    assert row.author == "alice"
    assert row.created_at == "2026-05-01T10:00:00+00:00"


def test_upsert_issue_preserves_created_at_on_update(isolated_state_db: Path) -> None:
    """Upsert with new created_at must NOT overwrite the original."""
    upsert_project(name="t", path="/p", repo="me/t")
    upsert_issue(project="t", number=1, created_at="2026-05-01T10:00:00+00:00")
    upsert_issue(project="t", number=1, created_at="2026-99-99T99:99:99+00:00")
    row = get_issue("t", 1)
    assert row is not None and row.created_at == "2026-05-01T10:00:00+00:00"


def test_recent_dispatches_orders_desc(isolated_state_db: Path) -> None:
    upsert_project(name="a", path="/a", repo="me/a")
    upsert_project(name="b", path="/b", repo="me/b")
    record_dispatch(project="a", issue=1, stage="plan-exec", started_at="2026-05-01T10:00:00+00:00")
    record_dispatch(project="b", issue=1, stage="plan-exec", started_at="2026-05-02T10:00:00+00:00")
    record_dispatch(project="a", issue=2, stage="plan-exec", started_at="2026-05-03T10:00:00+00:00")

    rows = recent_dispatches(limit=10)
    assert [(r.project, r.issue) for r in rows] == [("a", 2), ("b", 1), ("a", 1)]

    a_rows = recent_dispatches(project="a", limit=10)
    assert [(r.project, r.issue) for r in a_rows] == [("a", 2), ("a", 1)]


# ---------- quota ----------


def test_inc_quota_increments(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    assert inc_quota("t", day="2026-05-04") == 1
    assert inc_quota("t", day="2026-05-04") == 2
    assert quota_today("t", day="2026-05-04") == 2


def test_quota_today_returns_zero_when_missing(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    assert quota_today("t", day="2026-05-04") == 0


def test_quota_isolated_per_day(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    inc_quota("t", day="2026-05-03")
    inc_quota("t", day="2026-05-04")
    inc_quota("t", day="2026-05-04")
    assert quota_today("t", day="2026-05-03") == 1
    assert quota_today("t", day="2026-05-04") == 2


# ---------- notifications ----------


def test_add_notification_returns_id(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    nid = add_notification(kind="clarification", project="t", issue=1)
    assert isinstance(nid, int) and nid > 0


def test_notification_body_round_trips(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    add_notification(
        kind="state-changed", project="t", issue=1,
        body="line 1\nline 2",
    )
    [n] = list_unacked_notifications()
    assert n.body == "line 1\nline 2"


def test_list_unacked_filters_acked(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    a = add_notification(kind="clarification", project="t", issue=1)
    add_notification(kind="blocked", project="t", issue=2)
    acknowledge_notification(a, delivered_to="telegram")

    unacked = list_unacked_notifications()
    assert {n.kind for n in unacked} == {"blocked"}


def test_acknowledge_notification_raises_when_missing(isolated_state_db: Path) -> None:
    with pytest.raises(StateError, match="not found"):
        acknowledge_notification(9999)


# ---------- deployments ----------


def test_record_deployment_returns_row_and_round_trips(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    dep = record_deployment(
        project="t",
        sha="abc1234deadbeef",
        short_sha="abc1234",
        status="success",
        deployed_at="2026-05-06T18:00:00+00:00",
        branch="main",
        actor="valdisd96",
        workflow_run_url="https://github.com/me/t/actions/runs/123",
    )
    assert isinstance(dep.id, int) and dep.id > 0
    assert dep.sha == "abc1234deadbeef"
    assert dep.diagnose_dispatch_id is None

    # Returned row matches what get_deployment fetches back.
    fetched = get_deployment(dep.id)
    assert fetched == dep


def test_record_deployment_failure_keeps_journal(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    dep = record_deployment(
        project="t",
        sha="def5678",
        status="failed",
        deployed_at="2026-05-06T18:05:00+00:00",
        service_unit="t.service",
        journal_tail="Traceback (most recent call last)\nModuleNotFoundError: redis",
        workflow_run_url="https://github.com/me/t/actions/runs/124",
    )
    assert dep.status == "failed"
    assert dep.journal_tail is not None and "ModuleNotFoundError" in dep.journal_tail
    assert dep.service_unit == "t.service"


def test_record_deployment_rejects_bad_status(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    with pytest.raises(StateError, match="must be 'success' or 'failed'"):
        record_deployment(project="t", sha="abc", status="weird")


def test_get_deployment_returns_none_for_unknown(isolated_state_db: Path) -> None:
    assert get_deployment(9999) is None


def test_list_deployments_orders_newest_first(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    record_deployment(
        project="t", sha="aaa", status="success",
        deployed_at="2026-05-01T10:00:00+00:00",
    )
    record_deployment(
        project="t", sha="bbb", status="failed",
        deployed_at="2026-05-02T10:00:00+00:00",
    )
    record_deployment(
        project="t", sha="ccc", status="success",
        deployed_at="2026-05-03T10:00:00+00:00",
    )
    rows = list_deployments("t")
    assert [r.sha for r in rows] == ["ccc", "bbb", "aaa"]


def test_list_deployments_filters_by_status(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    record_deployment(
        project="t", sha="ok1", status="success",
        deployed_at="2026-05-01T10:00:00+00:00",
    )
    record_deployment(
        project="t", sha="bad1", status="failed",
        deployed_at="2026-05-02T10:00:00+00:00",
    )
    record_deployment(
        project="t", sha="ok2", status="success",
        deployed_at="2026-05-03T10:00:00+00:00",
    )
    success_only = list_deployments("t", status="success")
    failed_only = list_deployments("t", status="failed")
    assert [r.sha for r in success_only] == ["ok2", "ok1"]
    assert [r.sha for r in failed_only] == ["bad1"]


def test_list_deployments_respects_limit(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    for i in range(5):
        record_deployment(
            project="t", sha=f"sha{i}", status="success",
            deployed_at=f"2026-05-0{i + 1}T10:00:00+00:00",
        )
    assert len(list_deployments("t", limit=2)) == 2


def test_latest_deployment_returns_most_recent(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    record_deployment(
        project="t", sha="ok", status="success",
        deployed_at="2026-05-01T10:00:00+00:00",
    )
    record_deployment(
        project="t", sha="bad", status="failed",
        deployed_at="2026-05-02T10:00:00+00:00",
    )
    # without filter — newest of any status
    latest = latest_deployment("t")
    assert latest is not None and latest.sha == "bad"
    # filtered — newest success only
    latest_ok = latest_deployment("t", status="success")
    assert latest_ok is not None and latest_ok.sha == "ok"


def test_latest_deployment_returns_none_when_empty(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    assert latest_deployment("t") is None
    assert latest_deployment("t", status="success") is None


def test_previous_successful_deployment(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    ok1 = record_deployment(
        project="t", sha="ok1", status="success",
        deployed_at="2026-05-01T10:00:00+00:00",
    )
    record_deployment(
        project="t", sha="bad1", status="failed",
        deployed_at="2026-05-02T10:00:00+00:00",
    )
    ok2 = record_deployment(
        project="t", sha="ok2", status="success",
        deployed_at="2026-05-03T10:00:00+00:00",
    )
    bad2 = record_deployment(
        project="t", sha="bad2", status="failed",
        deployed_at="2026-05-04T10:00:00+00:00",
    )
    # the failed bad2 should resolve to the most recent prior success: ok2
    prev = previous_successful_deployment("t", before_id=bad2.id)
    assert prev is not None and prev.id == ok2.id

    # before ok1 there is no prior success
    prev_before_ok1 = previous_successful_deployment("t", before_id=ok1.id)
    assert prev_before_ok1 is None


def test_set_deployment_diagnose_dispatch_links(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    dep = record_deployment(project="t", sha="bad", status="failed")
    dispatch_id = record_dispatch(
        project="t", issue=42, stage="deploy-diagnose",
        triggered_by="auto:deploy-failure",
    )
    set_deployment_diagnose_dispatch(dep.id, dispatch_id)
    row = get_deployment(dep.id)
    assert row is not None and row.diagnose_dispatch_id == dispatch_id


def test_set_deployment_diagnose_dispatch_raises_when_missing(
    isolated_state_db: Path,
) -> None:
    with pytest.raises(StateError, match="not found"):
        set_deployment_diagnose_dispatch(9999, 1)


# ---------- foreign-key cascade ----------


def test_delete_project_cascades(isolated_state_db: Path) -> None:
    upsert_project(name="t", path="/p", repo="me/t")
    upsert_issue(project="t", number=1)
    record_dispatch(project="t", issue=1, stage="plan-exec")
    add_notification(kind="blocked", project="t", issue=1)
    inc_quota("t", day="2026-05-04")
    record_deployment(project="t", sha="abc", status="success")

    delete_project("t")

    assert list_issues("t") == []
    assert recent_dispatches(project="t") == []
    assert list_unacked_notifications() == []
    assert quota_today("t", day="2026-05-04") == 0
    assert list_deployments("t") == []
