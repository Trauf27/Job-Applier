"""Sync and scoring orchestration.

`sync_all()` pulls every enabled company's board, upserts postings, and scores
them against the current profile. `rescore_all()` re-runs scoring only — cheap,
local, and what you want after editing your profile.
"""

from __future__ import annotations

from typing import Any

from . import db, matching, sources


def _upsert_job(raw: sources.RawJob, company_name: str) -> tuple[int, bool]:
    """Insert or refresh a posting. Returns (job_id, is_new)."""
    ts = db.now()
    existing = db.query_one(
        "SELECT id FROM jobs WHERE source = ? AND source_job_id = ?",
        (raw.source, raw.source_job_id),
    )
    if existing is None:
        cursor = db.execute(
            """
            INSERT INTO jobs (source, source_job_id, company, title, location, remote,
                              url, description, posted_at, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                raw.source, raw.source_job_id, company_name, raw.title, raw.location,
                int(raw.remote), raw.url, raw.description, raw.posted_at, ts, ts,
            ),
        )
        return int(cursor.lastrowid), True

    db.execute(
        """
        UPDATE jobs
           SET company = ?, title = ?, location = ?, remote = ?, url = ?,
               description = ?, posted_at = ?, last_seen = ?
         WHERE id = ?
        """,
        (
            company_name, raw.title, raw.location, int(raw.remote), raw.url,
            raw.description, raw.posted_at, ts, existing["id"],
        ),
    )
    return int(existing["id"]), False


def score_job_row(job: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    import json

    result = matching.score_job(job, profile)
    db.execute(
        "UPDATE jobs SET score = ?, score_detail = ?, scored_at = ? WHERE id = ?",
        (result["score"], json.dumps(result), db.now(), job["id"]),
    )
    return result


def sync_company(company: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """Fetch one company's board. Never raises — errors are reported per company."""
    name = company["name"] or company["slug"]
    try:
        raw_jobs = sources.fetch(company["ats"], company["slug"])
    except sources.SourceError as exc:
        db.execute(
            "UPDATE companies SET last_error = ?, last_synced = ? WHERE id = ?",
            (str(exc), db.now(), company["id"]),
        )
        return {"company": name, "ok": False, "error": str(exc), "added": 0, "updated": 0}

    added = updated = 0
    for raw in raw_jobs:
        job_id, is_new = _upsert_job(raw, name)
        added += is_new
        updated += not is_new
        job = db.row_to_dict(db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,)))
        if job:
            score_job_row(job, profile)

    db.execute(
        "UPDATE companies SET last_error = NULL, last_synced = ? WHERE id = ?",
        (db.now(), company["id"]),
    )
    return {
        "company": name,
        "ok": True,
        "error": None,
        "added": added,
        "updated": updated,
        "total": len(raw_jobs),
    }


def sync_all() -> dict[str, Any]:
    profile = db.get_profile()
    companies = [dict(r) for r in db.query("SELECT * FROM companies WHERE enabled = 1 ORDER BY name")]
    if not companies:
        return {"results": [], "added": 0, "updated": 0, "errors": 0,
                "message": "No enabled companies. Add some on the Companies tab."}

    results = [sync_company(company, profile) for company in companies]
    return {
        "results": results,
        "added": sum(r["added"] for r in results),
        "updated": sum(r["updated"] for r in results),
        "errors": sum(0 if r["ok"] else 1 for r in results),
    }


def rescore_all() -> dict[str, Any]:
    profile = db.get_profile()
    jobs = [dict(r) for r in db.query("SELECT * FROM jobs")]
    for job in jobs:
        score_job_row(job, profile)
    return {"rescored": len(jobs)}
