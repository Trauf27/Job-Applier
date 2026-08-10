"""SQLite storage layer.

One connection per thread, foreign keys on, WAL for concurrent reads. The schema
is created on first use and migrated forward by `SCHEMA_STATEMENTS` being
idempotent (CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

from . import config

_local = threading.local()

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS profile (
        id          INTEGER PRIMARY KEY CHECK (id = 1),
        data        TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS companies (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        ats         TEXT NOT NULL,
        slug        TEXT NOT NULL,
        enabled     INTEGER NOT NULL DEFAULT 1,
        last_synced TEXT,
        last_error  TEXT,
        UNIQUE (ats, slug)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        source        TEXT NOT NULL,
        source_job_id TEXT NOT NULL,
        company       TEXT NOT NULL,
        title         TEXT NOT NULL,
        location      TEXT,
        remote        INTEGER NOT NULL DEFAULT 0,
        url           TEXT NOT NULL,
        description   TEXT NOT NULL DEFAULT '',
        posted_at     TEXT,
        first_seen    TEXT NOT NULL,
        last_seen     TEXT NOT NULL,
        score         REAL,
        score_detail  TEXT,
        scored_at     TEXT,
        hidden        INTEGER NOT NULL DEFAULT 0,
        UNIQUE (source, source_job_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs (score DESC)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs (company)",
    """
    CREATE TABLE IF NOT EXISTS applications (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id      INTEGER NOT NULL UNIQUE REFERENCES jobs (id) ON DELETE CASCADE,
        status      TEXT NOT NULL DEFAULT 'saved',
        notes       TEXT NOT NULL DEFAULT '',
        applied_at  TEXT,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_apps_status ON applications (status)",
    """
    CREATE TABLE IF NOT EXISTS application_events (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER NOT NULL REFERENCES applications (id) ON DELETE CASCADE,
        kind           TEXT NOT NULL,
        detail         TEXT NOT NULL DEFAULT '',
        created_at     TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_app ON application_events (application_id)",
    """
    CREATE TABLE IF NOT EXISTS documents (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id      INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
        kind        TEXT NOT NULL,
        content     TEXT NOT NULL,
        created_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_docs_job ON documents (job_id, kind)",
    """
    CREATE TABLE IF NOT EXISTS agent_settings (
        id          INTEGER PRIMARY KEY CHECK (id = 1),
        data        TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_runs (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        trigger      TEXT NOT NULL,
        dry_run      INTEGER NOT NULL DEFAULT 1,
        status       TEXT NOT NULL DEFAULT 'running',
        started_at   TEXT NOT NULL,
        finished_at  TEXT,
        stats        TEXT NOT NULL DEFAULT '{}',
        error        TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_runs_started ON agent_runs (started_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS agent_actions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id      INTEGER NOT NULL REFERENCES agent_runs (id) ON DELETE CASCADE,
        job_id      INTEGER REFERENCES jobs (id) ON DELETE SET NULL,
        stage       TEXT NOT NULL,
        decision    TEXT NOT NULL,
        detail      TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_actions_run ON agent_actions (run_id)",
    "CREATE INDEX IF NOT EXISTS idx_actions_job ON agent_actions (job_id, stage)",
    """
    CREATE TABLE IF NOT EXISTS apply_attempts (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id      INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
        method      TEXT NOT NULL,
        status      TEXT NOT NULL,
        form_url    TEXT NOT NULL DEFAULT '',
        filled      TEXT NOT NULL DEFAULT '[]',
        unfilled    TEXT NOT NULL DEFAULT '[]',
        detail      TEXT NOT NULL DEFAULT '',
        screenshot  TEXT,
        created_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_attempts_job ON apply_attempts (job_id)",
]

# Columns added after the first release. Applied by `init()` when missing, so an
# existing database upgrades in place without a migration tool.
COLUMN_MIGRATIONS = [
    ("jobs", "apply_url", "TEXT"),
]

# Pipeline stages, in the order they appear in the UI.
STATUSES = [
    "saved",
    "ready",
    "applied",
    "screening",
    "interview",
    "offer",
    "rejected",
    "withdrawn",
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        config.ensure_dirs()
        conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        _local.conn = conn
    return conn


def init() -> None:
    conn = connect()
    with conn:
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)
        for table, column, column_type in COLUMN_MIGRATIONS:
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def query(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return connect().execute(sql, tuple(params)).fetchall()


def query_one(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return connect().execute(sql, tuple(params)).fetchone()


def execute(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    conn = connect()
    with conn:
        return conn.execute(sql, tuple(params))


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


# --- profile -----------------------------------------------------------------

EMPTY_PROFILE: dict[str, Any] = {
    "name": "",
    "email": "",
    "phone": "",
    "location": "",
    "links": [],
    "headline": "",
    "summary": "",
    "skills": [],
    "target_titles": [],
    "target_locations": [],
    "remote_ok": True,
    "min_salary": None,
    "seniority": "",
    "exclude_keywords": [],
    "experience": [],
    "education": [],
    "certifications": [],
}


def get_profile() -> dict[str, Any]:
    row = query_one("SELECT data, updated_at FROM profile WHERE id = 1")
    if row is None:
        return dict(EMPTY_PROFILE, updated_at=None)
    data = dict(EMPTY_PROFILE)
    data.update(json.loads(row["data"]))
    data["updated_at"] = row["updated_at"]
    return data


def save_profile(data: dict[str, Any]) -> dict[str, Any]:
    merged = dict(EMPTY_PROFILE)
    merged.update({k: v for k, v in data.items() if k in EMPTY_PROFILE})
    execute(
        """
        INSERT INTO profile (id, data, updated_at) VALUES (1, ?, ?)
        ON CONFLICT (id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at
        """,
        (json.dumps(merged), now()),
    )
    return get_profile()


# --- applications ------------------------------------------------------------


def get_or_create_application(job_id: int, status: str = "saved") -> dict[str, Any]:
    row = query_one("SELECT * FROM applications WHERE job_id = ?", (job_id,))
    if row is not None:
        return dict(row)
    ts = now()
    cur = execute(
        """
        INSERT INTO applications (job_id, status, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        (job_id, status, ts, ts),
    )
    add_event(cur.lastrowid, "created", f"Tracked with status '{status}'")
    return dict(query_one("SELECT * FROM applications WHERE id = ?", (cur.lastrowid,)))


def add_event(application_id: int, kind: str, detail: str = "") -> None:
    execute(
        """
        INSERT INTO application_events (application_id, kind, detail, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (application_id, kind, detail, now()),
    )


def set_application_status(application_id: int, status: str, detail: str = "") -> dict[str, Any]:
    """Move an application to `status`, stamping `applied_at` the first time it lands
    on 'applied' and logging the transition. Shared by the UI and the agent so both
    produce the same event history."""
    row = query_one("SELECT * FROM applications WHERE id = ?", (application_id,))
    if row is None:
        raise KeyError(f"No application with id {application_id}")
    if status not in STATUSES:
        raise ValueError(f"status must be one of: {', '.join(STATUSES)}")

    if status != row["status"]:
        add_event(application_id, "status", detail or f"{row['status']} -> {status}")
    applied_at = row["applied_at"]
    if status == "applied" and not applied_at:
        applied_at = now()
    execute(
        "UPDATE applications SET status = ?, applied_at = ?, updated_at = ? WHERE id = ?",
        (status, applied_at, now(), application_id),
    )
    return dict(query_one("SELECT * FROM applications WHERE id = ?", (application_id,)))
