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
