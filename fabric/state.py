"""SQLite state store at `$FABRIC_HOME/state.db`.

Schema follows DESIGN.md "Decision 1 — State storage". The DAO layer is
plain `sqlite3` so callers (sync CLI, async server) can use the same API;
async callers wrap each call in `asyncio.to_thread` rather than colour
the whole module with `await`. SQLite at our scale (single writer, low
TPS) handles connection-per-call without ceremony.

Forward-only migrations: each entry in `_MIGRATIONS` is `(version, sql)`
applied in order at `init_db()` time and recorded in `schema_version`.
Later phases add columns by appending entries — never editing past ones.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fabric.registry import fabric_home


class StateError(Exception):
    """Raised on schema or DAO failures we want to surface explicitly."""


@dataclass(frozen=True)
class ProjectRow:
    name: str
    path: str
    repo: str
    fabric_version: str | None
    registered_at: str
    last_served_at: str | None = None
    paused: bool = False


@dataclass(frozen=True)
class IssueRow:
    project: str
    number: int
    state_label: str | None
    type_label: str | None
    priority_label: str | None
    area_label: str | None
    title: str | None
    url: str | None
    cycle_count: int
    last_seen_at: str
    author: str | None = None
    created_at: str | None = None


@dataclass(frozen=True)
class DispatchRow:
    id: int
    project: str
    issue: int
    stage: str
    started_at: str
    ended_at: str | None
    exit_code: int | None
    log_path: str | None
    triggered_by: str


@dataclass(frozen=True)
class NotificationRow:
    id: int
    kind: str
    project: str
    issue: int
    created_at: str
    delivered_to: str | None
    acknowledged_at: str | None
    tg_chat_id: int | None = None
    tg_message_id: int | None = None


def state_db_path() -> Path:
    return fabric_home() / "state.db"


_SCHEMA_V1 = """
CREATE TABLE projects (
  name TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  repo TEXT NOT NULL,
  fabric_version TEXT,
  registered_at TEXT NOT NULL
);

CREATE TABLE issues (
  project TEXT NOT NULL,
  number INTEGER NOT NULL,
  state_label TEXT,
  type_label TEXT,
  priority_label TEXT,
  area_label TEXT,
  title TEXT,
  url TEXT,
  cycle_count INTEGER NOT NULL DEFAULT 0,
  last_seen_at TEXT NOT NULL,
  PRIMARY KEY (project, number),
  FOREIGN KEY (project) REFERENCES projects(name) ON DELETE CASCADE
);

CREATE TABLE dispatches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT NOT NULL,
  issue INTEGER NOT NULL,
  stage TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  exit_code INTEGER,
  log_path TEXT,
  triggered_by TEXT NOT NULL,
  FOREIGN KEY (project) REFERENCES projects(name) ON DELETE CASCADE
);

CREATE INDEX idx_dispatches_project_issue ON dispatches(project, issue);
CREATE INDEX idx_dispatches_started ON dispatches(started_at);

CREATE TABLE notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  project TEXT NOT NULL,
  issue INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  delivered_to TEXT,
  acknowledged_at TEXT,
  FOREIGN KEY (project) REFERENCES projects(name) ON DELETE CASCADE
);

CREATE INDEX idx_notifications_unacked
  ON notifications(acknowledged_at, created_at);

CREATE TABLE quota_log (
  project TEXT NOT NULL,
  day TEXT NOT NULL,
  dispatches INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (project, day),
  FOREIGN KEY (project) REFERENCES projects(name) ON DELETE CASCADE
);

CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""

_SCHEMA_V2 = """
ALTER TABLE projects ADD COLUMN last_served_at TEXT;
ALTER TABLE projects ADD COLUMN paused INTEGER NOT NULL DEFAULT 0;
ALTER TABLE issues ADD COLUMN author TEXT;
ALTER TABLE issues ADD COLUMN created_at TEXT;
"""


_SCHEMA_V3 = """
ALTER TABLE notifications ADD COLUMN tg_chat_id INTEGER;
ALTER TABLE notifications ADD COLUMN tg_message_id INTEGER;
CREATE INDEX idx_notifications_tg
  ON notifications(tg_chat_id, tg_message_id);
"""


_MIGRATIONS: list[tuple[int, str]] = [
    (1, _SCHEMA_V1),
    (2, _SCHEMA_V2),
    (3, _SCHEMA_V3),
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Yield a sqlite3 connection with our pragmas applied."""
    p = db_path or state_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()


def init_db(db_path: Path | None = None) -> Path:
    """Apply pending forward migrations. Idempotent. Returns DB path."""
    p = db_path or state_db_path()
    with connect(p) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "  version INTEGER PRIMARY KEY,"
            "  applied_at TEXT NOT NULL"
            ")"
        )
        applied = {r[0] for r in conn.execute("SELECT version FROM schema_version")}
        for version, sql in _MIGRATIONS:
            if version in applied:
                continue
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, _utc_now()),
            )
            conn.commit()
    return p


# ---- settings DAO ----


def get_setting(key: str, default: str | None = None) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()


def delete_setting(key: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        conn.commit()


# ---- projects DAO ----


def upsert_project(
    *, name: str, path: str, repo: str, fabric_version: str | None = None
) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects (name, path, repo, fabric_version, registered_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET "
            "  path = excluded.path, "
            "  repo = excluded.repo, "
            "  fabric_version = excluded.fabric_version",
            (name, path, repo, fabric_version, _utc_now()),
        )
        conn.commit()


def _project_from_row(r: sqlite3.Row) -> ProjectRow:
    return ProjectRow(
        name=r["name"],
        path=r["path"],
        repo=r["repo"],
        fabric_version=r["fabric_version"],
        registered_at=r["registered_at"],
        last_served_at=r["last_served_at"],
        paused=bool(r["paused"]),
    )


_PROJECT_COLS = (
    "name, path, repo, fabric_version, registered_at, last_served_at, paused"
)


def list_projects() -> list[ProjectRow]:
    with connect() as conn:
        rows = conn.execute(
            f"SELECT {_PROJECT_COLS} FROM projects ORDER BY name"
        ).fetchall()
        return [_project_from_row(r) for r in rows]


def get_project(name: str) -> ProjectRow | None:
    with connect() as conn:
        row = conn.execute(
            f"SELECT {_PROJECT_COLS} FROM projects WHERE name = ?", (name,)
        ).fetchone()
        return _project_from_row(row) if row else None


def set_project_paused(name: str, paused: bool) -> None:
    with connect() as conn:
        result = conn.execute(
            "UPDATE projects SET paused = ? WHERE name = ?",
            (1 if paused else 0, name),
        )
        if result.rowcount == 0:
            raise StateError(f"project {name!r} not found")
        conn.commit()


def set_project_last_served(name: str, ts: str) -> None:
    with connect() as conn:
        result = conn.execute(
            "UPDATE projects SET last_served_at = ? WHERE name = ?",
            (ts, name),
        )
        if result.rowcount == 0:
            raise StateError(f"project {name!r} not found")
        conn.commit()


def delete_project(name: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM projects WHERE name = ?", (name,))
        conn.commit()


# ---- issues DAO ----


def upsert_issue(
    *,
    project: str,
    number: int,
    state_label: str | None = None,
    type_label: str | None = None,
    priority_label: str | None = None,
    area_label: str | None = None,
    title: str | None = None,
    url: str | None = None,
    author: str | None = None,
    created_at: str | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO issues (project, number, state_label, type_label, "
            "                    priority_label, area_label, title, url, "
            "                    author, created_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(project, number) DO UPDATE SET "
            "  state_label = excluded.state_label, "
            "  type_label = excluded.type_label, "
            "  priority_label = excluded.priority_label, "
            "  area_label = excluded.area_label, "
            "  title = excluded.title, "
            "  url = excluded.url, "
            "  author = excluded.author, "
            "  created_at = COALESCE(issues.created_at, excluded.created_at), "
            "  last_seen_at = excluded.last_seen_at",
            (
                project,
                number,
                state_label,
                type_label,
                priority_label,
                area_label,
                title,
                url,
                author,
                created_at,
                _utc_now(),
            ),
        )
        conn.commit()


def get_issue(project: str, number: int) -> IssueRow | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM issues WHERE project = ? AND number = ?", (project, number)
        ).fetchone()
        return IssueRow(**dict(row)) if row else None


def list_issues(project: str | None = None) -> list[IssueRow]:
    with connect() as conn:
        if project is None:
            rows = conn.execute("SELECT * FROM issues ORDER BY project, number").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM issues WHERE project = ? ORDER BY number", (project,)
            ).fetchall()
        return [IssueRow(**dict(r)) for r in rows]


def set_cycle_count(project: str, number: int, count: int) -> None:
    """Overwrite cycle_count. Used by the 1C max-of-both cold-boot logic."""
    if count < 0:
        raise StateError("cycle_count must be non-negative")
    with connect() as conn:
        result = conn.execute(
            "UPDATE issues SET cycle_count = ? WHERE project = ? AND number = ?",
            (count, project, number),
        )
        if result.rowcount == 0:
            raise StateError(f"issue {project}#{number} not found")
        conn.commit()


# ---- dispatches DAO ----


def record_dispatch(
    *,
    project: str,
    issue: int,
    stage: str,
    started_at: str | None = None,
    ended_at: str | None = None,
    exit_code: int | None = None,
    log_path: str | None = None,
    triggered_by: str = "manual",
) -> int:
    """Insert a dispatch row. `started_at` defaults to now-UTC.

    Pass `ended_at`/`exit_code`/`log_path` for one-step recording, or omit
    them and call `complete_dispatch` after the subprocess exits.
    """
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO dispatches (project, issue, stage, started_at, ended_at, "
            "                        exit_code, log_path, triggered_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project,
                issue,
                stage,
                started_at or _utc_now(),
                ended_at,
                exit_code,
                log_path,
                triggered_by,
            ),
        )
        conn.commit()
        if cur.lastrowid is None:
            raise StateError("INSERT into dispatches returned no rowid")
        return cur.lastrowid


def complete_dispatch(
    dispatch_id: int,
    *,
    ended_at: str | None = None,
    exit_code: int,
    log_path: str | None = None,
) -> None:
    """Mark an in-flight dispatch row complete."""
    with connect() as conn:
        result = conn.execute(
            "UPDATE dispatches SET ended_at = ?, exit_code = ?, log_path = ? WHERE id = ?",
            (ended_at or _utc_now(), exit_code, log_path, dispatch_id),
        )
        if result.rowcount == 0:
            raise StateError(f"dispatch {dispatch_id} not found")
        conn.commit()


def latest_dispatch(project: str, issue: int) -> DispatchRow | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM dispatches WHERE project = ? AND issue = ? "
            "ORDER BY started_at DESC, id DESC LIMIT 1",
            (project, issue),
        ).fetchone()
        return DispatchRow(**dict(row)) if row else None


def dispatches_for_issue(project: str, issue: int, *, limit: int = 50) -> list[DispatchRow]:
    """Newest-first dispatch history for one issue."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM dispatches WHERE project = ? AND issue = ? "
            "ORDER BY started_at DESC, id DESC LIMIT ?",
            (project, issue, limit),
        ).fetchall()
        return [DispatchRow(**dict(r)) for r in rows]


def consecutive_failures(project: str, issue: int) -> int:
    """How many failed dispatches since the most recent success (or all)."""
    rows = dispatches_for_issue(project, issue, limit=50)
    count = 0
    for r in rows:  # newest first
        if r.exit_code is None:
            continue  # still running, ignore
        if r.exit_code == 0:
            break
        count += 1
    return count


def recent_dispatches(*, project: str | None = None, limit: int = 50) -> list[DispatchRow]:
    with connect() as conn:
        if project is None:
            rows = conn.execute(
                "SELECT * FROM dispatches ORDER BY started_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM dispatches WHERE project = ? "
                "ORDER BY started_at DESC, id DESC LIMIT ?",
                (project, limit),
            ).fetchall()
        return [DispatchRow(**dict(r)) for r in rows]


# ---- quota DAO ----


def inc_quota(project: str, *, day: str | None = None) -> int:
    """Bump dispatch count for (project, day-UTC). Returns new count."""
    d = day or _utc_today()
    with connect() as conn:
        conn.execute(
            "INSERT INTO quota_log (project, day, dispatches) VALUES (?, ?, 1) "
            "ON CONFLICT(project, day) DO UPDATE SET "
            "  dispatches = quota_log.dispatches + 1",
            (project, d),
        )
        row = conn.execute(
            "SELECT dispatches FROM quota_log WHERE project = ? AND day = ?",
            (project, d),
        ).fetchone()
        conn.commit()
        return int(row[0])


def quota_today(project: str, *, day: str | None = None) -> int:
    d = day or _utc_today()
    with connect() as conn:
        row = conn.execute(
            "SELECT dispatches FROM quota_log WHERE project = ? AND day = ?",
            (project, d),
        ).fetchone()
        return int(row[0]) if row else 0


# ---- notifications DAO ----


def add_notification(*, kind: str, project: str, issue: int) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO notifications (kind, project, issue, created_at) VALUES (?, ?, ?, ?)",
            (kind, project, issue, _utc_now()),
        )
        conn.commit()
        if cur.lastrowid is None:
            raise StateError("INSERT into notifications returned no rowid")
        return cur.lastrowid


def acknowledge_notification(
    notification_id: int, *, delivered_to: str | None = None
) -> None:
    with connect() as conn:
        result = conn.execute(
            "UPDATE notifications SET acknowledged_at = ?, delivered_to = ? WHERE id = ?",
            (_utc_now(), delivered_to, notification_id),
        )
        if result.rowcount == 0:
            raise StateError(f"notification {notification_id} not found")
        conn.commit()


def list_unacked_notifications() -> list[NotificationRow]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM notifications WHERE acknowledged_at IS NULL "
            "ORDER BY created_at, id"
        ).fetchall()
        return [NotificationRow(**dict(r)) for r in rows]


def set_notification_tg_message(
    notification_id: int, *, chat_id: int, message_id: int
) -> None:
    """Record the Telegram message we sent for this notification."""
    with connect() as conn:
        result = conn.execute(
            "UPDATE notifications SET tg_chat_id = ?, tg_message_id = ? WHERE id = ?",
            (chat_id, message_id, notification_id),
        )
        if result.rowcount == 0:
            raise StateError(f"notification {notification_id} not found")
        conn.commit()


def find_notification_by_tg_message(
    chat_id: int, message_id: int
) -> NotificationRow | None:
    """Reverse-lookup: given a TG (chat_id, message_id), find the notification."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM notifications WHERE tg_chat_id = ? AND tg_message_id = ?",
            (chat_id, message_id),
        ).fetchone()
        return NotificationRow(**dict(row)) if row else None


__all__ = [
    "DispatchRow",
    "IssueRow",
    "NotificationRow",
    "ProjectRow",
    "StateError",
    "acknowledge_notification",
    "add_notification",
    "complete_dispatch",
    "connect",
    "consecutive_failures",
    "delete_project",
    "delete_setting",
    "dispatches_for_issue",
    "find_notification_by_tg_message",
    "get_issue",
    "get_project",
    "get_setting",
    "inc_quota",
    "init_db",
    "latest_dispatch",
    "list_issues",
    "list_projects",
    "list_unacked_notifications",
    "quota_today",
    "recent_dispatches",
    "record_dispatch",
    "set_cycle_count",
    "set_notification_tg_message",
    "set_project_last_served",
    "set_project_paused",
    "set_setting",
    "state_db_path",
    "upsert_issue",
    "upsert_project",
]
